"""
Worker Agent - 部署在各主机上的守护进程

职责:
1. 自动采集本机配置 (CPU/内存/磁盘/OS/网络)
2. 自动创建并暴露共享文件夹
3. UDP 广播发现 Secretary 节点
4. 通过 HTTP 向 Secretary 注册并发送心跳
5. 提供 HTTP API 供 Secretary 查询与文件下载

启动流程:
  生成 device_id → 采集 host_info → 创建 shared_folder
  → 启动 FastAPI → 启动 UDP 发现 → 发现 Secretary 后注册 → 心跳循环
"""
import os
from .application_lifecycle import wait_for_stop
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import requests
import uvicorn
from fastapi import FastAPI

from .config import AppConfig
from .discovery import DiscoveryService
from .host_info import (
    collect_host_info,
    load_or_create_device_id,
    make_discovery_packet,
)
from .protocol import (
    DiscoveryPacket,
    HEARTBEAT_INTERVAL_SECS,
    HostInfo,
    PRESENCE_INTERVAL_SECS,
)
from .shared_folder import SharedFolderManager
from .api import create_worker_router
from .agent_card import generate_agent_card
from .agent_runtime import AgentRuntime
from .pm_agent import ProjectManagerAgent
from .cloud_sync import CloudSyncManager
from .logger import get_logger

logger = get_logger("worker")


@dataclass
class WorkerState:
    """Worker 运行时共享状态。"""
    device_id: str = ""
    device_name: str = ""
    role: str = "worker"
    api_port: int = 0
    start_time: float = field(default_factory=time.time)
    shared_folder: SharedFolderManager = None
    secretary_ip: Optional[str] = None
    secretary_port: Optional[int] = None
    mesh_token: str = ""             # Phase 0: Secretary 注册时下发的节点认证 token
    agent_card: dict = None          # Agent Card 快照
    agent_runtime: AgentRuntime = None
    pm_agent: ProjectManagerAgent = None  # 内嵌 PM Agent
    sub_agents: dict = field(default_factory=dict)  # PM 创建的子 Agent Runtime 实例
    cloud_sync: CloudSyncManager = None  # 云存储同步管理器


from typing import Any

class WorkerAgent:
    """Worker 守护进程。

    在各主机上部署后自动:
    - 创建共享文件夹
    - 采集主机配置
    - 通过 UDP 发现 Secretary
    - 通过 HTTP 注册并维持心跳
    - 暴露 HTTP API 供 Secretary 查询
    """

    def __init__(self, cfg: AppConfig):
        self.cfg = cfg
        self.state = WorkerState(role="worker")

        # 数据目录
        self.data_dir = Path.home() / ".lan_mesh"
        self.data_dir.mkdir(parents=True, exist_ok=True)

        # 设备身份
        self.state.device_id = load_or_create_device_id(self.data_dir, role="worker")
        self.state.device_name = (
            cfg.worker.device_name
            or socket.gethostname()
            or f"worker-{self.state.device_id[:8]}"
        )

        # 共享文件夹
        shared_path = cfg.worker.shared_folder
        self.state.shared_folder = SharedFolderManager(shared_path)

        # 发现服务
        self.discovery: Optional[DiscoveryService] = None
        self._running = False
        self._threads: list[threading.Thread] = []

        # Secretary 子进程管理 (远程角色分配)
        self._secretary_process: Optional[subprocess.Popen] = None
        self._secretary_port: Optional[int] = None

    def _collect_info(self) -> HostInfo:
        """采集当前主机信息快照。"""
        return collect_host_info(
            device_id=self.state.device_id,
            device_name=self.state.device_name,
            role="worker",
            api_port=self.state.api_port,
            shared_folder=str(self.state.shared_folder.path),
            shared_file_count=self.state.shared_folder.file_count(),
            start_time=self.state.start_time,
        )

    def _make_packet(self) -> DiscoveryPacket:
        """生成 UDP 发现包。"""
        info = self._collect_info()
        return make_discovery_packet(info)

    def _on_device_seen(self, packet: DiscoveryPacket, ip: str):
        """UDP 发现到新设备时的回调。

        如果对方是 Secretary 或 Station Director, 记录其地址并尝试注册。
        """
        if packet.role in ("secretary", "station"):
            self.state.secretary_ip = ip
            self.state.secretary_port = packet.api_port

    # ── HTTP 注册与心跳 ─────────────────────────────────────────

    def _register_with_secretary(self) -> bool:
        """向 Secretary 发送 HTTP 注册请求 (主机信息 + Agent Card)。"""
        if not self.state.secretary_ip or not self.state.secretary_port:
            return False

        try:
            info = self._collect_info()
            # 1. 注册主机信息
            resp = requests.post(
                f"http://{self.state.secretary_ip}:{self.state.secretary_port}/api/register",
                json=info.to_dict(),
                timeout=5,
            )
            if resp.status_code == 200:
                # Phase 0: 保存 Secretary 下发的 mesh token (认证启用时)
                body = resp.json()
                issued = body.get("mesh_token", "")
                if issued:
                    self.state.mesh_token = issued
                    from .http_retry import set_auth_token
                    set_auth_token(issued)
                    # S1: 持久化信任根 (重启后仍可解密 Secretary 推送的密钥)
                    from .auth import save_mesh_token
                    save_mesh_token(issued)
                    logger.info("已接收 Secretary 下发的 mesh token")
                logger.info("主机信息已注册到 Secretary %s:%s", self.state.secretary_ip, self.state.secretary_port)
                # 2. 注册 Agent Card
                self._register_agent_card()
                # 3. 拉取已授权技能到本地缓存
                self._pull_skills()
                # 4. R3/M5-2: 注入资源用量上报目标 (记账汇总到 Secretary,
                #    携带 mesh_token 启用 WS 直推通道)
                from .model_resources import set_report_target_global
                set_report_target_global(
                    f"http://{self.state.secretary_ip}:{self.state.secretary_port}",
                    token=self.state.mesh_token)
                return True
        except requests.RequestException as e:
            logger.error("注册失败: %s", e)
        return False

    def _register_agent_card(self):
        """向 Secretary 注册 Agent Card (能力声明)。"""
        if not self.state.secretary_ip or not self.state.secretary_port:
            return
        try:
            ips = self._collect_info().ip_addresses
            card = generate_agent_card(
                device_id=self.state.device_id,
                agent_name=self.state.device_name,
                ip=ips[0] if ips else "",
                api_port=self.state.api_port,
                hostname=self.state.device_name,
            )
            self.state.agent_card = card.to_dict()
            resp = requests.post(
                f"http://{self.state.secretary_ip}:{self.state.secretary_port}/api/agents/register",
                json=card.to_dict(),
                timeout=5,
            )
            if resp.status_code == 200:
                logger.info("Agent Card 已注册: %d 技能, %d 工具", len(card.skills), len(card.tools))
        except requests.RequestException as e:
            logger.error("Agent Card 注册失败: %s", e)

    def _pull_skills(self):
        """向 Station Director 拉取已授权技能并缓存到本地。"""
        if not self.state.secretary_ip or not self.state.secretary_port:
            return
        try:
            resp = requests.get(
                f"http://{self.state.secretary_ip}:{self.state.secretary_port}/api/station/skills/download",
                params={"role": "worker", "agent_id": self.state.device_id},
                timeout=10,
            )
            if resp.status_code == 200:
                skills = resp.json()
                cache_dir = Path.home() / ".lan_mesh" / "skills_cache"
                cache_dir.mkdir(parents=True, exist_ok=True)
                for skill in skills:
                    skill_dir = cache_dir / skill.get("skill_id", "unknown")
                    skill_dir.mkdir(parents=True, exist_ok=True)
                    (skill_dir / "SKILL.md").write_text(
                        skill.get("content", ""), encoding="utf-8"
                    )
                    if skill.get("reference"):
                        (skill_dir / "reference.md").write_text(
                            skill["reference"], encoding="utf-8"
                        )
                    from .skill_knowledge import RESOURCE_NAMES
                    for name, content in skill.get("resources", {}).items():
                        if name in RESOURCE_NAMES:
                            (skill_dir / name).write_text(content, encoding="utf-8")
                logger.info("已拉取 %d 个技能到本地缓存", len(skills))
        except Exception as e:
            logger.error("技能拉取失败: %s", e)

    def _send_heartbeat(self) -> bool:
        """向 Secretary 发送心跳 (携带实时资源使用率)。"""
        if not self.state.secretary_ip or not self.state.secretary_port:
            return False

        try:
            info = self._collect_info()
            # 同步更新共享文件夹中的配置报告
            self.state.shared_folder.write_host_config(info)
            resp = requests.post(
                f"http://{self.state.secretary_ip}:{self.state.secretary_port}/api/heartbeat",
                json={
                    "device_id": self.state.device_id,
                    "cpu_percent": info.cpu_percent,
                    "memory_percent": info.memory_percent,
                    "disk_percent": info.disk_percent,
                    "shared_file_count": info.shared_file_count,
                },
                timeout=5,
            )
            return resp.status_code == 200
        except requests.RequestException:
            return False

    def _deploy_config_script(self):
        """将独立采集脚本部署到共享文件夹,供其他主机使用。"""
        script_src = Path(__file__).parent / "collect_config.py"
        script_dest = self.state.shared_folder.path / "collect_config.py"
        if script_src.is_file() and not script_dest.exists():
            shutil.copy2(script_src, script_dest)

    def _heartbeat_loop(self):
        """心跳循环 - 定期向 Secretary 发送状态更新 + 刷新共享配置。"""
        registered = False
        while self._running:
            if not registered:
                registered = self._register_with_secretary()
                if wait_for_stop(self, HEARTBEAT_INTERVAL_SECS):
                    return
                continue

            if not self._send_heartbeat():
                logger.warning("心跳失败, 尝试重新注册...")
                registered = False
            if wait_for_stop(self, HEARTBEAT_INTERVAL_SECS):
                return

    # ── Secretary 子进程管理 (远程角色分配) ─────────────────────────

    def start_secretary(self, port: int=None) -> dict:
        return call_component(self, WorkerSecretaryService, 'start_secretary', port=port)

    def stop_secretary(self) -> dict:
        return call_component(self, WorkerSecretaryService, 'stop_secretary')

    def get_secretary_status(self) -> dict:
        return call_component(self, WorkerSecretaryService, 'get_secretary_status')

    # ── PM Agent 管理 (内嵌模块) ───────────────────────────────────

    def start_pm(self, task_id: str, secretary_url: str, task_data: dict=None) -> dict:
        return call_component(self, WorkerPmService, 'start_pm', task_id=task_id, secretary_url=secretary_url, task_data=task_data)

    def stop_pm(self, expected_pm_id: str="") -> dict:
        return call_component(self, WorkerPmService, 'stop_pm', expected_pm_id=expected_pm_id)

    def cancel_pm(self, expected_pm_id: str="") -> dict:
        return call_component(self, WorkerPmService, 'cancel_pm', expected_pm_id=expected_pm_id)

    def pause_pm(self, expected_pm_id: str="") -> dict:
        return call_component(self, WorkerPmService, 'pause_pm', expected_pm_id=expected_pm_id)

    def inject_pm_input(self, input_data: dict, expected_pm_id: str="") -> dict:
        return call_component(self, WorkerPmService, 'inject_pm_input', input_data=input_data, expected_pm_id=expected_pm_id)

    def get_pm_status(self) -> dict:
        return call_component(self, WorkerPmService, 'get_pm_status')

    def create_subagent(self, agent_name: str, skills: list, task_description: str='', system_prompt: str='', preferred_agent_id: str='') -> dict:
        return call_component(self, WorkerSubagentService, 'create_subagent', agent_name=agent_name, skills=skills, task_description=task_description, system_prompt=system_prompt, preferred_agent_id=preferred_agent_id)

    def cancel_subagent(self, agent_id: str) -> dict:
        return call_component(self, WorkerSubagentService, 'cancel_subagent', agent_id=agent_id)

    def forward_progress_report(self, report: dict) -> dict:
        return call_component(self, WorkerPmService, 'forward_progress_report', report=report)

    def get_subagent_status(self) -> dict:
        return call_component(self, WorkerSubagentService, 'get_subagent_status')

    def update_subagent_prompt(self, agent_id: str, new_prompt: str) -> dict:
        return call_component(self, WorkerSubagentService, 'update_subagent_prompt', agent_id=agent_id, new_prompt=new_prompt)

    # ── FastAPI 应用 ─────────────────────────────────────────────

    def _create_app(self) -> FastAPI:
        """创建 Worker 的 FastAPI 应用。"""
        from .version import __version__
        app = FastAPI(title="LAN Mesh Worker", version=__version__)

        @app.on_event("shutdown")
        async def shutdown_event() -> None:
            from starlette.concurrency import run_in_threadpool
            await run_in_threadpool(self.stop)

        # Phase 0: 节点间 mesh token 认证 (与 Station 同一中间件, auth_enabled 时启用)
        from .station_api import api_guard_middleware, configure_mesh_auth, mesh_auth_enabled
        if mesh_auth_enabled():
            configure_mesh_auth(True, self.state.mesh_token)
        app.middleware("http")(api_guard_middleware)

        router = create_worker_router(
            collect_info_fn=self._collect_info,
            shared_folder=self.state.shared_folder,
            agent_runtime=self.state.agent_runtime,
            role_manager=self,
        )
        app.include_router(router)

        @app.get("/")
        async def root():
            return {
                "service": "lan-mesh-worker",
                "device_id": self.state.device_id,
                "device_name": self.state.device_name,
                "status": "running",
            }

        return app

    def _find_available_port(self, start: int, count: int = 20) -> int:
        """查找可用端口 (参考 QuickLAN 的端口递增策略)。"""
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
        """启动 Worker 守护进程。"""
        from .application_lifecycle import stop_signal
        stop_signal(self).clear()
        vars(self).pop("_shutdown_report", None)
        self._services_stopping = False
        self._running = True

        # 启动前自检
        from .preflight import run_preflight
        if not run_preflight("worker", self.cfg):
            logger.critical("自检未通过, 启动中止。请根据上述提示修复后重试。")
            sys.exit(1)

        logger.info("设备 ID: %s", self.state.device_id)
        logger.info("设备名称: %s", self.state.device_name)
        logger.info("共享目录: %s", self.state.shared_folder.path)

        # 查找可用端口
        self.state.api_port = self._find_available_port(self.cfg.worker.api_port)
        logger.info("HTTP API 端口: %d", self.state.api_port)

        # 启动 UDP 发现服务
        self.discovery = DiscoveryService(
            device_id=self.state.device_id,
            device_name=self.state.device_name,
            role="worker",
            api_port=self.state.api_port,
            make_packet=self._make_packet,
            on_device_seen=self._on_device_seen,
            discovery_port=self.cfg.discovery.port,
            presence_interval=self.cfg.discovery.presence_interval,
            device_ttl=self.cfg.discovery.device_ttl,
        )
        self.discovery.start()

        # 云存储同步 (如果启用)
        cloud_cfg = self.cfg.cloud_storage
        if cloud_cfg.enabled and cloud_cfg.endpoint:
            self.state.cloud_sync = CloudSyncManager(
                local_path=str(self.state.shared_folder.path),
                device_id=self.state.device_id,
                endpoint=cloud_cfg.endpoint,
                bucket=cloud_cfg.bucket,
                prefix=cloud_cfg.prefix,
                access_key=cloud_cfg.access_key,
                secret_key=cloud_cfg.secret_key,
                region=cloud_cfg.region,
                secure=cloud_cfg.secure,
                auto_sync=cloud_cfg.auto_sync,
                sync_interval=cloud_cfg.sync_interval,
            )
            self.state.cloud_sync.start_auto_sync()
            logger.info("云存储同步已启动: %s/%s", cloud_cfg.endpoint, cloud_cfg.bucket)

        # 部署采集脚本并生成初始配置报告
        self._deploy_config_script()
        initial_info = self._collect_info()
        self.state.shared_folder.write_host_config(initial_info)
        logger.info("配置报告已生成: %s/host_config.json", self.state.shared_folder.path)

        # 创建 Agent 运行时
        self.state.agent_runtime = AgentRuntime(
            agent_id=self.state.device_id,
            shared_folder_path=str(self.state.shared_folder.path),
        )

        # 启动心跳线程
        hb_thread = threading.Thread(
            target=self._heartbeat_loop, name="worker-heartbeat", daemon=True
        )
        hb_thread.start()
        self._threads.append(hb_thread)

        self._serve()

    def _serve(self) -> None:
        """Own the Uvicorn server and always clean up after it exits."""
        # 启动 FastAPI (阻塞主线程)
        app = self._create_app()
        config = uvicorn.Config(
            app,
            host="0.0.0.0",
            port=self.state.api_port,
            log_level="warning",
        )
        server = uvicorn.Server(config)
        self._server = server
        logger.info("服务已启动 → http://0.0.0.0:%d", self.state.api_port)
        try:
            server.run()
        finally:
            self.stop()

    def stop(self) -> dict:
        """Cooperatively stop owned work and release service containers."""
        from .application_lifecycle import shutdown_application
        return shutdown_application(self)


from typing import Any
from .component_service import call_component
from .worker_secretary_service import WorkerSecretaryService
from .worker_pm_service import WorkerPmService
from .worker_subagent_service import WorkerSubagentService
