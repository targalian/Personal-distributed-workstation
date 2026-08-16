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
                time.sleep(HEARTBEAT_INTERVAL_SECS)
                continue

            if not self._send_heartbeat():
                logger.warning("心跳失败, 尝试重新注册...")
                registered = False
            time.sleep(HEARTBEAT_INTERVAL_SECS)

    # ── Secretary 子进程管理 (远程角色分配) ─────────────────────────

    def start_secretary(self, port: int = None) -> dict:
        """在本机启动 Secretary 子进程。

        Args:
            port: Secretary HTTP 端口 (默认自动查找)

        Returns:
            {ok, pid, port} 或 {ok: False, message}
        """
        if self._secretary_process and self._secretary_process.poll() is None:
            return {"ok": False, "message": "Secretary 已在运行", "port": self._secretary_port}

        if port is None:
            port = self._find_available_port(45470)

        python_exe = sys.executable
        project_root = str(Path(__file__).parent.parent)
        cmd = [python_exe, "main.py", "secretary", "--port", str(port)]

        try:
            self._secretary_process = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                cwd=project_root,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            self._secretary_port = port
            logger.info("Secretary 子进程已启动: PID=%d, port=%d", self._secretary_process.pid, port)
            return {"ok": True, "pid": self._secretary_process.pid, "port": port}
        except Exception as e:
            logger.error("启动 Secretary 失败: %s", e)
            return {"ok": False, "message": str(e)}

    def stop_secretary(self) -> dict:
        """停止本机的 Secretary 子进程。"""
        if not self._secretary_process or self._secretary_process.poll() is not None:
            self._secretary_process = None
            self._secretary_port = None
            return {"ok": False, "message": "Secretary 未在运行"}

        try:
            self._secretary_process.terminate()
            self._secretary_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._secretary_process.kill()
            self._secretary_process.wait(timeout=3)
        except Exception:
            pass

        logger.info("Secretary 子进程已停止")
        self._secretary_process = None
        self._secretary_port = None
        return {"ok": True, "message": "Secretary 已停止"}

    def get_secretary_status(self) -> dict:
        """查询本机 Secretary 运行状态。"""
        if self._secretary_process and self._secretary_process.poll() is None:
            return {"running": True, "pid": self._secretary_process.pid, "port": self._secretary_port}
        return {"running": False}

    # ── PM Agent 管理 (内嵌模块) ───────────────────────────────────

    def start_pm(self, task_id: str, secretary_url: str, task_data: dict = None) -> dict:
        """在本 Worker 上启动 PM Agent 接管指定任务。

        Args:
            task_id: 任务 ID
            secretary_url: Secretary API 地址 (用于上报状态/进度)
            task_data: 任务详情 (可选, 由 Secretary 传入)

        Returns:
            {ok, pm_id} 或 {ok: False, message}
        """
        if self.state.pm_agent and self.state.pm_agent._running:
            return {"ok": False, "message": "PM Agent 已在运行"}

        if not self.state.agent_runtime:
            return {"ok": False, "message": "Agent 运行时未初始化"}

        import uuid as _uuid
        pm_id = f"pm-{_uuid.uuid4().hex[:12]}"

        # 如果未提供 task_data, 从 Secretary 获取
        if not task_data:
            try:
                import requests as _req
                resp = _req.get(f"{secretary_url.rstrip('/')}/api/tasks/{task_id}", timeout=5)
                if resp.status_code == 200:
                    task_data = resp.json()
            except Exception as e:
                return {"ok": False, "message": f"获取任务详情失败: {e}"}

        if not task_data:
            return {"ok": False, "message": "无法获取任务详情"}

        # 创建 PM Agent 实例
        self.state.pm_agent = ProjectManagerAgent(
            pm_id=pm_id,
            agent_runtime=self.state.agent_runtime,
            secretary_url=secretary_url,
            device_id=self.state.device_id,
            device_name=self.state.device_name,
        )

        # 启动任务 (异步)
        self.state.pm_agent.start_task(task_data)
        logger.info("PM Agent 已启动: %s, 任务: %s", pm_id, task_id)

        return {"ok": True, "pm_id": pm_id, "device_id": self.state.device_id}

    def stop_pm(self) -> dict:
        """停止本 Worker 上的 PM Agent。"""
        if not self.state.pm_agent:
            return {"ok": False, "message": "PM Agent 未运行"}

        self.state.pm_agent.cancel()
        pm_id = self.state.pm_agent.pm_id
        self.state.pm_agent = None
        # 清理子 Agent
        self.state.sub_agents.clear()
        logger.info("PM Agent 已停止: %s", pm_id)
        return {"ok": True, "pm_id": pm_id}

    def cancel_pm(self) -> dict:
        """取消 PM Agent (保留实例, 标记取消)。"""
        if not self.state.pm_agent:
            return {"ok": False, "message": "PM Agent 未运行"}
        self.state.pm_agent.cancel()
        return {"ok": True, "pm_id": self.state.pm_agent.pm_id}

    def pause_pm(self) -> dict:
        """暂停 PM Agent。"""
        if not self.state.pm_agent:
            return {"ok": False, "message": "PM Agent 未运行"}
        self.state.pm_agent.pause()
        return {"ok": True, "pm_id": self.state.pm_agent.pm_id}

    def inject_pm_input(self, input_data: dict) -> dict:
        """向 PM Agent 注入来自 Secretary/Boss 的回复输入。

        用于反向沟通通道: Boss 回复 PM 的澄清请求后,
        Secretary 通过此方法将回复注入 PM。

        Args:
            input_data: Boss 回复数据

        Returns:
            {ok: True} 或 {ok: False, message}
        """
        if not self.state.pm_agent:
            return {"ok": False, "message": "PM Agent 未运行"}
        if not self.state.pm_agent._running:
            return {"ok": False, "message": "PM Agent 已停止"}
        self.state.pm_agent.receive_input(input_data)
        return {"ok": True, "pm_id": self.state.pm_agent.pm_id}

    def get_pm_status(self) -> dict:
        """查询本 Worker 上的 PM Agent 运行状态。"""
        if not self.state.pm_agent:
            return {"running": False}
        return self.state.pm_agent.get_status()

    def create_subagent(self, agent_name: str, skills: list, task_description: str = "",
                        system_prompt: str = "", preferred_agent_id: str = "") -> dict:
        """在本 Worker 上为 PM 创建子 Agent (新 AgentRuntime 实例)。

        Args:
            agent_name: 子 Agent 名称
            skills: 技能列表
            task_description: 任务描述
            system_prompt: PM 生成的定制 system prompt (角色/团队/依赖/质量要求)
            preferred_agent_id: PM 预先生成的 agent_id (用于 prompt 中引用)

        Returns:
            {agent_id, agent_name} 或 {ok: False, message}
        """
        import uuid as _uuid
        # 优先使用 PM 预先生成的 agent_id (已在 prompt 中引用)
        agent_id = preferred_agent_id or f"sub-{_uuid.uuid4().hex[:10]}"

        # 创建新的 AgentRuntime 实例, 注入 PM 定制的 system prompt
        sub_runtime = AgentRuntime(
            agent_id=agent_id,
            shared_folder_path=str(self.state.shared_folder.path),
            custom_system_prompt=system_prompt,
        )

        self.state.sub_agents[agent_id] = {
            "agent_id": agent_id,
            "agent_name": agent_name,
            "runtime": sub_runtime,
            "skills": skills,
            "current_task": task_description,
            "status": "idle",
            "progress": 0.0,
            "has_custom_prompt": bool(system_prompt),
        }

        logger.info("子 Agent 已创建: %s (%s)", agent_id, agent_name)
        return {"agent_id": agent_id, "agent_name": agent_name}

    def forward_progress_report(self, report: dict) -> dict:
        """将子 Agent 的进度报告转发给 PM Agent。

        Args:
            report: 进度报告字典

        Returns:
            {ok: True} 或 {ok: False, message}
        """
        if not self.state.pm_agent:
            return {"ok": False, "message": "PM Agent 未运行"}

        self.state.pm_agent.receive_progress_report(report)
        return {"ok": True}

    def get_subagent_status(self) -> dict:
        """返回本 Worker 上所有子 Agent 的状态。"""
        result = []
        for agent_id, info in self.state.sub_agents.items():
            result.append({
                "agent_id": agent_id,
                "agent_name": info.get("agent_name", ""),
                "current_task": info.get("current_task", ""),
                "status": info.get("status", "idle"),
                "progress": info.get("progress", 0.0),
            })
        return {"sub_agents": result, "total": len(result)}

    def update_subagent_prompt(self, agent_id: str, new_prompt: str) -> dict:
        """动态更新子 Agent 的 system prompt (优化2)。

        PM 可在任务执行中途调用此方法, 更新子 Agent 的定制 prompt,
        用于纠偏、补充上下文、调整策略。

        Args:
            agent_id: 子 Agent ID
            new_prompt: 新的 system prompt

        Returns:
            {ok: True} 或 {ok: False, message}
        """
        info = self.state.sub_agents.get(agent_id)
        if not info:
            return {"ok": False, "message": f"子 Agent {agent_id} 不存在"}
        runtime = info.get("runtime")
        if not runtime:
            return {"ok": False, "message": "子 Agent runtime 不可用"}
        runtime.set_custom_prompt(new_prompt)
        info["has_custom_prompt"] = bool(new_prompt)
        logger.info("子 Agent %s prompt 已更新 (%d 字符)", agent_id, len(new_prompt))
        return {"ok": True, "agent_id": agent_id}

    # ── FastAPI 应用 ─────────────────────────────────────────────

    def _create_app(self) -> FastAPI:
        """创建 Worker 的 FastAPI 应用。"""
        app = FastAPI(title="LAN Mesh Worker", version="0.1.0")

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

        # 启动 FastAPI (阻塞主线程)
        app = self._create_app()
        config = uvicorn.Config(
            app,
            host="0.0.0.0",
            port=self.state.api_port,
            log_level="warning",
        )
        server = uvicorn.Server(config)
        logger.info("服务已启动 → http://0.0.0.0:%d", self.state.api_port)
        try:
            server.run()
        except KeyboardInterrupt:
            logger.info("正在停止...")
            self.stop()

    def stop(self):
        """停止 Worker。"""
        self._running = False
        # 停止云同步
        if self.state.cloud_sync:
            self.state.cloud_sync.stop()
        # 停止 PM Agent
        if self.state.pm_agent:
            self.state.pm_agent.stop()
            logger.info("PM Agent 已停止")
        if self.discovery:
            self.discovery.stop()
