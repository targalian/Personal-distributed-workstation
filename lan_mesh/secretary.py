"""
Secretary Controller - 中心控制节点

职责:
1. 自动采集本机配置 (Secretary 本身也是一台主机)
2. 自动创建共享文件夹
3. UDP 广播发现 (广播 Secretary 角色,监听 Worker presence)
4. 接收 Worker HTTP 注册与心跳
5. 持久化主机信息到 SQLite
6. 提供 Web UI 仪表盘
7. 定期清理超时离线主机

启动流程:
  生成 device_id → 初始化 SQLite → 采集 host_info → 创建 shared_folder
  → 启动 FastAPI (含 Web UI) → 启动 UDP 发现 → 启动离线清理循环
"""
import asyncio
import shutil
import socket
import sys
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Set

import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from .api import create_secretary_router, create_worker_router, broadcast_ws
from .config import AppConfig, get_db_path, load_model_pool
from .database import Database
from .discovery import DiscoveryService
from .host_info import (
    collect_host_info,
    load_or_create_device_id,
    make_discovery_packet,
)
from .protocol import DiscoveryPacket, HEARTBEAT_INTERVAL_SECS, HostInfo, PRUNE_INTERVAL_SECS
from .shared_folder import SharedFolderManager
from .orchestrator import Orchestrator
from .mcp_gateway import MCPGateway
from .project import ProjectManager
from .model_router import ModelRouter
from .station_director import StationDirector
from .logger import get_logger

logger = get_logger("secretary")


# ── Web UI 模板路径 ─────────────────────────────────────────────

WEB_DIR = Path(__file__).parent / "web"
TEMPLATES_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"


@dataclass
class SecretaryState:
    """Secretary 运行时共享状态。"""
    device_id: str = ""
    device_name: str = ""
    role: str = "secretary"
    api_port: int = 0
    start_time: float = field(default_factory=time.time)
    shared_folder: SharedFolderManager = None
    ws_clients: Set = field(default_factory=set)


class SecretaryController:
    """Secretary 控制器。

    在一台主机上部署后:
    - 通过 UDP 广播自身存在 (角色=secretary)
    - 接收 Worker 的 HTTP 注册和心跳
    - 聚合所有主机信息到 SQLite
    - 提供 Web UI 仪表盘
    """

    def __init__(self, cfg: AppConfig):
        self.cfg = cfg
        self.state = SecretaryState(role="secretary")

        # 数据目录
        self.data_dir = Path.home() / ".lan_mesh"
        self.data_dir.mkdir(parents=True, exist_ok=True)

        # 设备身份
        self.state.device_id = load_or_create_device_id(self.data_dir, role="secretary")
        self.state.device_name = (
            cfg.secretary.device_name
            or f"{socket.gethostname()}-secretary"
        )

        # 数据库
        db_path = get_db_path(cfg)
        self.db = Database(str(db_path))

        # 共享文件夹
        self.state.shared_folder = SharedFolderManager(cfg.secretary.shared_folder)

        # 发现服务
        self.discovery: Optional[DiscoveryService] = None

        # 项目管理器
        self.project_manager = ProjectManager(self.db)

        # 模型路由器 (Phase 2)
        model_pool = load_model_pool()
        self.model_router = ModelRouter(model_pool.models, self.project_manager) if model_pool.models else None
        if self.model_router:
            logger.info("模型路由器已加载: %d 个模型", self.model_router.pool_size)

        # 工作站主管 (Station Director) — 管理主机出入站/评级/资源池
        self.station_director = StationDirector(
            db=self.db,
            discovery=None,  # 延迟绑定, discovery 在 start() 中创建
            shared_folder=self.state.shared_folder,
        )

        # 任务编排器
        self.orchestrator = Orchestrator(self.db, self.project_manager, self.model_router)

        # MCP 工具网关
        self.mcp_gateway = MCPGateway()
        self._mcp_config_path = str(self.data_dir / "mcp_servers.yaml")

        self._running = False
        self._threads: list[threading.Thread] = []

    def _collect_info(self) -> HostInfo:
        """采集 Secretary 自身的主机信息。"""
        return collect_host_info(
            device_id=self.state.device_id,
            device_name=self.state.device_name,
            role="secretary",
            api_port=self.state.api_port,
            shared_folder=str(self.state.shared_folder.path),
            shared_file_count=self.state.shared_folder.file_count(),
            start_time=self.state.start_time,
        )

    def _make_packet(self) -> DiscoveryPacket:
        """生成 Secretary 的 UDP 发现包。"""
        info = self._collect_info()
        packet = make_discovery_packet(info)
        packet.role = "secretary"
        return packet

    def _on_device_seen(self, packet: DiscoveryPacket, ip: str):
        """UDP 发现到新设备时的回调。

        Worker 会通过 HTTP 主动注册, 此处仅记录日志。
        """
        if packet.role == "worker" and packet.device_id:
            logger.debug("UDP 发现 Worker: %s (%s)", packet.device_name, ip)

    def _deploy_config_script(self):
        """将独立采集脚本部署到共享文件夹,供其他主机使用。"""
        script_src = Path(__file__).parent / "collect_config.py"
        script_dest = self.state.shared_folder.path / "collect_config.py"
        if script_src.is_file() and not script_dest.exists():
            shutil.copy2(script_src, script_dest)

    def _refresh_host_config(self):
        """刷新共享文件夹中的本机配置报告。"""
        try:
            info = self._collect_info()
            self.state.shared_folder.write_host_config(info)
        except Exception as e:
            logger.error("配置报告刷新异常: %s", e)

    def _config_refresh_loop(self):
        """定期刷新共享文件夹中的配置报告。"""
        while self._running:
            time.sleep(HEARTBEAT_INTERVAL_SECS)
            if not self._running:
                break
            self._refresh_host_config()

    def _prune_loop(self):
        """定期清理超时离线主机。"""
        while self._running:
            time.sleep(PRUNE_INTERVAL_SECS)
            if not self._running:
                break
            try:
                self.station_director.prune_offline(self.cfg.discovery.device_ttl)
            except Exception as e:
                logger.error("清理离线主机异常: %s", e)

    def _background_scheduler(self):
        """统一后台调度器: 合并配置刷新 + 离线清理, 减少线程数。

        以 HEARTBEAT_INTERVAL_SECS 为基本周期,
        每轮执行配置刷新, 每 N 轮执行一次离线清理。
        """
        prune_every_n = max(1, PRUNE_INTERVAL_SECS // HEARTBEAT_INTERVAL_SECS)
        tick = 0
        while self._running:
            time.sleep(HEARTBEAT_INTERVAL_SECS)
            if not self._running:
                break
            tick += 1
            # 每轮: 刷新配置报告
            self._refresh_host_config()
            # 每 N 轮: 清理离线主机
            if tick % prune_every_n == 0:
                try:
                    self.station_director.prune_offline(self.cfg.discovery.device_ttl)
                except Exception as e:
                    logger.error("清理离线主机异常: %s", e)

    async def _ws_push_loop(self):
        """定期向 WebSocket 客户端推送最新主机状态。"""
        while self._running:
            await asyncio.sleep(3)
            try:
                hosts = self.db.list_hosts()
                await broadcast_ws(self.state, "hosts", [h.to_dict() for h in hosts])
            except Exception:
                pass

    # ── FastAPI 应用 ─────────────────────────────────────────────

    def _create_app(self) -> FastAPI:
        """创建 Secretary 的 FastAPI 应用 (含 API + Web UI, 使用 lifespan 管理生命周期)。"""

        @asynccontextmanager
        async def lifespan(app: FastAPI):
            """FastAPI lifespan: 启动时创建 WS 推送任务, 关闭时优雅停止。"""
            ws_task = asyncio.create_task(self._ws_push_loop())
            yield
            # shutdown: 取消 WS 推送 + 停止编排器线程池
            ws_task.cancel()
            try:
                await ws_task
            except asyncio.CancelledError:
                pass
            self.orchestrator.shutdown(wait=False)
            logger.info("FastAPI lifespan 关闭完成")

        app = FastAPI(title="LAN Mesh Secretary", version="0.2.0", lifespan=lifespan)

        # Secretary API 路由
        secretary_router = create_secretary_router(
            db=self.db,
            discovery=self.discovery,
            collect_info_fn=self._collect_info,
            shared_folder=self.state.shared_folder,
            state=self.state,
            orchestrator=self.orchestrator,
            mcp_gateway=self.mcp_gateway,
            project_manager=self.project_manager,
            model_router=self.model_router,
            station_director=self.station_director,
        )
        app.include_router(secretary_router)

        # Secretary 本身也有 Worker 能力 (可被其他 Secretary 查询)
        worker_router = create_worker_router(
            collect_info_fn=self._collect_info,
            shared_folder=self.state.shared_folder,
        )
        app.include_router(worker_router)

        # 静态文件
        if STATIC_DIR.is_dir():
            app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

        # Web UI 仪表盘
        @app.get("/", response_class=HTMLResponse)
        async def dashboard():
            html_path = TEMPLATES_DIR / "dashboard.html"
            if html_path.is_file():
                # 禁缓存: 避免 UI 更新后浏览器用旧页面导致按钮无响应
                return HTMLResponse(
                    content=html_path.read_text(encoding="utf-8"),
                    headers={"Cache-Control": "no-cache, must-revalidate"})
            return HTMLResponse(content="<h1>LAN Mesh Secretary</h1><p>dashboard.html 未找到</p>")

        return app

    def _find_available_port(self, start: int, count: int = 20) -> int:
        """查找可用端口。"""
        for port in range(start, start + count):
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.bind(("", port))
                    return port
            except OSError:
                continue
        return start

    # ── 生命周期 ─────────────────────────────────────────────────

    def start(self):
        """启动 Secretary 控制器。"""
        self._running = True

        # 启动前自检
        from .preflight import run_preflight
        if not run_preflight("secretary", self.cfg):
            logger.critical("自检未通过, 启动中止。请根据上述提示修复后重试。")
            sys.exit(1)

        self.state.api_port = self._find_available_port(self.cfg.secretary.api_port)

        logger.info("设备 ID: %s", self.state.device_id)
        logger.info("设备名称: %s", self.state.device_name)
        logger.info("共享目录: %s", self.state.shared_folder.path)
        logger.info("数据库: %s", get_db_path(self.cfg))
        logger.info("HTTP API + Web UI 端口: %d", self.state.api_port)

        # 启动 UDP 发现服务
        self.discovery = DiscoveryService(
            device_id=self.state.device_id,
            device_name=self.state.device_name,
            role="secretary",
            api_port=self.state.api_port,
            make_packet=self._make_packet,
            on_device_seen=self._on_device_seen,
            discovery_port=self.cfg.discovery.port,
            presence_interval=self.cfg.discovery.presence_interval,
            device_ttl=self.cfg.discovery.device_ttl,
        )
        # 重新绑定 discovery 到 secretary_router 和 station_director
        self.station_director.bind_discovery(self.discovery)
        self.discovery.start()

        # 部署采集脚本并生成初始配置报告
        self._deploy_config_script()
        self._refresh_host_config()
        logger.info("配置报告已生成: %s/host_config.json", self.state.shared_folder.path)

        # 启动统一后台调度线程 (配置刷新 + 离线清理)
        scheduler_thread = threading.Thread(
            target=self._background_scheduler, name="secretary-scheduler", daemon=True
        )
        scheduler_thread.start()
        self._threads.append(scheduler_thread)

        # 创建 FastAPI 应用 (lifespan 已内置 WS 推送任务)
        app = self._create_app()

        config = uvicorn.Config(
            app,
            host="0.0.0.0",
            port=self.state.api_port,
            log_level="warning",
        )
        server = uvicorn.Server(config)

        local_ips = self._collect_info().ip_addresses
        logger.info("服务已启动!")
        logger.info("  Web UI:  http://localhost:%d", self.state.api_port)
        for ip in local_ips:
            logger.info("  局域网:  http://%s:%d", ip, self.state.api_port)
        logger.info("等待 Worker 节点注册...")

        try:
            server.run()
        except KeyboardInterrupt:
            logger.info("正在停止...")
            self.stop()

    def stop(self):
        """优雅停止 Secretary: 停止发现服务 → 等待后台线程 → 关闭编排器 → 关闭 DB。"""
        logger.info("正在停止 Secretary...")
        self._running = False

        # 1. 停止 UDP 发现
        if self.discovery:
            self.discovery.stop()

        # 2. 等待后台线程结束 (最多 5s)
        for t in self._threads:
            t.join(timeout=5)
            if t.is_alive():
                logger.warning("线程 %s 未能在超时内结束", t.name)
        self._threads.clear()

        # 3. 关闭编排器线程池
        self.orchestrator.shutdown(wait=False)

        # 4. 关闭数据库连接
        if hasattr(self.db, 'close'):
            self.db.close()

        logger.info("Secretary 已停止")
