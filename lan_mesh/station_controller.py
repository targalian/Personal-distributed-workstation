"""
Station Director 独立控制器 — 基础设施管理入口

职责:
1. 通过 UDP 广播发现主机 (role=station)
2. 接收 Worker HTTP 注册与心跳
3. 管理主机评级、资源池、事件记录 (Station Director)
4. 提供 Web UI (Station Tab 为默认)
5. 支持同进程激活/停用 Secretary 模式

启动流程:
  python main.py station → Station Director + Web UI
  → 网页端「启动秘书」激活 Secretary → 加载项目管理组件

设计原则:
- Station Director 只管「机器」, Secretary 只管「项目」
- 激活 Secretary 后同进程加载 Orchestrator/ProjectManager/ModelRouter/MCPGateway
- 停用 Secretary 后卸载上述组件, 回到纯基础设施管理模式
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
from .station_director import StationDirector
from .station_api import create_station_router
from .api import create_worker_router
from .skill_registry import SkillRegistry


# ── Web UI 模板路径 ─────────────────────────────────────────────

WEB_DIR = Path(__file__).parent / "web"
TEMPLATES_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"


@dataclass
class StationState:
    """Station Director 运行时共享状态。"""
    device_id: str = ""
    device_name: str = ""
    role: str = "station"
    api_port: int = 0
    start_time: float = field(default_factory=time.time)
    shared_folder: SharedFolderManager = None
    ws_clients: Set = field(default_factory=set)


class StationController:
    """Station Director 独立控制器。

    启动后:
    - 通过 UDP 广播自身存在 (角色=station)
    - 接收 Worker 的 HTTP 注册和心跳
    - 管理 Station Director (主机评级/资源池)
    - 提供 Web UI (Station Tab 为默认)

    激活 Secretary 后 (同进程):
    - 加载 ProjectManager / Orchestrator / ModelRouter / MCPGateway
    - Web UI 显示 Secretary Tabs (任务/Agent/工具/项目)
    """

    def __init__(self, cfg: AppConfig):
        self.cfg = cfg
        self.state = StationState(role="station")

        # 数据目录
        self.data_dir = Path.home() / ".lan_mesh"
        self.data_dir.mkdir(parents=True, exist_ok=True)

        # 设备身份
        self.state.device_id = load_or_create_device_id(self.data_dir, role="station")
        self.state.device_name = (
            cfg.secretary.device_name
            or f"{socket.gethostname()}-station"
        )

        # 数据库
        db_path = get_db_path(cfg)
        self.db = Database(str(db_path))

        # 共享文件夹
        self.state.shared_folder = SharedFolderManager(cfg.secretary.shared_folder)

        # 发现服务 (延迟创建, start() 中初始化)
        self.discovery: Optional[DiscoveryService] = None

        # 工作站主管
        self.station_director = StationDirector(
            db=self.db,
            discovery=None,
            shared_folder=self.state.shared_folder,
        )

        # 技能库 (项目根目录下的 skills/ 文件夹)
        skills_dir = str(Path(__file__).parent.parent / "skills")
        self.skill_registry = SkillRegistry(self.db, skills_dir)
        self.skill_registry.scan_and_register()

        # Secretary 组件 (初始未加载, activate_secretary() 时创建)
        self.secretary_active = False
        self.project_manager = None
        self.orchestrator = None
        self.model_router = None
        self.mcp_gateway = None
        self._mcp_config_path = str(self.data_dir / "mcp_servers.yaml")

        # Secretary 主机分配追踪 (哪台主机在运行 Secretary)
        self.secretary_host_id = None  # device_id of the host running Secretary
        self.secretary_host_port = None  # Secretary HTTP port on that host

        self._running = False
        self._threads: list[threading.Thread] = []

    # ── Secretary 激活/停用 ───────────────────────────────────────

    def activate_secretary(self) -> dict:
        """同进程激活 Secretary 模式。

        加载项目管理组件: ProjectManager / Orchestrator / ModelRouter / MCPGateway
        激活后 API 路由自动可用 (无需重启)。
        """
        if self.secretary_active:
            return {"ok": True, "message": "Secretary 已激活", "already_active": True}

        from .project import ProjectManager
        from .orchestrator import Orchestrator
        from .model_router import ModelRouter
        from .mcp_gateway import MCPGateway

        # 项目管理器
        self.project_manager = ProjectManager(self.db)

        # 模型路由器
        model_pool = load_model_pool()
        self.model_router = ModelRouter(model_pool.models, self.project_manager) if model_pool.models else None
        if self.model_router:
            print(f"[Station] 模型路由器已加载: {self.model_router.pool_size} 个模型")

        # 任务编排器
        self.orchestrator = Orchestrator(
            self.db, self.project_manager, self.model_router,
            skill_registry=self.skill_registry,
        )

        # MCP 工具网关
        self.mcp_gateway = MCPGateway()

        self.secretary_active = True
        print("[Station] Secretary 模式已激活 — 项目管理/任务分发/模型路由/MCP工具 已就绪")

        return {
            "ok": True,
            "message": "Secretary 已激活",
            "models": self.model_router.pool_size if self.model_router else 0,
        }

    def deactivate_secretary(self) -> dict:
        """停用 Secretary 模式, 回到纯 Station 模式。"""
        if not self.secretary_active:
            return {"ok": True, "message": "Secretary 未激活"}

        self.secretary_active = False
        self.project_manager = None
        self.orchestrator = None
        self.model_router = None
        self.mcp_gateway = None

        print("[Station] Secretary 模式已停用 — 回到纯基础设施管理")
        return {"ok": True, "message": "Secretary 已停用"}

    # ── 主机信息采集 ───────────────────────────────────────────────

    def _collect_info(self) -> HostInfo:
        """采集 Station Director 自身的主机信息。"""
        role = "secretary" if self.secretary_active else "station"
        return collect_host_info(
            device_id=self.state.device_id,
            device_name=self.state.device_name,
            role=role,
            api_port=self.state.api_port,
            shared_folder=str(self.state.shared_folder.path),
            shared_file_count=self.state.shared_folder.file_count(),
            start_time=self.state.start_time,
        )

    def _make_packet(self) -> DiscoveryPacket:
        """生成 Station 的 UDP 发现包。"""
        info = self._collect_info()
        packet = make_discovery_packet(info)
        packet.role = "station"
        return packet

    def _on_device_seen(self, packet: DiscoveryPacket, ip: str):
        """UDP 发现到新设备时的回调。"""
        if packet.role == "worker" and packet.device_id:
            pass  # Worker 会通过 HTTP 主动注册

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
            print(f"[Station] 配置报告刷新异常: {e}")

    def _config_refresh_loop(self):
        """定期刷新共享文件夹中的配置报告 + 自身心跳。"""
        while self._running:
            time.sleep(HEARTBEAT_INTERVAL_SECS)
            self._refresh_host_config()
            # 自身心跳: 保持 Station Director 在线状态
            try:
                info = self._collect_info()
                self.station_director.on_heartbeat(self.state.device_id, {
                    "cpu_percent": info.cpu_percent,
                    "memory_percent": info.memory_percent,
                    "disk_percent": info.disk_percent,
                    "shared_file_count": info.shared_file_count,
                })
            except Exception:
                pass

    def _prune_loop(self):
        """定期清理超时离线主机。"""
        while self._running:
            time.sleep(PRUNE_INTERVAL_SECS)
            try:
                self.station_director.prune_offline(self.cfg.discovery.device_ttl)
            except Exception as e:
                print(f"[Station] 清理离线主机异常: {e}")

    async def _ws_push_loop(self):
        """定期向 WebSocket 客户端推送最新主机状态。"""
        while self._running:
            await asyncio.sleep(3)
            try:
                hosts = self.db.list_hosts()
                from .station_api import _broadcast
                await _broadcast(self.state, "hosts", [h.to_dict() for h in hosts])
            except Exception:
                pass

    # ── FastAPI 应用 ───────────────────────────────────────────────

    def _create_app(self) -> FastAPI:
        """创建 Station Director 的 FastAPI 应用。

        包含全部路由:
        - 基础层 (始终可用): 主机注册/心跳/查询/Station API/角色激活
        - Secretary 层 (激活后可用): 任务/Agent/项目/MCP工具/模型路由
        """
        app = FastAPI(title="LAN Mesh Station Director", version="0.1.0")

        # Station 路由 (含全部 API, Secretary 路由会检查 active 状态)
        station_router = create_station_router(self)
        app.include_router(station_router)

        # Worker 路由 (本机也可被其他节点查询)
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
            return HTMLResponse(content="<h1>LAN Mesh Station Director</h1><p>dashboard.html 未找到</p>")

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

    # ── 生命周期 ───────────────────────────────────────────────────

    def start(self):
        """启动 Station Director。"""
        self._running = True

        # 启动前自检 (复用 secretary 自检: 含 DB 路径 + Web 模板检查)
        from .preflight import run_preflight
        if not run_preflight("secretary", self.cfg):
            print("[Station] 自检未通过,启动中止。请根据上述提示修复后重试。")
            sys.exit(1)

        self.state.api_port = self._find_available_port(self.cfg.secretary.api_port)

        print(f"[Station] 设备 ID: {self.state.device_id}")
        print(f"[Station] 设备名称: {self.state.device_name}")
        print(f"[Station] 共享目录: {self.state.shared_folder.path}")
        print(f"[Station] 数据库: {get_db_path(self.cfg)}")
        print(f"[Station] HTTP API + Web UI 端口: {self.state.api_port}")
        print(f"[Station] Secretary 模式: 未激活 (请在 Web UI 中激活)")

        # 启动 UDP 发现服务
        self.discovery = DiscoveryService(
            device_id=self.state.device_id,
            device_name=self.state.device_name,
            role="station",
            api_port=self.state.api_port,
            make_packet=self._make_packet,
            on_device_seen=self._on_device_seen,
            discovery_port=self.cfg.discovery.port,
            presence_interval=self.cfg.discovery.presence_interval,
            device_ttl=self.cfg.discovery.device_ttl,
        )
        self.station_director.bind_discovery(self.discovery)
        self.discovery.start()

        # 自注册为主机 (Station Director 本身也是一台主机)
        self_info = self._collect_info()
        self.station_director.on_host_registered(self_info)

        # 部署采集脚本并生成初始配置报告
        self._deploy_config_script()
        self._refresh_host_config()
        print(f"[Station] 配置报告已生成: {self.state.shared_folder.path}/host_config.json")

        # 启动配置刷新线程
        config_thread = threading.Thread(
            target=self._config_refresh_loop, name="station-config-refresh", daemon=True
        )
        config_thread.start()
        self._threads.append(config_thread)

        # 启动离线清理线程
        prune_thread = threading.Thread(
            target=self._prune_loop, name="station-prune", daemon=True
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
        print(f"\n[Station] 服务已启动!")
        print(f"  Web UI:  http://localhost:{self.state.api_port}")
        for ip in local_ips:
            print(f"  局域网:  http://{ip}:{self.state.api_port}")
        print(f"\n[Station] 等待 Worker 节点注册...")
        print(f"[Station] 在 Web UI 中点击「启动秘书」激活 Secretary 模式\n")

        try:
            server.run()
        except KeyboardInterrupt:
            print("\n[Station] 正在停止...")
            self.stop()

    def stop(self):
        """停止 Station Director。"""
        self._running = False
        if self.discovery:
            self.discovery.stop()
