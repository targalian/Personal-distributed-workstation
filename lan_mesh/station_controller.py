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
import os
import shutil
import socket
import sys
import threading
import time
import uuid
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
from .logger import get_logger

logger = get_logger("station")
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
from .bot_gateway import BotGateway, BotChannel
from .cloud_sync import CloudSyncManager


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
    p2p_messages: dict = field(default_factory=dict)  # 主机间 P2P 聊天消息存储
    cloud_sync: CloudSyncManager = None  # 云存储同步管理器


class StationController:
    """Station Director 独立控制器。

    启动后:
    - 通过 UDP 广播自身存在 (角色=station)
    - 接收 Worker 的 HTTP 注册和心跳
    - 管理 Station Director (主机评级/资源池)
    - 提供 Web UI (Station Tab 为默认)

    激活 Secretary 后 (同进程):
    - 加载 ChatHandler / ProjectManager / ModelRouter / MCPGateway
    - 任务由 PM Agent 接管 (在 Worker 上运行, 替代 Orchestrator)
    - Web UI 显示 Secretary Tabs (对话/任务/团队/Agent/工具/项目)
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

        # Bot 通道 (手机消息推送) — 传入聚合/重试/免打扰配置
        bot_cfg = self.cfg.bot
        qh = bot_cfg.quiet_hours
        self.bot_gateway = BotGateway(
            aggregate_window=bot_cfg.aggregate_window,
            max_retry=bot_cfg.max_retry,
            retry_backoff=bot_cfg.retry_backoff,
            quiet_hours={
                "enabled": qh.enabled,
                "start": qh.start,
                "end": qh.end,
                "override_priority": qh.override_priority,
            },
        )
        self._load_bot_config()

        # Secretary 组件 (初始未加载, activate_secretary() 时创建)
        self.secretary_active = False
        self.project_manager = None
        self.orchestrator = None  # 保留属性以兼容旧引用, 但不再使用
        self.model_router = None
        self._default_model = ""  # 全局默认模型 (model_pool.yaml)
        self.mcp_gateway = None
        self.chat_handler = None    # 秘书聊天处理器
        self.chat_runtime = None    # 聊天专用 AgentRuntime
        self._mcp_config_path = str(self.data_dir / "mcp_servers.yaml")

        # Secretary 主机分配追踪 (哪台主机在运行 Secretary)
        self.secretary_host_id = None  # device_id of the host running Secretary
        self.secretary_host_port = None  # Secretary HTTP port on that host

        # ── 优化7: PM→Worker 映射 (用于反向沟通和取消/暂停) ──
        self._pm_worker_map: dict[str, dict] = {}  # pm_id → {"ip", "api_port", "device_id"}

        # ── 内嵌 Worker 能力: 本机直接运行 PM Agent (无需单独 Worker 进程) ──
        self._local_pm_agent = None       # 本机 PM Agent 实例
        self._local_sub_agents: dict = {} # 本机子 Agent Runtime 实例

        self._running = False
        self._start_timestamp: float = time.time()  # F1.2: 启动时间戳
        self._threads: list[threading.Thread] = []
        self._ws_push_event: Optional[asyncio.Event] = None  # 在 async 上下文中初始化
        self._ws_broadcast_queue: list = []  # 同步代码向 WS 队列塞事件

        # ── 优化12: 定期汇报 ──
        self._report_interval: float = 300.0  # 汇报间隔 (默认5分钟)
        self._last_report_time: float = 0.0
        self._server = None  # uvicorn Server 引用 (dev-reload 优雅重启用)

        # ── F3.1: 自动扩缩容 ──
        self._autoscale_up_threshold: int = 2    # 队列积压 >= 2 时扩容
        self._autoscale_down_threshold: int = 0  # 队列清空时记录缩容观察

        # ── S2: 版本升级提醒 ──
        from .version_sync import UpgradeNotifier
        self._upgrade_notifier = UpgradeNotifier()   # 同目标同版本只通知一次
        self._version_behind_warned: set = set()     # 已提醒过的领先者 commit

        # ── Phase 0: 节点间 mesh token 认证 ──
        # 出站请求自动携带 token, 入站由 api_guard_middleware 校验
        from .auth import get_mesh_token
        from .http_retry import http_post, set_auth_token
        self._mesh_auth_enabled = bool(cfg.security.auth_enabled)
        # S1: 加密信任根 — 无论认证开关如何都加载持久化 token (注册引导
        # 时已全网下发同一值); 认证开关仅门控传输层校验, 不影响密钥分发
        self._mesh_token = get_mesh_token(cfg)
        if self._mesh_auth_enabled:
            set_auth_token(self._mesh_token)
            logger.info("节点间认证已启用 (auth_enabled=true), 出站请求将携带 mesh token")
        else:
            set_auth_token("")
            logger.info("节点间认证未启用 (config.yaml security.auth_enabled=false)")

    # ── F1.2: 自愈重启 ─────────────────────────────────────────

    def request_restart(self, reason: str = ""):
        """F1.2: 调度 graceful 重启。

        在后台线程中等待 3 秒后关闭进程，
        配合外部进程管理器 (systemd/schtasks) 实现自动拉起。
        """
        import os
        import signal

        logger.warning("自愈重启已触发: %s", reason)

        def _delayed_exit():
            time.sleep(3)
            logger.info("进程即将退出 (reason=%s)", reason)
            os.kill(os.getpid(), signal.SIGTERM if hasattr(signal, 'SIGTERM') else signal.SIGINT)

        t = threading.Thread(target=_delayed_exit, daemon=True, name="restart-worker")
        t.start()

    # ── Secretary 激活/停用 ───────────────────────────────────────

    def activate_secretary(self) -> dict:
        """同进程激活 Secretary 模式。

        PM Agent 架构演进后:
        - 不再创建 Orchestrator (由 PM Agent 接管任务分解/调度/分发)
        - 创建 ChatHandler (Web 端聊天窗口)
        - 保留 ModelRouter 和 MCPGateway (PM 需要)
        - 创建聊天专用 AgentRuntime (LLM 调用)
        """
        if self.secretary_active:
            return {"ok": True, "message": "Secretary 已激活", "already_active": True}

        from .project import ProjectManager
        from .model_router import ModelRouter
        from .mcp_gateway import MCPGateway
        from .agent_runtime import AgentRuntime
        from .chat_handler import ChatHandler

        # 项目管理器
        self.project_manager = ProjectManager(self.db)

        # 模型路由器
        model_pool = load_model_pool()
        self.model_router = ModelRouter(model_pool.models, self.project_manager) if model_pool.models else None
        self._default_model = model_pool.default_model  # 全局默认模型
        if self.model_router:
            model_info = f"{self.model_router.pool_size} 个模型"
            if self._default_model:
                model_info += f", 默认: {self._default_model}"
            logger.info("模型路由器已加载: %s", model_info)

        # R1: 模型资源管理 (resources.yaml 不存在时 no-op, 不影响功能)
        try:
            from .model_resources import (init_resource_manager,
                                          set_bot_notify_global)
            resources_path = self._find_resources_path()
            if resources_path:
                init_resource_manager(
                    resources_path,
                    model_pool.models if model_pool.models else None,
                    self.db,
                )
                # R7: 到期/额度预警推送到 Bot 通道
                set_bot_notify_global(self.bot_gateway.notify)
        except Exception as e:
            logger.warning("模型资源管理初始化失败 (no-op): %s", e)

        # MCP 工具网关
        self.mcp_gateway = MCPGateway()

        # 聊天专用 AgentRuntime (用于秘书对话的 LLM 调用)
        self.chat_runtime = AgentRuntime(
            agent_id=f"secretary-{self.state.device_id[:8]}",
            shared_folder_path=str(self.state.shared_folder.path),
        )

        # 秘书聊天处理器 (多对话 + 共享文件夹持久化)
        self.chat_handler = ChatHandler(
            runtime=self.chat_runtime,
            controller=self,
            db=self.db,
            shared_folder=self.state.shared_folder,
        )

        # 优化15: 将 ChatHandler 注入 BotGateway, 统一消息入口
        if self.bot_gateway:
            self.bot_gateway.set_chat_handler(self.chat_handler)

        self.secretary_active = True
        # BUG-004 fix: 统一状态源 — 激活时同步设置 host_id/port
        self.secretary_host_id = self.state.device_id
        self.secretary_host_port = self.state.api_port
        logger.info("Secretary 模式已激活 — 聊天处理器/模型路由/MCP工具 已就绪 (PM Agent 架构)")

        # 任务断点恢复: 将上次运行中断的任务标记为 interrupted
        self._recover_stale_tasks()

        return {
            "ok": True,
            "message": "Secretary 已激活",
            "models": self.model_router.pool_size if self.model_router else 0,
        }

    def _find_resources_path(self) -> str:
        """查找 resources.yaml (与 model_pool.yaml 同目录约定)。"""
        candidates = []
        env_path = os.environ.get("LAN_MESH_RESOURCES")
        if env_path:
            candidates.append(Path(env_path))
        candidates.append(Path(__file__).parent / "resources.yaml")
        candidates.append(Path("resources.yaml"))
        for c in candidates:
            try:
                if c.is_file():
                    return str(c)
            except Exception:
                continue
        return ""

    def deactivate_secretary(self) -> dict:
        """停用 Secretary 模式, 回到纯 Station 模式。"""
        if not self.secretary_active:
            return {"ok": True, "message": "Secretary 未激活"}

        self.secretary_active = False
        # BUG-004 fix: 统一状态源 — 停用时同步清除 host_id/port
        self.secretary_host_id = None
        self.secretary_host_port = None
        self.project_manager = None
        self.orchestrator = None
        self.model_router = None
        self.mcp_gateway = None
        self.chat_handler = None
        self.chat_runtime = None

        # 优化15: 清除 BotGateway 的 ChatHandler 引用
        if self.bot_gateway:
            self.bot_gateway.set_chat_handler(None)

        logger.info("Secretary 模式已停用 — 回到纯基础设施管理")
        return {"ok": True, "message": "Secretary 已停用"}

    def _recover_stale_tasks(self):
        """任务断点恢复: 将上次运行中断的任务标记为 interrupted。

        系统重启后, 之前处于 running/monitoring/planning/executing 的任务
        其 PM Agent 进程已不存在, 无法继续执行。
        将这些任务标记为 interrupted, 用户可在 UI 中看到并决定重新提交或删除。
        """
        stale_statuses = ("running", "monitoring", "planning", "executing")
        recovered = 0
        for status in stale_statuses:
            try:
                tasks = self.db.list_tasks(status=status, limit=100)
                for task in tasks:
                    task.status = "interrupted"
                    if not task.output_data:
                        task.output_data = {}
                    task.output_data["_interrupted"] = {
                        "reason": "系统重启, PM Agent 进程已丢失",
                        "original_status": status,
                        "interrupted_at": time.time(),
                    }
                    self.db.save_task(task)
                    recovered += 1
            except Exception as e:
                logger.warning("恢复中断任务失败 (status=%s): %s", status, e)
        if recovered:
            logger.info("任务断点恢复: %d 个中断任务已标记为 interrupted", recovered)

    # ── Secretary 自动选举 ─────────────────────────────────────

    def _secretary_election(self):
        """First-Station-Wins 选举: 等待发现窗口, 判断是否由本站担任 Secretary。"""
        logger.info("Secretary 选举: 等待 5s 发现窗口...")
        time.sleep(5)  # 等待 UDP 发现收集网络中已有主机

        existing = self._find_existing_secretary()
        if not existing:
            try:
                self.activate_secretary()
                logger.info("Secretary 选举: 本站当选 (网络中无其他 Secretary)")
                # 通知前端 Secretary 已激活
                self._queue_ws_broadcast("secretary_activated", {
                    "message": "Secretary 自动选举激活",
                    "device_id": self.state.device_id,
                })
            except Exception as e:
                logger.error("Secretary 自动激活失败: %s (可在 Web UI 手动激活)", e)
        else:
            logger.info("Secretary 选举: 网络中已有 Secretary [%s], 本站保持 Station 模式", existing)
            self._converge_mesh_token()

    def _converge_mesh_token(self):
        """S1: 从 Secretary 拉取 mesh token 收敛加密信任根。

        Station 间无注册链路 (注册仅 Worker 用), 非 Secretary 节点
        在此处引导拉取并持久化; 与注册下发同一信任假设 (LAN 成员)。
        """
        from .auth import save_mesh_token
        from .http_retry import http_get
        sec_host = None
        for h in self.db.list_hosts():
            if (getattr(h, "device_id", "") != self.state.device_id
                    and getattr(h, "role", "") == "secretary"
                    and getattr(h, "online", False)
                    and getattr(h, "ip", "") and getattr(h, "api_port", 0)):
                sec_host = h
                break
        if not sec_host:
            return
        try:
            resp = http_get(
                f"http://{sec_host.ip}:{sec_host.api_port}"
                "/api/station/bootstrap-token", timeout=10)
            token = (resp.json() or {}).get("mesh_token", "")
            if token and token != self._mesh_token:
                if save_mesh_token(token):
                    self._mesh_token = token
                    logger.info("[S1] mesh token 已收敛 (拉取自 Secretary %s)",
                                sec_host.ip)
        except Exception as e:
            logger.warning("[S1] mesh token 收敛失败: %s", e)

    def _find_existing_secretary(self) -> str:
        """查找网络中已存在的在线 Secretary。返回设备名或空字符串。"""
        hosts = self.db.list_hosts()
        for h in hosts:
            if (getattr(h, 'device_id', '') != self.state.device_id
                    and getattr(h, 'role', '') == 'secretary'
                    and getattr(h, 'online', False)):
                return getattr(h, 'device_name', h.device_id[:8])
        return ""

    def submit_task_from_chat(self, name: str, description: str, created_by: str = "secretary",
                              priority: str = "normal") -> dict:
        """从秘书对话直接提交任务并分配 PM Agent。

        与 station_api.submit_task() 逻辑一致, 但同步执行。
        优化13: 支持优先级 + 负载感知选站。

        Args:
            priority: 优先级 (low / normal / high / urgent)
        """
        from .protocol import Task, PMAgent

        task = Task(
            task_id=f"task-{uuid.uuid4().hex[:12]}",
            name=name,
            description=description,
            created_by=created_by,
            status="pending",
        )
        # 优化13: 记录优先级到 input_data
        task.input_data = task.input_data or {}
        task.input_data["_priority"] = priority
        self.db.save_task(task)
        logger.info("对话提交任务: %s (%s) 优先级=%s", task.task_id, name, priority)
        # WS 广播: 通知前端任务面板刷新
        self._queue_ws_broadcast("task_submitted", task.to_dict())

        # 选择在线 work_station (优化13: 评级 + 负载感知)
        hosts = self.db.list_hosts()
        online_hosts = [h for h in hosts if h.online and h.device_id != self.state.device_id]
        if not online_hosts:
            online_hosts = [h for h in hosts if h.online]

        if not online_hosts:
            task.status = "failed"
            task.output_data = {"error": "无可用 work_station"}
            self.db.save_task(task)
            return task.to_dict()

        # 优化13: 负载感知排序 — 评级优先, 同评级选 PM 数量少的
        tier_order = {"S": 5, "A": 4, "B": 3, "C": 2, "D": 1, "": 0}
        pm_agents = self.db.list_pm_agents()
        # 统计每台主机上的活跃 PM 数量
        host_pm_count: dict[str, int] = {}
        for pm in pm_agents:
            if pm.status in ("planning", "executing", "monitoring", "awaiting_input"):
                host_pm_count[pm.device_id] = host_pm_count.get(pm.device_id, 0) + 1

        def _host_sort_key(h):
            tier = tier_order.get(h.rating_tier, 0)
            load = host_pm_count.get(h.device_id, 0)
            # 先按评级降序, 再按负载升序
            return (-tier, load)

        online_hosts.sort(key=_host_sort_key)
        target_host = online_hosts[0]

        # 构造 Secretary URL: 本机派发用 localhost; 远程派发在回退分支重建可达地址
        secretary_url = f"http://127.0.0.1:{self.state.api_port}"

        # 优先本机派发 (内嵌 Worker, 无需单独 Worker 进程)
        if self.secretary_active and self.chat_runtime:
            result = self._local_start_pm(task.task_id, secretary_url, task.to_dict())
            if result.get("ok"):
                pm_id = result["pm_id"]
                task.pm_agent_id = pm_id
                task.status = "running"
                self.db.save_task(task)
                pm_agent = PMAgent(
                    pm_id=pm_id,
                    agent_name=f"PM-{pm_id[:8]}",
                    task_id=task.task_id,
                    device_id=self.state.device_id,
                    hostname=self.state.device_name,
                    ip="127.0.0.1",
                    api_port=self.state.api_port,
                    status="starting",
                )
                self.db.upsert_pm_agent(pm_agent)
                self._pm_worker_map[pm_id] = {
                    "ip": "127.0.0.1",
                    "api_port": self.state.api_port,
                    "device_id": self.state.device_id,
                    "local": True,
                }
                logger.info("PM Agent 本机启动: %s", pm_id[:12])
                # WS 广播: 通知前端 PM 面板 + 任务面板刷新
                self._queue_ws_broadcast("pm_registered", {
                    "pm_id": pm_id, "agent_name": f"PM-{pm_id[:8]}",
                    "task_id": task.task_id, "status": "starting",
                    "device_id": self.state.device_id,
                })
                self._queue_ws_broadcast("task_updated", task.to_dict())
                self.bot_gateway.notify("pm_registered", {
                    "pm_id": pm_id[:12], "task": task.name, "station": self.state.device_name,
                })
                return task.to_dict()
            else:
                logger.warning("本机 PM 启动失败: %s, 尝试远程派发", result.get('message'))

        # 回退: POST 到远程 Worker 启动 PM Agent
        # 修复: 如果目标就是本机 (Tailscale IP 不可达), 直接用 127.0.0.1
        target_ip = target_host.ip
        if target_host.device_id == self.state.device_id:
            target_ip = "127.0.0.1"
        # 修复 (任务③): 远程 PM 必须回报到本机 Secretary, 不能用 127.0.0.1
        from .host_info import pick_reachable_ip
        reach_ip = pick_reachable_ip(target_ip)
        remote_secretary_url = (
            f"http://{reach_ip}:{self.state.api_port}" if reach_ip else secretary_url
        )
        try:
            resp = http_post(
                f"http://{target_ip}:{target_host.api_port}/role/start-pm",
                json={
                    "task_id": task.task_id,
                    "secretary_url": remote_secretary_url,
                    "task_data": task.to_dict(),
                },
                timeout=15,
            )
            if resp.status_code == 200:
                pm_data = resp.json()
                pm_id = pm_data.get("pm_id", "")
                task.pm_agent_id = pm_id
                task.status = "running"
                self.db.save_task(task)

                pm_agent = PMAgent(
                    pm_id=pm_id,
                    agent_name=f"PM-{pm_id[:8]}",
                    task_id=task.task_id,
                    device_id=target_host.device_id,
                    hostname=target_host.hostname or target_host.device_name,
                    ip=target_host.ip,
                    api_port=target_host.api_port,
                    status="starting",
                )
                self.db.upsert_pm_agent(pm_agent)
                # 优化7: 记录 PM→Worker 映射
                self._pm_worker_map[pm_id] = {
                    "ip": target_host.ip,
                    "api_port": target_host.api_port,
                    "device_id": target_host.device_id,
                }
                logger.info("PM Agent 已启动: %s → %s", pm_id[:12], target_host.device_name)
                self.bot_gateway.notify("pm_registered", {
                    "pm_id": pm_id[:12], "task": task.name,
                    "station": target_host.device_name or target_host.hostname,
                })
                # 方案C: 自动将 PM 绑定到当前活跃对话的线程
                self._auto_attach_pm_thread(pm_id, task.name, f"PM-{pm_id[:8]}")
            else:
                task.status = "failed"
                task.output_data = {"error": f"PM 启动失败: {resp.text}"}
                self.db.save_task(task)
        except Exception as e:
            task.status = "failed"
            task.output_data = {"error": f"PM 启动异常: {e}"}
            self.db.save_task(task)

        return task.to_dict()

    # ── 内嵌 Worker: 本机 PM Agent 管理 ─────────────────────

    def _local_start_pm(self, task_id: str, secretary_url: str, task_data: dict = None) -> dict:
        """在本机 Station 进程内直接启动 PM Agent (无需 Worker)。"""
        if self._local_pm_agent and getattr(self._local_pm_agent, '_running', False):
            return {"ok": False, "message": "本机 PM Agent 已在运行"}
        if not self.chat_runtime:
            return {"ok": False, "message": "AgentRuntime 未初始化 (Secretary 未激活)"}

        import uuid as _uuid
        from .pm_agent import ProjectManagerAgent
        pm_id = f"pm-{_uuid.uuid4().hex[:12]}"

        if not task_data:
            return {"ok": False, "message": "缺少任务数据"}

        self._local_pm_agent = ProjectManagerAgent(
            pm_id=pm_id,
            agent_runtime=self.chat_runtime,
            secretary_url=secretary_url,
            device_id=self.state.device_id,
            device_name=self.state.device_name,
        )
        self._local_pm_agent.start_task(task_data)
        logger.info("本机 PM Agent 已启动: %s, 任务: %s", pm_id, task_id)
        # 方案C: 自动将 PM 绑定到当前活跃对话的线程
        task_name = task_data.get("name", task_id)
        self._auto_attach_pm_thread(pm_id, task_name, f"PM-{pm_id[:8]}")
        return {"ok": True, "pm_id": pm_id, "device_id": self.state.device_id}

    def _local_stop_pm(self) -> dict:
        """停止本机 PM Agent。"""
        if not self._local_pm_agent:
            return {"ok": False, "message": "PM Agent 未运行"}
        self._local_pm_agent.cancel()
        pm_id = self._local_pm_agent.pm_id
        self._local_pm_agent = None
        self._local_sub_agents.clear()
        logger.info("本机 PM Agent 已停止: %s", pm_id)
        return {"ok": True, "pm_id": pm_id}

    def _local_cancel_pm(self) -> dict:
        if not self._local_pm_agent:
            return {"ok": False, "message": "PM Agent 未运行"}
        self._local_pm_agent.cancel()
        return {"ok": True, "pm_id": self._local_pm_agent.pm_id}

    def _local_pause_pm(self) -> dict:
        if not self._local_pm_agent:
            return {"ok": False, "message": "PM Agent 未运行"}
        self._local_pm_agent.pause()
        return {"ok": True, "pm_id": self._local_pm_agent.pm_id}

    def _local_inject_input(self, input_data: dict) -> dict:
        if not self._local_pm_agent or not self._local_pm_agent._running:
            return {"ok": False, "message": "PM Agent 未运行"}
        self._local_pm_agent.receive_input(input_data)
        return {"ok": True, "pm_id": self._local_pm_agent.pm_id}

    def _auto_attach_pm_thread(self, pm_id: str, task_name: str, agent_name: str):
        """方案C: PM 启动后自动绑定到当前活跃对话的线程列表。"""
        ch = self.chat_handler
        if not ch:
            return
        conv_id = ch._active_conv_id
        if not conv_id:
            return
        try:
            ch.attach_pm_thread(conv_id, pm_id, task_name=task_name, agent_name=agent_name)
        except Exception as e:
            logger.debug("方案C 自动绑定 PM 线程失败 (non-critical): %s", e)

    def _local_pm_status(self) -> dict:
        if not self._local_pm_agent:
            return {"running": False}
        return self._local_pm_agent.get_status()

    def _local_create_subagent(self, agent_name: str, skills: list,
                               task_description: str = "", system_prompt: str = "",
                               preferred_agent_id: str = "") -> dict:
        """在本机创建子 Agent。"""
        import uuid as _uuid
        from .agent_runtime import AgentRuntime
        agent_id = preferred_agent_id or f"sub-{_uuid.uuid4().hex[:10]}"
        sub_runtime = AgentRuntime(
            agent_id=agent_id,
            shared_folder_path=str(self.state.shared_folder.path),
            custom_system_prompt=system_prompt,
        )
        self._local_sub_agents[agent_id] = {
            "agent_id": agent_id, "agent_name": agent_name,
            "runtime": sub_runtime, "skills": skills,
            "current_task": task_description, "status": "idle",
        }
        logger.info("本机子 Agent 已创建: %s (%s)", agent_id, agent_name)
        return {"agent_id": agent_id, "agent_name": agent_name}

    def _local_forward_progress(self, report: dict) -> dict:
        if not self._local_pm_agent:
            return {"ok": False, "message": "PM Agent 未运行"}
        self._local_pm_agent.receive_progress_report(report)
        return {"ok": True}

    def _local_execute_task(self, payload: dict) -> dict:
        """本机执行 PM 分发的子任务。

        根据 reporter_id 定位子 Agent Runtime，找不到则回退到 chat_runtime。
        执行期间更新子 Agent 状态 (idle → executing → idle)。
        """
        reporter_id = payload.get("reporter_id", "")
        sub_info = self._local_sub_agents.get(reporter_id)

        if sub_info:
            runtime = sub_info["runtime"]
            sub_info["status"] = "executing"
            sub_info["current_task"] = payload.get("name", "")
            logger.info("本机子 Agent 执行任务: %s (%s)", reporter_id, payload.get("name", ""))
        else:
            # 回退: 使用通用 runtime
            runtime = self.chat_runtime
            if not runtime:
                return {"output": {}, "status": "failed", "error": "Agent 运行时未初始化"}
            logger.warning("子 Agent %s 未找到, 回退到通用 Runtime", reporter_id)

        try:
            result = runtime.execute(payload)
        except Exception as e:
            logger.error("本机子任务执行异常: %s", e)
            result = {"output": {}, "status": "failed", "error": str(e)}
        finally:
            if sub_info:
                sub_info["status"] = "idle"

        # 执行完成后向 PM 上报
        if self._local_pm_agent and self._local_pm_agent.running:
            task_name = payload.get("name", "")
            status = result.get("status", "failed")
            output_data = result.get("output", {})
            self._local_pm_agent.receive_subtask_result(
                task_name=task_name,
                status=status,
                output_data=output_data,
                agent_id=reporter_id,
            )

        return result

    # ── 优化7: 反向沟通 ──

    def inject_input_to_pm(self, pm_id: str, input_data: dict) -> dict:
        """向指定 PM Agent 注入来自 Boss 的回复。

        优先检查本机 PM, 否则 HTTP POST 到远程 Worker。
        """
        worker = self._pm_worker_map.get(pm_id)

        # 本机 PM 直接调用
        if worker and worker.get("local") and self._local_pm_agent:
            result = self._local_inject_input(input_data)
            if result.get("ok"):
                logger.info("已注入回复到本机 PM %s", pm_id[:12])
                return {"ok": True, "message": "回复已发送到 PM Agent"}
            return result

        if not worker:
            return {"ok": False, "message": f"PM {pm_id[:12]} 的 Worker 信息未找到"}

        ip = worker.get("ip", "")
        port = worker.get("api_port", 0)
        if not ip or not port:
            return {"ok": False, "message": "Worker 地址信息不完整"}

        try:
            resp = http_post(
                f"http://{ip}:{port}/pm/inject-input",
                json=input_data,
                timeout=10,
            )
            if resp.status_code == 200:
                result = resp.json()
                logger.info("已注入回复到 PM %s", pm_id[:12])
                return {"ok": True, "message": "回复已发送到 PM Agent"}
            else:
                return {"ok": False, "message": f"Worker 返回错误: {resp.text[:200]}"}
        except Exception as e:
            return {"ok": False, "message": f"注入失败: {e}"}

    # ── 优化8: 取消/暂停任务 ──

    def cancel_task(self, task_id: str) -> dict:
        """取消指定任务及对应的 PM Agent。

        Args:
            task_id: 任务 ID

        Returns:
            {ok: bool, message: str}
        """
        task = self.db.get_task(task_id)
        if not task:
            return {"ok": False, "message": f"任务 {task_id} 不存在"}

        pm_id = task.pm_agent_id
        if not pm_id:
            # 任务尚未分配 PM, 直接取消
            self.db.update_task_status(task_id, "cancelled")
            return {"ok": True, "message": "任务已取消 (未分配 PM)"}

        worker = self._pm_worker_map.get(pm_id)
        if not worker:
            # Worker 信息丢失, 标记取消
            self.db.update_task_status(task_id, "cancelled")
            self.db.update_pm_status(pm_id, "cancelled")
            return {"ok": True, "message": "任务已标记取消 (Worker 信息丢失)"}

        # 本机 PM 直接取消
        if worker.get("local"):
            self._local_cancel_pm()
            self.db.update_task_status(task_id, "cancelled")
            self.db.update_pm_status(pm_id, "cancelled")
            logger.info("本机任务已取消: %s", task_id)
            self.bot_gateway.notify("task_cancelled", {"task_id": task_id, "name": task.name})
            return {"ok": True, "message": "任务已取消"}

        ip = worker.get("ip", "")
        port = worker.get("api_port", 0)
        if not ip or not port:
            return {"ok": False, "message": "Worker 地址信息不完整"}

        try:
            resp = http_post(
                f"http://{ip}:{port}/role/cancel-pm",
                timeout=10,
            )
            if resp.status_code == 200:
                self.db.update_task_status(task_id, "cancelled")
                self.db.update_pm_status(pm_id, "cancelled")
                logger.info("任务已取消: %s", task_id)
                self.bot_gateway.notify("task_cancelled", {
                    "task_id": task_id, "name": task.name,
                })
                return {"ok": True, "message": "任务已取消"}
            else:
                return {"ok": False, "message": f"Worker 返回错误: {resp.text[:200]}"}
        except Exception as e:
            return {"ok": False, "message": f"取消失败: {e}"}

    def pause_task(self, task_id: str) -> dict:
        """暂停指定任务及对应的 PM Agent。

        Args:
            task_id: 任务 ID

        Returns:
            {ok: bool, message: str}
        """
        task = self.db.get_task(task_id)
        if not task:
            return {"ok": False, "message": f"任务 {task_id} 不存在"}

        pm_id = task.pm_agent_id
        if not pm_id:
            return {"ok": False, "message": "任务尚未分配 PM Agent"}

        worker = self._pm_worker_map.get(pm_id)
        if not worker:
            return {"ok": False, "message": "PM Agent 的 Worker 信息未找到"}

        # 本机 PM 直接暂停
        if worker.get("local"):
            self._local_pause_pm()
            self.db.update_task_status(task_id, "paused")
            self.db.update_pm_status(pm_id, "paused")
            logger.info("本机任务已暂停: %s", task_id)
            self.bot_gateway.notify("task_paused", {"task_id": task_id, "name": task.name})
            return {"ok": True, "message": "任务已暂停"}

        ip = worker.get("ip", "")
        port = worker.get("api_port", 0)
        if not ip or not port:
            return {"ok": False, "message": "Worker 地址信息不完整"}

        try:
            resp = http_post(
                f"http://{ip}:{port}/role/pause-pm",
                timeout=10,
            )
            if resp.status_code == 200:
                self.db.update_task_status(task_id, "paused")
                self.db.update_pm_status(pm_id, "paused")
                logger.info("任务已暂停: %s", task_id)
                self.bot_gateway.notify("task_paused", {
                    "task_id": task_id, "name": task.name,
                })
                return {"ok": True, "message": "任务已暂停"}
            else:
                return {"ok": False, "message": f"Worker 返回错误: {resp.text[:200]}"}
        except Exception as e:
            return {"ok": False, "message": f"暂停失败: {e}"}

    # ── Bot 通道管理 ───────────────────────────────────────────────

    def _load_bot_config(self):
        """从 AppConfig 加载 Bot 通道配置。"""
        bot_cfg = self.cfg.bot
        for ch_cfg in bot_cfg.channels:
            channel = BotChannel(
                channel_type=ch_cfg.channel_type,
                enabled=ch_cfg.enabled,
                webhook_url=ch_cfg.webhook_url,
                bot_token=ch_cfg.bot_token,
                chat_id=ch_cfg.chat_id,
                webhook_url_base=ch_cfg.webhook_url_base,
                min_priority=ch_cfg.min_priority,
            )
            self.bot_gateway.add_channel(channel)
        # 设置命令处理回调
        self.bot_gateway.set_command_handler(self._on_bot_command)
        enabled_count = sum(1 for c in bot_cfg.channels if c.enabled)
        if enabled_count:
            logger.info("Bot 通道已加载: %d 个启用", enabled_count)

    def _on_orchestrator_event(self, event_type: str, data: dict):
        """Orchestrator 事件回调 → 转发到 Bot 通道。"""
        try:
            self.bot_gateway.notify(event_type, data)
        except Exception as e:
            logger.error("Bot 事件转发失败: %s", e)

    def _on_bot_command(self, command: str, args: str, chat_id: str) -> str:
        """处理来自 Telegram 的命令。"""
        if command == "status":
            hosts = self.db.list_hosts()
            online = [h for h in hosts if h.online]
            tasks = self.db.list_tasks(limit=5) if self.secretary_active else []
            return (
                f"📊 工作站状态\n"
                f"在线主机: {len(online)}/{len(hosts)}\n"
                f"Secretary: {'激活' if self.secretary_active else '未激活'}\n"
                f"最近任务: {len(tasks)} 个"
            )
        if command == "hosts":
            hosts = self.db.list_hosts()
            lines = ["🖥️ 主机列表:"]
            for h in hosts:
                status = "✅" if h.online else "❌"
                lines.append(f"{status} {h.device_name or h.hostname or '未知'} ({h.ip or '-'})")
            return "\n".join(lines) if len(lines) > 1 else "暂无主机"
        if command == "tasks":
            if not self.secretary_active:
                return "Secretary 未激活"
            tasks = self.db.list_tasks(limit=5)
            if not tasks:
                return "暂无任务"
            lines = ["📋 最近任务:"]
            for t in tasks:
                icon = {"completed": "✅", "failed": "❌", "running": "🔄", "pending": "⏳"}.get(t.status, "❓")
                lines.append(f"{icon} {t.name} [{t.status}]")
            return "\n".join(lines)
        return f"未知命令: /{command}\n可用: /status /hosts /tasks /help"

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
        """UDP 发现到设备时: 首次自动注册入 DB, 后续更新 last_seen (轻量心跳)。

        UDP presence 包每 3 秒到达一次, 证明对方存活。
        对于已注册主机, 利用 UDP 包更新 last_seen + IP, 避免被 prune_offline 误判离线。
        这对 Station 间互相发现尤为重要 (Station 间无 HTTP 心跳通道)。
        """
        if not packet.device_id or packet.device_id == self.state.device_id:
            return

        # Secretary 冲突检测: 发现另一个 Secretary 且本站也是 Secretary
        if (packet.role == "secretary" and self.secretary_active
                and not getattr(self, '_secretary_conflict_warned', False)):
            logger.warning("[Secretary 冲突] 网络中发现另一个 Secretary: %s (%s), 请手动决定保留哪个",
                          packet.device_name, ip)
            self._secretary_conflict_warned = True

        existing = self.db.get_host(packet.device_id)
        if existing:
            # 已注册: 仅更新 last_seen + IP + 实时指标 (轻量心跳)
            try:
                self.station_director.on_heartbeat(packet.device_id, {
                    "cpu_percent": packet.cpu_percent,
                    "memory_percent": packet.memory_percent,
                    "disk_percent": packet.disk_percent,
                    "ip": ip,
                })
            except Exception:
                pass
            return

        # 首次发现: 从 UDP 包构造 HostInfo 并自动入站
        try:
            info = HostInfo(
                device_id=packet.device_id,
                device_name=packet.device_name,
                role=packet.role,
                hostname=packet.hostname,
                platform=packet.platform,
                cpu_count=packet.cpu_count,
                cpu_percent=packet.cpu_percent,
                memory_total_mb=packet.memory_total_mb,
                memory_percent=packet.memory_percent,
                disk_total_gb=packet.disk_total_gb,
                disk_percent=packet.disk_percent,
                shared_folder=packet.shared_folder,
                ip_addresses=packet.ip_addresses or [ip],
                api_port=packet.api_port,
            )
            self.station_director.on_host_registered(info)
            # 触发 WS 立即推送 (从非 async 线程安全地设置 event)
            if self._ws_push_event:
                try:
                    loop = self._ws_push_event._loop
                    loop.call_soon_threadsafe(self._ws_push_event.set)
                except Exception:
                    pass
            logger.info("UDP 自动注册: %s (%s)", packet.device_name, ip)
        except Exception as e:
            logger.error("UDP 自动注册异常: %s", e)

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
        """定期刷新共享文件夹中的配置报告 + 自身心跳 + 优化12: 定期汇报。"""
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
                    "ip": info.ip_addresses[0] if info.ip_addresses else "",
                })
            except Exception:
                pass
            # 优化12: 定期汇报 (仅 Secretary 激活时)
            if self.secretary_active:
                self._try_periodic_report()

    def _prune_loop(self):
        """定期清理超时离线主机, 并触发 F3.3 PM 迁移。"""
        while self._running:
            time.sleep(PRUNE_INTERVAL_SECS)
            try:
                gone_ids = self.station_director.prune_offline(self.cfg.discovery.device_ttl)
                if gone_ids:
                    self._migrate_orphaned_pms(gone_ids)
            except Exception as e:
                logger.error("清理离线主机异常: %s", e)

    # ── S2: 版本升级提醒 ─────────────────────────────────

    def _version_watch_loop(self):
        """S2: 定期比对局域网内各节点代码版本, 领先者通知落后者升级。"""
        from .version_sync import local_version_info
        while self._running:
            time.sleep(60)
            try:
                self_ver = local_version_info()
                if not self_ver.get("commit") or not self.discovery:
                    continue
                peers = [
                    d for d in self.discovery.list_devices()
                    if d.get("online") and d.get("device_id") != self.state.device_id
                    and d.get("code_version")
                ]
                if not peers:
                    continue
                self._check_version_leadership(self_ver, peers)
            except Exception as e:
                logger.warning("[S2] 版本监测异常: %s", e)

    def _check_version_leadership(self, self_ver: dict, peers: list):
        """S2: 版本领先检测 — 本机领先全网则通知落后节点, 落后则提醒自身升级。

        通知内容仅为升级建议 (git pull + 重启), 不远程操控目标节点。
        """
        from .version_sync import compare_versions, find_leader
        from .http_retry import http_post

        versions = [{"device_id": self.state.device_id,
                     "commit": self_ver["commit"],
                     "commit_time": self_ver.get("commit_time", 0.0)}]
        for p in peers:
            versions.append({"device_id": p["device_id"],
                             "device_name": p.get("device_name", ""),
                             "commit": p.get("code_version", ""),
                             "commit_time": p.get("version_ts", 0.0),
                             "ip": p.get("ip", ""),
                             "api_port": p.get("api_port", 0)})
        leader = find_leader(versions)
        if not leader:
            return

        if leader["device_id"] == self.state.device_id:
            # 本机严格领先: 通知每个落后节点 (同版本只通知一次)
            for p in versions[1:]:
                if compare_versions(self_ver, p) != "ahead":
                    continue
                if not self._upgrade_notifier.should_notify(
                        p["device_id"], self_ver["commit"]):
                    continue
                ip, port = p.get("ip", ""), p.get("api_port", 0)
                if not ip or not port:
                    continue
                try:
                    http_post(
                        f"http://{ip}:{port}/api/version/upgrade-notice",
                        json={
                            "from_device_id": self.state.device_id,
                            "from_name": self.state.device_name,
                            "commit": self_ver["commit"],
                            "version": self_ver.get("version", ""),
                            "note": self_ver.get("note", ""),
                            "upgrade_hint": self_ver.get("upgrade_hint", ""),
                        }, timeout=10)
                    logger.info("[S2] 已通知 %s (%s) 升级: 本机 %s 领先于 %s",
                                p.get("device_name") or p["device_id"][:8], ip,
                                self_ver["commit"], p["commit"])
                except Exception as e:
                    logger.warning("[S2] 通知 %s 升级失败: %s", ip, e)
        else:
            # 他人领先: 提醒本机升级 (同一领先版本只提醒一次)
            lead_commit = leader.get("commit", "")
            if lead_commit in self._version_behind_warned:
                return
            self._version_behind_warned.add(lead_commit)
            lead_name = leader.get("device_name") or lead_commit[:8]
            logger.warning("[S2] 检测到 %s 版本领先 (%s), 建议本机 git pull 升级后重启",
                           lead_name, lead_commit)
            from .event_bus import publish_event
            publish_event("version_upgrade_notice", {
                "behind": True,
                "from_name": lead_name,
                "commit": lead_commit,
                "hint": "git pull 升级后重启节点",
            })

    # ── F3.3: PM Agent 故障迁移 ─────────────────────────────────

    def _migrate_orphaned_pms(self, gone_device_ids: list[str]):
        """F3.3: 检测离线主机上的 PM, 将其任务迁移到可用节点或本机接管。

        策略:
        1. 扫描 _pm_worker_map, 找出 device_id 在 gone_ids 中的 PM
        2. 查找该 PM 关联的任务, 重置为 pending
        3. 优先派发到其他在线 Worker, 否则本机接管
        """
        gone_set = set(gone_device_ids)
        orphaned_pms = []

        for pm_id, info in list(self._pm_worker_map.items()):
            if info.get("device_id") in gone_set:
                orphaned_pms.append((pm_id, info))

        if not orphaned_pms:
            return

        logger.warning("[F3.3] 检测到 %d 个 PM 因主机离线而孤立", len(orphaned_pms))

        # 查找可用替代 Worker
        hosts = self.db.list_hosts()
        available_workers = [
            h for h in hosts
            if getattr(h, 'online', False)
            and getattr(h, 'role', '') == 'worker'
            and getattr(h, 'device_id', '') not in gone_set
        ]

        for pm_id, info in orphaned_pms:
            # 查找关联任务
            task_id = info.get("task_id", "")
            if task_id:
                task = self.db.get_task(task_id)
                if task and getattr(task, 'status', '') in ('running', 'monitoring'):
                    # 重置任务状态
                    task.status = "pending"
                    task.pm_agent_id = ""
                    self.db.upsert_task(task)
                    logger.info("[F3.3] 任务 %s 已重置为 pending", task_id[:8])

                    # 尝试迁移
                    if available_workers:
                        target = available_workers[0]
                        self._dispatch_next_task_to_worker(target)
                        logger.info("[F3.3] 任务已迁移到 %s",
                                   getattr(target, 'device_name', ''))
                    else:
                        # 本机接管
                        logger.info("[F3.3] 无可用 Worker, 本机接管任务 %s", task_id[:8])
                        self._start_local_pm_for_task(task_id)

            # 清理映射
            del self._pm_worker_map[pm_id]

    def _start_local_pm_for_task(self, task_id: str):
        """F3.3: 本机启动 PM Agent 接管指定任务。"""
        try:
            from .pm_agent import ProjectManagerAgent
            task = self.db.get_task(task_id)
            if not task:
                return

            pm = ProjectManagerAgent(
                pm_id=f"pm-migrated-{task_id[:8]}",
                task=task.to_dict() if hasattr(task, 'to_dict') else {"task_id": task_id},
                secretary_url=f"http://127.0.0.1:{self.state.api_port}",
                runtime=None,
            )
            self._local_pm_agent = pm
            logger.info("[F3.3] 本机 PM 已启动: %s", pm.pm_id[:8])
        except Exception as e:
            logger.error("[F3.3] 本机接管失败: %s", e)

    def _try_periodic_report(self):
        """优化12: 定期汇报 — 当有活跃任务时, 生成简报推送到 Web UI 和 Bot。

        每 _report_interval 秒检查一次, 如果有 running/pending 任务:
        1. 汇总所有 PM 进度
        2. 检测异常 (进度停滞、资源不足)
        3. 生成简报推送到 WebSocket + Bot
        """
        now = time.time()
        if now - self._last_report_time < self._report_interval:
            return
        self._last_report_time = now

        try:
            # 检查是否有活跃任务
            tasks = self.db.list_tasks(limit=20)
            active_tasks = [t for t in tasks if t.status in ("running", "pending")]
            if not active_tasks:
                return  # 无活跃任务, 不汇报

            # 汇总 PM 进度
            pm_lines = []
            pms = self.db.list_pm_agents()
            for pm in pms:
                if pm.status in ("planning", "executing", "monitoring", "awaiting_input", "escalated"):
                    reports = self.db.get_progress_reports(pm.pm_id, limit=1)
                    progress = reports[0]["progress"] if reports else 0.0
                    pm_lines.append(f"{pm.agent_name}: {pm.status} ({progress*100:.0f}%)")

            # 检测异常
            alerts = []
            for pm in pms:
                if pm.status == "escalated":
                    alerts.append(f"🚨 {pm.agent_name} 已升级, 需要决策")
                elif pm.status == "awaiting_input":
                    alerts.append(f"⚠️ {pm.agent_name} 等待您的输入")

            # 生成简报
            report = (
                f"📊 工作站定期汇报\n"
                f"活跃任务: {len(active_tasks)} 个\n"
                f"PM Agent: {', '.join(pm_lines) if pm_lines else '无活跃 PM'}\n"
            )
            if alerts:
                report += f"⚠️ 注意: {'; '.join(alerts)}\n"

            # Bot 推送 (仅当有异常或活跃任务时)
            self.bot_gateway.notify("periodic_report", {
                "active_tasks": len(active_tasks),
                "summary": report[:300],
            })

            logger.info("定期汇报: %d 个活跃任务", len(active_tasks))

        except Exception as e:
            logger.error("定期汇报异常: %s", e)

    # ── F3.1: Worker 自动扩缩容 ─────────────────────────────────

    def _start_autoscaler(self):
        """F3.1: 启动自动扩缩容监控线程。"""
        t = threading.Thread(target=self._autoscaler_loop, daemon=True, name="autoscaler")
        t.start()
        logger.info("自动扩缩容监控已启动 (interval=30s, scale_up=%d, scale_down=%d)",
                   self._autoscale_up_threshold, self._autoscale_down_threshold)

    def _autoscaler_loop(self):
        """F3.1: 定期检测任务队列深度, 触发扩缩容决策。"""
        while self._running:
            time.sleep(30)  # 每 30 秒检测一次
            try:
                self._autoscale_check()
            except Exception as e:
                logger.debug("自动扩缩容检测异常: %s", e)

    def _autoscale_check(self):
        """F3.1: 扩缩容决策逻辑。

        策略:
        - 队列深度 > up_threshold → 尝试激活空闲 Worker
        - 队列深度 < down_threshold 且无活跃 PM → 允许 Worker 进入低功耗
        """
        # 统计任务队列
        all_tasks = self.db.list_tasks()
        pending = [t for t in all_tasks if getattr(t, 'status', '') in ('pending', 'queued')]
        running = [t for t in all_tasks if getattr(t, 'status', '') in ('running', 'monitoring')]
        queue_depth = len(pending)
        active_count = len(running)

        # 获取在线 Worker 列表
        hosts = self.db.list_hosts()
        online_workers = [h for h in hosts
                         if getattr(h, 'online', False)
                         and getattr(h, 'role', '') == 'worker']
        idle_workers = [h for h in online_workers
                       if not self._is_worker_busy(h)]

        # 扩容: 队列积压且有可用 Worker
        if queue_depth >= self._autoscale_up_threshold and idle_workers:
            target = idle_workers[0]
            logger.info("[自动扩容] 队列=%d, 激活 Worker: %s",
                       queue_depth, getattr(target, 'device_name', ''))
            self._dispatch_next_task_to_worker(target)

        # 缩容日志 (仅记录, 不主动关闭 Worker)
        elif queue_depth == 0 and active_count == 0 and online_workers:
            logger.debug("[缩容观察] 无活跃任务, %d 台 Worker 空闲", len(online_workers))

    def _is_worker_busy(self, host) -> bool:
        """F3.1: 检查 Worker 是否正在执行任务。"""
        device_id = getattr(host, 'device_id', '')
        # 检查是否有活跃 PM 在该 Worker 上
        for pm_info in self._pm_worker_map.values():
            if pm_info.get("device_id") == device_id:
                return True
        return False

    def _dispatch_next_task_to_worker(self, worker_host):
        """F3.1: 将队列中下一个 pending 任务派发到指定 Worker。"""
        all_tasks = self.db.list_tasks()
        pending = [t for t in all_tasks if getattr(t, 'status', '') == 'pending']
        if not pending:
            return

        task = pending[0]
        task_id = getattr(task, 'task_id', '')
        ip = getattr(worker_host, 'ip', '')
        port = getattr(worker_host, 'api_port', 0)

        if not ip or not port:
            return

        # 修复 (任务③): 远程 PM 回报地址用本机对目标可达的 IP, 非 127.0.0.1
        from .host_info import pick_reachable_ip
        reach_ip = pick_reachable_ip(ip)
        sec_url = (
            f"http://{reach_ip}:{self.state.api_port}"
            if reach_ip else f"http://127.0.0.1:{self.state.api_port}"
        )
        try:
            resp = http_post(
                f"http://{ip}:{port}/role/start-pm",
                json={"task_id": task_id, "secretary_url": sec_url},
                timeout=10,
            )
            if resp.status_code == 200:
                logger.info("[自动扩容] 任务 %s 已派发到 %s", task_id[:8], ip)
        except Exception as e:
            logger.debug("[自动扩容] 派发失败: %s", e)

    # ── S1: API Key 加密自动分发 ────────────────────────────

    def push_resource_secrets(self, only_device_id: str = "",
                              fallback_ip: str = "",
                              fallback_port: int = 0) -> list:
        """S1: 将本机资源配置 (含 api_key 直填) 加密推送到在线节点。

        信任根为 mesh_token (HKDF 派生 AES-256-GCM 密钥), 与认证
        开关解耦; token 缺失时拒绝推送 (绝不降级明文)。

        Args:
            only_device_id: 仅推指定节点 (空 = 本机外全部在线节点)
            fallback_ip/fallback_port: 目标主机记录缺 ip/port 时的
                回退地址 (注册即推场景, 发现信息尚未入库)

        Returns:
            [{"device_id", "device_name", "ok", "detail"}]
        """
        if not self._mesh_token:
            return [{"ok": False,
                     "detail": "无 mesh_token (加密信任根缺失), 拒绝推送"}]

        from .http_retry import http_post
        from .model_resources import read_config_data
        from .secret_sync import config_hash, encrypt_config
        target = Path(__file__).parent / "resources.yaml"
        if not target.is_file():
            return [{"ok": False, "detail": "本机无 resources.yaml, 无密钥可分发"}]
        cfg = read_config_data(target)
        data = cfg.get("data") or {}
        pools = data.get("resources") or []
        key_count = sum(1 for p in pools if (p.get("api_key") or "").strip())
        if not key_count:
            return [{"ok": False, "detail": "无资源池配置 api_key 直填值, 无需推送"}]

        hosts = self.db.list_hosts()
        targets = [
            h for h in hosts
            if h.online and h.device_id != self.state.device_id
            and (not only_device_id or h.device_id == only_device_id)
        ]
        if not targets:
            return [{"ok": False, "detail": "无其他在线节点"}]

        results = []
        cfg_hash = config_hash(data)
        for h in targets:
            ip = h.ip or (fallback_ip if h.device_id == only_device_id else "")
            port = h.api_port or (fallback_port if h.device_id == only_device_id else 0)
            item = {"device_id": h.device_id,
                    "device_name": h.device_name or h.hostname}
            if not ip or not port:
                item["ok"] = False
                item["detail"] = "目标地址不完整"
                results.append(item)
                continue
            try:
                payload = encrypt_config(data, self._mesh_token)
                payload["config_hash"] = cfg_hash
                resp = http_post(
                    f"http://{ip}:{port}/api/secrets/receive",
                    json=payload,
                    timeout=15,
                )
                try:
                    body = resp.json()
                except Exception:
                    body = {}
                item["ok"] = resp.status_code == 200
                if resp.status_code == 200 and body.get("applied"):
                    item["detail"] = f"已应用 ({body.get('pools', 0)} 池)"
                elif resp.status_code == 200:
                    item["detail"] = "配置一致, 已跳过"
                else:
                    item["detail"] = (body.get("detail")
                                      or f"HTTP {resp.status_code}")
            except Exception as e:
                item["ok"] = False
                item["detail"] = f"推送失败: {e}"
            results.append(item)
            logger.info("[S1] 密钥推送 %s (%s): %s — %s",
                        item.get("device_name"), ip,
                        "成功" if item["ok"] else "失败", item["detail"])
        return results

    async def _ws_push_loop(self):
        """定期向 WebSocket 客户端推送最新主机状态 (新主机入站时立即触发)。"""
        self._ws_push_event = asyncio.Event()
        while self._running:
            try:
                # 等待事件触发或 3 秒超时
                try:
                    await asyncio.wait_for(self._ws_push_event.wait(), timeout=3)
                except asyncio.TimeoutError:
                    pass
                self._ws_push_event.clear()
                hosts = self.db.list_hosts()
                from .station_api import _broadcast
                await _broadcast(self.state, "hosts", [h.to_dict() for h in hosts])

                # 消费同步代码塞入的广播队列 (任务创建/PM启动等事件)
                while self._ws_broadcast_queue:
                    evt = self._ws_broadcast_queue.pop(0)
                    await _broadcast(self.state, evt["type"], evt["data"])
            except Exception:
                pass

    def _queue_ws_broadcast(self, event_type: str, data):
        """从同步代码向 WS 广播队列塞事件 (下次 push loop 迭代时发送)。"""
        self._ws_broadcast_queue.append({"type": event_type, "data": data})
        # 触发 push loop 立即唤醒
        if self._ws_push_event:
            try:
                loop = self._ws_push_event._loop
                loop.call_soon_threadsafe(self._ws_push_event.set)
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

        # F1.5: 注册限流 + 认证中间件
        from .station_api import api_guard_middleware, configure_mesh_auth
        # Phase 0: 将节点认证配置同步给中间件 (auth_enabled 时才校验)
        configure_mesh_auth(self._mesh_auth_enabled, self._mesh_token)
        app.middleware("http")(api_guard_middleware)

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
                # 禁缓存: 避免 UI 更新后浏览器用旧页面导致按钮无响应
                return HTMLResponse(
                    content=html_path.read_text(encoding="utf-8"),
                    headers={"Cache-Control": "no-cache, must-revalidate"})
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

    def start(self, dev_reload: bool = False):
        """启动 Station Director。

        Args:
            dev_reload: 开发模式, 监控 lan_mesh/ 文件变动自动重启进程。
        """
        self._running = True

        # 启动前自检 (复用 secretary 自检: 含 DB 路径 + Web 模板检查)
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
        try:
            self_info = self._collect_info()
            self.station_director.on_host_registered(self_info)
            logger.info("自注册完成: %s (%s)", self_info.device_name, self_info.ip_addresses)
        except Exception as e:
            logger.warning("自注册失败: %s (服务器仍将启动)", e)

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
        self._refresh_host_config()
        logger.info("配置报告已生成: %s/host_config.json", self.state.shared_folder.path)

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

        # S2: 启动版本升级提醒监测线程
        version_thread = threading.Thread(
            target=self._version_watch_loop, name="station-version-watch", daemon=True
        )
        version_thread.start()
        self._threads.append(version_thread)

        # F3.1: 启动自动扩缩容监控
        self._start_autoscaler()

        # Secretary 自动选举 (First-Station-Wins, 后台线程不阻塞 API 启动)
        election_thread = threading.Thread(
            target=self._secretary_election, name="secretary-election", daemon=True
        )
        election_thread.start()
        self._threads.append(election_thread)

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
        self._server = server

        # 开发模式: 启动文件监控线程, 变动时自动重启进程
        if dev_reload:
            watch_thread = threading.Thread(
                target=self._dev_file_watcher, daemon=True, name="dev-reload"
            )
            watch_thread.start()
            logger.info("🔁 开发模式已启用: lan_mesh/ 文件变动将自动重启")

        local_ips = self._collect_info().ip_addresses
        logger.info("服务已启动!")
        logger.info("  Web UI:  http://localhost:%d", self.state.api_port)
        for ip in local_ips:
            logger.info("  局域网:  http://%s:%d", ip, self.state.api_port)
        logger.info("Secretary 已就绪, 可直接通过聊天窗口下发任务")

        try:
            server.run()
        except KeyboardInterrupt:
            logger.info("正在停止...")
            self.stop()

    def stop(self):
        """停止 Station Director。"""
        self._running = False
        if self.state.cloud_sync:
            self.state.cloud_sync.stop()
        if self.discovery:
            self.discovery.stop()

    def _dev_file_watcher(self):
        """开发模式: 监控 lan_mesh/ 目录文件变动, 检测到修改后自动重启进程。"""
        watch_dir = Path(__file__).parent
        extensions = {".py", ".html", ".yaml"}
        # 初始快照
        snapshot = {}
        for f in watch_dir.rglob("*"):
            if f.suffix in extensions and "__pycache__" not in str(f):
                try:
                    snapshot[str(f)] = f.stat().st_mtime
                except OSError:
                    pass
        logger.info("[dev-reload] 监控 %d 个文件 (%s)", len(snapshot), watch_dir)

        while self._running:
            time.sleep(1.5)
            changed = []
            for f in watch_dir.rglob("*"):
                if f.suffix not in extensions or "__pycache__" in str(f):
                    continue
                try:
                    mtime = f.stat().st_mtime
                except OSError:
                    continue
                old = snapshot.get(str(f))
                if old is None or mtime > old:
                    changed.append(f.name)
                    snapshot[str(f)] = mtime
            if changed:
                logger.info("🔁 [dev-reload] 检测到变动: %s → 重启中...", ", ".join(changed[:5]))
                time.sleep(0.5)  # 等待文件写入完成
                self._dev_restart()

    def _dev_restart(self):
        """重启进程: 优雅关闭 uvicorn 释放端口 → 等待端口释放 → 启动新进程 → 退出。

        M2 修复: 原实现直接 os._exit(0), 旧进程监听端口未释放时
        新进程 bind 失败 (Windows 上 uvicorn 端口占用报错)。
        """
        self._running = False  # 通知主循环停止
        # 请求 uvicorn 优雅退出 (释放监听 socket)
        if self._server:
            self._server.should_exit = True
        # 轮询等待端口释放 (最多 8s), 消除新进程绑定竞态
        deadline = time.time() + 8.0
        while time.time() < deadline:
            if not self._port_in_use(self.state.api_port):
                break
            time.sleep(0.2)
        import subprocess as _sp
        _sp.Popen([sys.executable] + sys.argv)
        logger.info("🔁 [dev-reload] 新进程已启动, 当前进程退出")
        time.sleep(0.3)  # 等待日志刷新
        os._exit(0)

    @staticmethod
    def _port_in_use(port: int) -> bool:
        """探测端口是否仍有进程监听 (重启前等待释放用)。"""
        import socket as _socket
        try:
            with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as s:
                s.settimeout(0.3)
                s.connect(("127.0.0.1", port))
            return True
        except OSError:
            return False
