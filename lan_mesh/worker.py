"""
Worker Agent - 部署在各主机上的守护进程

职责:
1. 自动采集本机配置 (CPU/内存/磁盘/OS/网络)
2. 自动创建并暴露共享文件夹
3. UDP 广播发现 Master 节点
4. 通过 HTTP 向 Master 注册并发送心跳
5. 提供 HTTP API 供 Master 查询与文件下载

启动流程:
  生成 device_id → 采集 host_info → 创建 shared_folder
  → 启动 FastAPI → 启动 UDP 发现 → 发现 Master 后注册 → 心跳循环
"""
import shutil
import socket
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


@dataclass
class WorkerState:
    """Worker 运行时共享状态。"""
    device_id: str = ""
    device_name: str = ""
    role: str = "worker"
    api_port: int = 0
    start_time: float = field(default_factory=time.time)
    shared_folder: SharedFolderManager = None
    master_ip: Optional[str] = None
    master_port: Optional[int] = None
    agent_card: dict = None          # Agent Card 快照
    agent_runtime: AgentRuntime = None


class WorkerAgent:
    """Worker 守护进程。

    在各主机上部署后自动:
    - 创建共享文件夹
    - 采集主机配置
    - 通过 UDP 发现 Master
    - 通过 HTTP 注册并维持心跳
    - 暴露 HTTP API 供 Master 查询
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

        如果对方是 Master,记录其地址并尝试注册。
        """
        if packet.role == "master":
            self.state.master_ip = ip
            self.state.master_port = packet.api_port

    # ── HTTP 注册与心跳 ─────────────────────────────────────────

    def _register_with_master(self) -> bool:
        """向 Master 发送 HTTP 注册请求 (主机信息 + Agent Card)。"""
        if not self.state.master_ip or not self.state.master_port:
            return False

        try:
            info = self._collect_info()
            # 1. 注册主机信息
            resp = requests.post(
                f"http://{self.state.master_ip}:{self.state.master_port}/api/register",
                json=info.to_dict(),
                timeout=5,
            )
            if resp.status_code == 200:
                print(f"[Worker] 主机信息已注册到 Master {self.state.master_ip}:{self.state.master_port}")
                # 2. 注册 Agent Card
                self._register_agent_card()
                return True
        except requests.RequestException as e:
            print(f"[Worker] 注册失败: {e}")
        return False

    def _register_agent_card(self):
        """向 Master 注册 Agent Card (能力声明)。"""
        if not self.state.master_ip or not self.state.master_port:
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
                f"http://{self.state.master_ip}:{self.state.master_port}/api/agents/register",
                json=card.to_dict(),
                timeout=5,
            )
            if resp.status_code == 200:
                print(f"[Worker] Agent Card 已注册: {len(card.skills)} 技能, {len(card.tools)} 工具")
        except requests.RequestException as e:
            print(f"[Worker] Agent Card 注册失败: {e}")

    def _send_heartbeat(self) -> bool:
        """向 Master 发送心跳 (携带实时资源使用率)。"""
        if not self.state.master_ip or not self.state.master_port:
            return False

        try:
            info = self._collect_info()
            # 同步更新共享文件夹中的配置报告
            self.state.shared_folder.write_host_config(info)
            resp = requests.post(
                f"http://{self.state.master_ip}:{self.state.master_port}/api/heartbeat",
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
        """心跳循环 - 定期向 Master 发送状态更新 + 刷新共享配置。"""
        registered = False
        while self._running:
            if not registered:
                registered = self._register_with_master()
                time.sleep(HEARTBEAT_INTERVAL_SECS)
                continue

            if not self._send_heartbeat():
                print("[Worker] 心跳失败,尝试重新注册...")
                registered = False
            time.sleep(HEARTBEAT_INTERVAL_SECS)

    # ── FastAPI 应用 ─────────────────────────────────────────────

    def _create_app(self) -> FastAPI:
        """创建 Worker 的 FastAPI 应用。"""
        app = FastAPI(title="LAN Mesh Worker", version="0.1.0")
        router = create_worker_router(
            collect_info_fn=self._collect_info,
            shared_folder=self.state.shared_folder,
            agent_runtime=self.state.agent_runtime,
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
        self._running = True

        # 启动前自检
        from .preflight import run_preflight
        if not run_preflight("worker", self.cfg):
            print("[Worker] 自检未通过,启动中止。请根据上述提示修复后重试。")
            sys.exit(1)

        print(f"[Worker] 设备 ID: {self.state.device_id}")
        print(f"[Worker] 设备名称: {self.state.device_name}")
        print(f"[Worker] 共享目录: {self.state.shared_folder.path}")

        # 查找可用端口
        self.state.api_port = self._find_available_port(self.cfg.worker.api_port)
        print(f"[Worker] HTTP API 端口: {self.state.api_port}")

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

        # 部署采集脚本并生成初始配置报告
        self._deploy_config_script()
        initial_info = self._collect_info()
        self.state.shared_folder.write_host_config(initial_info)
        print(f"[Worker] 配置报告已生成: {self.state.shared_folder.path}/host_config.json")

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

        # 启动 FastAPI (阻塞主线程)
        app = self._create_app()
        config = uvicorn.Config(
            app,
            host="0.0.0.0",
            port=self.state.api_port,
            log_level="warning",
        )
        server = uvicorn.Server(config)
        print(f"[Worker] 服务已启动 → http://0.0.0.0:{self.state.api_port}")
        try:
            server.run()
        except KeyboardInterrupt:
            print("\n[Worker] 正在停止...")
            self.stop()

    def stop(self):
        """停止 Worker。"""
        self._running = False
        if self.discovery:
            self.discovery.stop()
