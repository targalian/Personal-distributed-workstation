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
            print(f"[Secretary] 模型路由器已加载: {self.model_router.pool_size} 个模型")

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

        如果是 Worker,自动更新 UDP 发现列表。
        Worker 会通过 HTTP 主动注册。
        """
        # 这里只做日志,实际注册由 Worker 主动发起 HTTP 请求
        if packet.role == "worker" and packet.device_id:
            # 如果 Worker 尚未通过 HTTP 注册,先在 UDP 层记录
            pass

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
            print(f"[Secretary] 配置报告刷新异常: {e}")

    def _config_refresh_loop(self):
        """定期刷新共享文件夹中的配置报告。"""
        while self._running:
            time.sleep(HEARTBEAT_INTERVAL_SECS)
            self._refresh_host_config()

    def _prune_loop(self):
        """定期清理超时离线主机。"""
        while self._running:
            time.sleep(PRUNE_INTERVAL_SECS)
            try:
                self.station_director.prune_offline(self.cfg.discovery.device_ttl)
            except Exception as e:
                print(f"[Secretary] 清理离线主机异常: {e}")

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
        """创建 Secretary 的 FastAPI 应用 (含 API + Web UI)。"""
        app = FastAPI(title="LAN Mesh Secretary", version="0.1.0")

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
                return HTMLResponse(content=html_path.read_text(encoding="utf-8"))
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
            print("[Secretary] 自检未通过,启动中止。请根据上述提示修复后重试。")
            sys.exit(1)

        self.state.api_port = self._find_available_port(self.cfg.secretary.api_port)

        print(f"[Secretary] 设备 ID: {self.state.device_id}")
        print(f"[Secretary] 设备名称: {self.state.device_name}")
        print(f"[Secretary] 共享目录: {self.state.shared_folder.path}")
        print(f"[Secretary] 数据库: {get_db_path(self.cfg)}")
        print(f"[Secretary] HTTP API + Web UI 端口: {self.state.api_port}")

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
        print(f"[Secretary] 配置报告已生成: {self.state.shared_folder.path}/host_config.json")

        # 启动配置刷新线程
        config_thread = threading.Thread(
            target=self._config_refresh_loop, name="secretary-config-refresh", daemon=True
        )
        config_thread.start()
        self._threads.append(config_thread)

        # 启动离线清理线程
        prune_thread = threading.Thread(
            target=self._prune_loop, name="secretary-prune", daemon=True
        )
        prune_thread.start()
        self._threads.append(prune_thread)

        # 创建 FastAPI 应用
        app = self._create_app()

        # 添加 WS 推送后台任务
        @app.on_event("startup")
        async def startup_event():
            asyncio.create_task(self._ws_push_loop())

        config = uvicorn.Config(
            app,
            host="0.0.0.0",
            port=self.state.api_port,
            log_level="warning",
        )
        server = uvicorn.Server(config)

        local_ips = self._collect_info().ip_addresses
        print(f"\n[Secretary] 服务已启动!")
        print(f"  Web UI:  http://localhost:{self.state.api_port}")
        for ip in local_ips:
            print(f"  局域网:  http://{ip}:{self.state.api_port}")
        print(f"\n[Secretary] 等待 Worker 节点注册...\n")

        try:
            server.run()
        except KeyboardInterrupt:
            print("\n[Secretary] 正在停止...")
            self.stop()

    def stop(self):
        """停止 Secretary。"""
        self._running = False
        if self.discovery:
            self.discovery.stop()
