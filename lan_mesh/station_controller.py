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
- 激活 Secretary 后同进程加载 ChatHandler/ProjectManager/ModelRouter/MCPGateway
- 停用 Secretary 后卸载上述组件, 回到纯基础设施管理模式
"""
import asyncio
import os
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
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import AppConfig, get_db_path, load_model_pool
from .database import Database
from .discovery import DiscoveryService
from .logger import get_logger

logger = get_logger("station")
from .host_info import load_or_create_device_id
from .shared_folder import SharedFolderManager
from .station_director import StationDirector
from .station_api import create_station_router
from .api import create_worker_router
from .skill_registry import SkillRegistry
from .bot_gateway import BotGateway
from .cloud_sync import CloudSyncManager
# 节点间 HTTP 调用 (iter-65 修复: 模块级导入, 供 submit_task_from_chat/
# _federation_forward_task/cancel_task/pause_task 等方法全局名引用;
# 此前仅方法内导入, 模块级调用会 NameError 被 except 静默吞掉)
from .http_retry import http_post

# iter-74: 职责域 mixin (拆分自本文件, 见 docs/reference/controller-split-plan.md)
# 组合顺序即 MRO 顺序; mixin 之间不互相继承, 仅通过 self 属性交互。
from .station_lifecycle import StationLifecycleMixin
from .station_selfheal import StationSelfHealMixin
from .station_secretary import StationSecretaryMixin
from .station_local_pm import StationLocalPmMixin
from .station_pm_control import StationPmControlMixin
from .station_scheduler import StationSchedulerMixin
from .station_sync import StationSyncMixin
from .station_hosts import StationHostsMixin


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


class StationController(
        StationLifecycleMixin,
        StationSelfHealMixin,
        StationSecretaryMixin,
        StationLocalPmMixin,
        StationPmControlMixin,
        StationSchedulerMixin,
        StationSyncMixin,
        StationHostsMixin,
):
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

        # iter-50: 自动自愈守护状态 (冷却表按类别 + 执行摘要, 端点/面板可查)
        self._auto_heal_last: dict[str, float] = {}
        self._auto_heal_state: dict = {"runs": 0, "last_run": 0.0,
                                       "last_actions": []}
        # iter-60: 写动作护栏状态 — 每日配额/连续失败熔断 (F4.2 全自动闭环)
        self._auto_heal_daily: dict[str, dict[str, int]] = {}  # 日期 → {类别: 次数}
        self._auto_heal_fused: dict[str, str] = {}             # 类别 → 熔断原因
        self._auto_heal_fail_streak: dict[str, int] = {}      # 类别 → 连续失败次数

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

        # 技能市场 (iter-61 F5.3 插件系统): 第三方 Skill 浏览/安装/卸载
        from .skill_market import SkillMarket
        market_dir = Path(self.cfg.skill_market_dir or "skills_market")
        if not market_dir.is_absolute():
            market_dir = Path(__file__).parent.parent / market_dir
        self.skill_market = SkillMarket(
            db=self.db,
            skills_dir=Path(skills_dir),
            market_dir=market_dir,
            max_size_kb=self.cfg.skill_max_size_kb,
        )

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
        self.model_router = None
        self._default_model = ""  # 全局默认模型 (model_pool.yaml)
        self._model_pool = None  # iter-55: 启动时预加载的模型池配置 (任何模式)
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
        from .shadow_dev import ShadowDevManager
        self.shadow_dev_manager = ShadowDevManager()
        from .workstation_optimizer import WorkstationOptimizationManager
        self.workstation_optimizer = WorkstationOptimizationManager(
            self.db, self.shadow_dev_manager,
            broadcast=self._queue_ws_broadcast)
        self._ws_push_event: Optional[asyncio.Event] = None  # 在 async 上下文中初始化
        self._ws_broadcast_queue: list = []  # 同步代码向 WS 队列塞事件

        # ── 优化12: 定期汇报 ──
        self._report_interval: float = 300.0  # 汇报间隔 (默认5分钟)
        self._last_report_time: float = 0.0
        self._server = None  # uvicorn Server 引用 (dev-reload 优雅重启用)

        # ── iter-54: 日志容量修剪节流 (补强#2) ──
        self._last_log_prune_ts: float = 0.0

        # ── F3.1: 自动扩缩容 ──
        self._autoscale_up_threshold: int = 2    # 队列积压 >= 2 时扩容
        self._autoscale_down_threshold: int = 0  # 队列清空时记录缩容观察

        # ── S2: 版本升级提醒 ──
        from .version_sync import UpgradeNotifier
        self._upgrade_notifier = UpgradeNotifier()   # 同目标同版本只通知一次
        self._version_behind_warned: set = set()     # 已提醒过的领先者 commit

        # ── F1: 角色无关自动对齐 ──
        # 密钥对齐: 周期线程 + 启动/新机/保存后触发, 主从无关按 config_ts 收敛
        self._align_interval: float = 60.0           # 对齐周期 (秒)
        # 版本对齐: 落后节点自动 git pull (工作区脏则跳过)
        self.auto_upgrade_enabled = bool(getattr(cfg, "auto_upgrade", True))
        self._upgrade_attempted: set = set()         # 已自动尝试过的领先者 commit

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

    def _load_model_resources(self) -> None:
        """iter-55: 任何 station 模式都预加载模型资源池 (LLM Key 注入)。

        让位主机 (网络中已有 Secretary, 本机未激活) 作为远程派发
        Worker 执行 PM 任务时同样需要模型 Key 就绪 — 与激活解耦:
        - 加载 model_pool.yaml → self._model_pool (activate_secretary 复用)
        - 加载 resources.yaml → 注入直填 API Key 环境变量
        幂等, 重复调用无害; 异常 no-op 不影响启动。
        """
        try:
            from .config import load_model_pool
            from .model_resources import (init_resource_manager,
                                          set_bot_notify_global)
            if self._model_pool is None:
                self._model_pool = load_model_pool()
            pool = self._model_pool
            resources_path = self._find_resources_path()
            if resources_path:
                init_resource_manager(
                    resources_path,
                    pool.models if pool.models else None,
                    self.db,
                )
                if self.bot_gateway:
                    set_bot_notify_global(self.bot_gateway.notify)
        except Exception as e:
            logger.warning("模型资源预加载失败 (no-op): %s", e)

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
        model_pool = self._model_pool or load_model_pool()
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

        # iter-41: 任务停滞主动告警守护 (基于 task_flow 追踪, 异常不影响激活)
        # iter-43: 检查周期/阈值改由 config.yaml observability 段驱动 (≤0 禁用)
        try:
            from .runtime_trace import set_stall_bot_notify, start_stall_watcher
            if self.bot_gateway:
                set_stall_bot_notify(self.bot_gateway.notify)
            obs = self.cfg.observability
            start_stall_watcher(interval=obs.stall_check_interval,
                                stall_minutes=obs.stall_minutes)
        except Exception as e:
            logger.warning("任务停滞检测启动失败 (no-op): %s", e)

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
        # F1: 激活后立即与在线节点对齐密钥 (角色无关, 兜底启动同步时机差)
        try:
            threading.Thread(target=self._align_config_with_peers,
                             name="station-secret-align", daemon=True).start()
        except Exception as e:
            logger.warning("[F1] 激活后密钥对齐启动失败: %s", e)

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
        """iter-53: 启动时恢复中断任务 — 有快照自动续跑, 无快照标 interrupted。

        系统重启后, 之前处于 running/monitoring/planning/executing 的任务
        其 PM Agent 进程已不存在:
        - 存在执行态快照 (pm_snapshots) → 自动重建 PM 从断点续跑
          (保留已完成子任务输出, 重新分发未完成部分)
        - 无快照 → 标记 interrupted, 用户可在 UI 中看到并决定重新提交或删除
        """
        stale_statuses = ("running", "monitoring", "planning", "executing", "awaiting_input", "paused")
        recovered = 0
        for status in stale_statuses:
            try:
                tasks = self.db.list_tasks(status=status, limit=100)
                for task in tasks:
                    # iter-53: 有快照 → 自动续跑
                    if self.db.get_pm_snapshot_by_task(task.task_id):
                        result = self._local_resume_pm(task.task_id)
                        if result.get("ok"):
                            logger.info("任务断点恢复: %s 已从快照续跑 (pm=%s)",
                                        task.task_id[:16], result["pm_id"][:12])
                            recovered += 1
                            continue
                        logger.warning("任务断点恢复失败: %s → %s",
                                       task.task_id[:16], result.get("message"))
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
            logger.info("任务断点恢复: %d 个中断任务已处理 (快照续跑或标记 interrupted)", recovered)

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

    def _converge_mesh_token(self, target_ip: str = "", target_port: int = 0):
        """S1: 从 Secretary 拉取 mesh token 收敛加密信任根。

        Station 间无注册链路 (注册仅 Worker 用), 非 Secretary 节点
        在此处引导拉取并持久化; 与注册下发同一信任假设 (LAN 成员)。
        E4: 支持直接传入目标 (冲突仲裁时 DB 角色可能尚未刷新)。
        """
        from .auth import save_mesh_token
        from .http_retry import http_get, set_auth_token
        ip, port = (target_ip or "").strip(), int(target_port or 0)
        if not ip or not port:
            for h in self.db.list_hosts():
                if (getattr(h, "device_id", "") != self.state.device_id
                        and getattr(h, "role", "") == "secretary"
                        and getattr(h, "online", False)
                        and getattr(h, "ip", "") and getattr(h, "api_port", 0)):
                    ip, port = h.ip, h.api_port
                    break
        if not ip or not port:
            return
        try:
            resp = http_get(
                f"http://{ip}:{port}"
                "/api/station/bootstrap-token", timeout=10)
            token = (resp.json() or {}).get("mesh_token", "")
            if token and token != self._mesh_token:
                if save_mesh_token(token):
                    self._mesh_token = token
                    set_auth_token(token if self._mesh_auth_enabled else "")
                    logger.info("[S1] mesh token 已收敛 (拉取自 Secretary %s)", ip)
        except Exception as e:
            logger.warning("[S1] mesh token 收敛失败: %s", e)

    def _yield_secretary_to(self, peer_name: str, ip: str, port: int):
        """E4: 向仲裁优先级更高 (device_id 更小) 的节点让位 Secretary。

        选举 5s 窗口丢包/时机错开会致双 Secretary 脑裂 (各自生成
        mesh_token, 密钥互推解密失败)。双端对称规则: device_id
        字典序较大者降级。让位后收敛 token 并拉取资源密钥,
        保证加密信任根与配置全网一致。
        """
        if getattr(self, "_secretary_yielded", False):
            return
        self._secretary_yielded = True
        logger.warning("[E4] Secretary 冲突: %s (%s) 仲裁优先, 本站让位为 Station 模式",
                       peer_name, ip)
        try:
            self.deactivate_secretary()
        except Exception as e:
            logger.error("[E4] Secretary 让位失败: %s", e)
            return
        try:
            self._queue_ws_broadcast("secretary_yielded", {
                "message": "Secretary 冲突仲裁: 本站已让位, %s 接管" % peer_name,
                "peer": peer_name,
            })
        except Exception:
            pass

        def _converge_after_yield():
            time.sleep(2)  # 等对端服务稳定
            try:
                self._converge_mesh_token(target_ip=ip, target_port=port)
            except Exception as e:
                logger.warning("[E4] 让位后 token 收敛失败: %s", e)
            try:
                self.pull_resource_secrets(ip, port)
            except Exception as e:
                logger.warning("[E4] 让位后密钥拉取失败: %s", e)

        threading.Thread(target=_converge_after_yield,
                         name="station-yield-converge", daemon=True).start()

    def _find_existing_secretary(self) -> str:
        """查找网络中已存在的在线 Secretary。返回设备名或空字符串。

        F3.4 (iter-64): 仅考虑本网段主机 (source=lan) — 联邦远端
        Secretary 不参与本网段仲裁, 各网段 Secretary 联邦共存。
        """
        hosts = self.db.list_hosts(source="lan")
        for h in hosts:
            if (getattr(h, 'device_id', '') != self.state.device_id
                    and getattr(h, 'role', '') == 'secretary'
                    and getattr(h, 'online', False)):
                return getattr(h, 'device_name', h.device_id[:8])
        return ""

    # ── E5: Secretary Failover ─────────────────────────────────

    def _secretary_failover_check(self):
        """E5: Secretary 离线接管检查 (由 _prune_loop 每轮调用)。

        选举只在启动时进行 (First-Station-Wins), Secretary 宕机后无人接管。
        本检查在 Secretary 超时离线且网络无其他在线 Secretary 时,
        由 device_id 字典序最小的在线 Station 接任 — 与 E4 同一对称
        仲裁规则, 多节点并发接管亦自然收敛 (双 Secretary 出现时
        _on_device_seen 的让位逻辑兼容裁决)。
        """
        if self.secretary_active:
            return
        # F3.4 (iter-64): 仅本网段主机参与仲裁 (联邦远端 Secretary 隔离)
        hosts = self.db.list_hosts(source="lan")
        for h in hosts:
            if (getattr(h, "device_id", "") != self.state.device_id
                    and getattr(h, "role", "") == "secretary"
                    and getattr(h, "online", False)):
                return  # 仍有在线 Secretary, 无需接管
        # 候选集 = 本站 + 全部在线非 Secretary 主机
        candidate_ids = [self.state.device_id]
        for h in hosts:
            if (getattr(h, "device_id", "") != self.state.device_id
                    and getattr(h, "online", False)
                    and getattr(h, "role", "") != "secretary"):
                candidate_ids.append(h.device_id)
        if min(candidate_ids) != self.state.device_id:
            return  # 存在仲裁优先级更高 (device_id 更小) 的在线 Station
        try:
            self.activate_secretary()
        except Exception as e:
            logger.error("[E5] Secretary failover 激活失败: %s", e)
            return
        logger.warning("[E5] Secretary failover: 原 Secretary 离线, 本站接管 "
                       "(device_id 仲裁最小)")
        try:
            self._queue_ws_broadcast("secretary_failover", {
                "message": "Secretary 离线故障转移: 本站接管",
                "device_id": self.state.device_id,
            })
        except Exception:
            pass
        try:
            self.bot_gateway.notify("secretary_failover", {
                "device": self.state.device_name,
            })
        except Exception:
            pass

    # ── F3.4 (iter-64): 跨网段联邦 ────────────────────────────

    def _federation_sync_peer(self, peer) -> int:
        """同步单个联邦对端: 拉取对方与网段主机写入 DB (source=fed)。

        返回 1 成功 / 0 失败。对端自身与其网段全部主机均标记
        source=fed + federation=peer.name, 与 UDP 发现的本网段主机
        (source=lan) 完全隔离 (选举/仲裁互不干扰)。
        """
        import requests as _requests
        from .protocol import HostRecord

        port = int(peer.port or self.state.api_port)
        url = f"http://{peer.host}:{port}/api/federation/info"
        headers = {}
        if getattr(self, "_mesh_auth_enabled", False) and getattr(self, "_mesh_token", ""):
            headers["Authorization"] = f"Bearer {self._mesh_token}"
        try:
            resp = _requests.get(url, headers=headers, timeout=8)
            if resp.status_code != 200:
                return 0
            info = resp.json() or {}
        except Exception as e:
            logger.debug("[F3.4] 联邦对端 %s 拉取失败: %s", peer.name, e)
            return 0

        now = time.time()
        records: list[HostRecord] = []
        # 对端自身
        records.append(HostRecord(
            device_id=info.get("device_id", ""),
            device_name=info.get("device_name", "") or peer.name,
            role=info.get("role", "station"),
            ip=peer.host,
            api_port=port,
            online=True,
            registered_at=now,
            last_seen=now,
            code_version=info.get("code_version", ""),
            version_ts=info.get("version_ts", 0.0),
            source="fed",
            federation=peer.name,
        ))
        # 对端网段主机 (转播其本地视图, 含其自身联邦转发)
        for hd in (info.get("hosts") or []):
            if hd.get("device_id") == self.state.device_id:
                continue  # 不回写自己
            if hd.get("device_id") == info.get("device_id"):
                continue  # iter-65 修复: 对端自身已按 peer.host 入库, 防被报告 ip 覆盖
            try:
                records.append(HostRecord(
                    device_id=hd.get("device_id", ""),
                    device_name=hd.get("device_name", ""),
                    role=hd.get("role", "station"),
                    hostname=hd.get("hostname", ""),
                    platform=hd.get("platform", ""),
                    ip=hd.get("ip", ""),
                    api_port=hd.get("api_port", 0),
                    cpu_count=hd.get("cpu_count", 0),
                    memory_total_mb=hd.get("memory_total_mb", 0),
                    disk_total_gb=hd.get("disk_total_gb", 0),
                    cpu_percent=hd.get("cpu_percent", 0.0),
                    memory_percent=hd.get("memory_percent", 0.0),
                    disk_percent=hd.get("disk_percent", 0.0),
                    shared_folder=hd.get("shared_folder", ""),
                    online=bool(hd.get("online", True)),
                    registered_at=now,
                    last_seen=now,
                    rating_tier=hd.get("rating_tier", ""),
                    rating_score=hd.get("rating_score", 0),
                    rating_summary=hd.get("rating_summary", ""),
                    code_version=hd.get("code_version", ""),
                    version_ts=hd.get("version_ts", 0.0),
                    source="fed",
                    federation=peer.name,
                ))
            except Exception:
                continue
        for rec in records:
            if not rec.device_id:
                continue
            try:
                self.db.upsert_host(rec)
            except Exception as e:
                logger.warning("[F3.4] 联邦主机入库失败 %s: %s", rec.device_id, e)
        return 1

    def _federation_loop(self):
        """F3.4 (iter-64): 跨网段联邦轮询线程。

        周期拉取全部配置对端; 连续 offline_after 次失败将该联邦
        主机集合置离线。配置变更需重启生效 (静态联邦 v1)。
        """
        cfg = self.cfg.federation
        failures: dict[str, int] = {}
        logger.info("[F3.4] 联邦轮询启动: %d 个对端, 间隔 %ds",
                    len(cfg.peers), cfg.interval)
        while self._running:
            for peer in cfg.peers:
                ok = self._federation_sync_peer(peer)
                failures[peer.name] = 0 if ok else failures.get(peer.name, 0) + 1
                if failures[peer.name] >= cfg.offline_after:
                    try:
                        for h in self.db.list_hosts(source="fed"):
                            if h.federation == peer.name and h.online:
                                self.db.set_offline(h.device_id)
                    except Exception:
                        pass
                    logger.warning("[F3.4] 联邦对端 %s 连续 %d 次不可达, 已标记离线",
                                   peer.name, failures[peer.name])
            try:
                time.sleep(max(1, int(cfg.interval)))
            except Exception:
                return

    def submit_task_from_chat(self, name: str, description: str, created_by: str = "secretary",
                              priority: str = "normal",
                              fed_relay: bool = False) -> dict:
        """从秘书对话直接提交任务并分配 PM Agent。

        与 station_api.submit_task() 逻辑一致, 但同步执行。
        优化13: 支持优先级 + 负载感知选站。

        Args:
            priority: 优先级 (low / normal / high / urgent)
            fed_relay: iter-65 联邦防环 — 本任务已是跨网段委托任务
                (对端转来), 本侧选站再命中联邦主机时不再回传,
                直接失败终止 (防 A↔B 互相委托死循环)
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
        # iter-65: 联邦防环标记落盘 (审计可见)
        if fed_relay:
            task.input_data["_federation_relay"] = True
        # F4.4 (iter-52): 成本感知调度 — 预算预估 + 适配检查 (异常静默, 不阻断)
        try:
            from .budget_advisor import build_task_cost_estimate
            cost_est = build_task_cost_estimate(
                name, description, self.db, project_id="",
                project_manager=self.project_manager)
            task.input_data["_cost_estimate"] = cost_est
            fit = cost_est.get("budget_fit", {})
            if fit.get("status") in ("tight", "insufficient"):
                self.bot_gateway.notify("cost_budget_warning", {
                    "name": name, "task_id": task.task_id[:8],
                    "estimated": cost_est["estimated_tokens"],
                    "status": fit.get("label", fit.get("status", "")),
                    "advice": fit.get("advice", ""),
                })
                self._queue_ws_broadcast("cost_budget_warning", {
                    "task_id": task.task_id, "name": name,
                    "estimated_tokens": cost_est["estimated_tokens"],
                    "status": fit.get("status", "unknown"),
                    "advice": fit.get("advice", ""),
                })
        except Exception:
            pass  # 预估失败不影响任务提交
        self.db.save_task(task)
        logger.info("对话提交任务: %s (%s) 优先级=%s", task.task_id, name, priority)
        # WS 广播: 通知前端任务面板刷新
        self._queue_ws_broadcast("task_submitted", task.to_dict())
        # P3: 任务流追踪 — 提交阶段点 (异常静默)
        try:
            from . import runtime_trace
            runtime_trace.trace_task_event(
                task.task_id, "submitted",
                detail=f"{name} (created_by={created_by}, 优先级={priority})")
        except Exception:
            pass

        # 选择在线 work_station (优化13: 评级 + 负载感知; iter-65: lan 优先 fed 兜底)
        hosts = self.db.list_hosts()
        online_hosts = [h for h in hosts if h.online and h.device_id != self.state.device_id]
        if not online_hosts:
            online_hosts = [h for h in hosts if h.online]

        if not online_hosts:
            task.status = "failed"
            task.output_data = {"error": "无可用 work_station"}
            self.db.save_task(task)
            return task.to_dict()

        # iter-65 (F3.4 联邦任务转发): 选站分层 — 本网段优先, 联邦兜底
        target_host = self._pick_task_host(online_hosts)

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
                    "task_id": task.task_id,   # iter-66 (Bug B): F3.3 迁移依赖
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

        # iter-65 (F3.4 联邦任务转发): 目标为联邦主机时经对端 Secretary 委托执行
        # (网段内主机 IP 跨网段不可达; 对端全权接管, 本侧标记 forwarded)
        if target_host and getattr(target_host, "source", "lan") == "fed":
            if fed_relay:
                # 防环: 委托任务不再回传 (跳数上限 1), 本侧无法执行直接失败
                task.status = "failed"
                task.output_data = {"error": "联邦委托已达跳数上限 (防环), 本侧暂无法执行"}
                self.db.save_task(task)
                logger.warning("联邦委托防环终止: %s (本侧仅联邦主机可达且本机 PM 忙)",
                               task.task_id)
                return task.to_dict()
            self._federation_forward_task(task, target_host)
            return task.to_dict()

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
                    "task_id": task.task_id,   # iter-66 (Bug B): F3.3 迁移依赖
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

    def _pick_task_host(self, online_hosts: list):
        """iter-65 (F3.4 联邦任务转发): 任务目标选站 — 本网段优先, 联邦兜底。

        本网段主机 (source=lan) 按评级+负载排序优先; 无本网段主机时
        退回联邦主机, 且仅限 role=secretary 的对端 (跨网段场景下只有
        peer.host 可达, 对端网段内主机 IP 不可达)。返回选中的 HostRecord。
        """
        lan_hosts = [h for h in online_hosts if getattr(h, "source", "lan") != "fed"]
        fed_hosts = [h for h in online_hosts if getattr(h, "source", "lan") == "fed"
                     and getattr(h, "role", "") == "secretary"]
        if not fed_hosts:  # 无对端 Secretary 时退化为任意 fed 主机 (尽力而为)
            fed_hosts = [h for h in online_hosts if getattr(h, "source", "lan") == "fed"]

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

        lan_hosts.sort(key=_host_sort_key)
        fed_hosts.sort(key=_host_sort_key)
        return (lan_hosts or fed_hosts)[0]

    def _federation_forward_task(self, task, target_host) -> bool:
        """iter-65 (F3.4 联邦任务转发): 将任务委托给对端 Secretary 网段执行。

        对端调用其本网段选站流程, 全权接管任务生命周期; 本侧标记
        forwarded (output_data 记录委托目标)。成功返回 True。
        """
        try:
            # iter-65 防环: 转发的任务数据带 _federation_relay 标记 (跳数上限 1)
            fwd_data = task.to_dict()
            fwd_input = fwd_data.setdefault("input_data", {}) or {}
            fwd_input["_federation_relay"] = True
            fwd_data["input_data"] = fwd_input
            resp = http_post(
                f"http://{target_host.ip}:{target_host.api_port}/api/federation/tasks/forward",
                json={
                    "task_data": fwd_data,
                    "forwarded_from": self.state.device_id,
                    "federation": getattr(target_host, "federation", ""),
                },
                timeout=15,
            )
            if resp.status_code == 200:
                task.status = "forwarded"
                task.output_data = {
                    "forwarded_to": target_host.device_id,
                    "federation": getattr(target_host, "federation", ""),
                }
                self.db.save_task(task)
                self._queue_ws_broadcast("task_updated", task.to_dict())
                self.bot_gateway.notify("task_forwarded", {
                    "task": task.name, "task_id": task.task_id[:8],
                    "target": target_host.device_name or target_host.hostname,
                    "federation": getattr(target_host, "federation", ""),
                })
                logger.info("联邦委托任务: %s → %s (%s)",
                            task.task_id, target_host.device_id[:8],
                            getattr(target_host, "federation", ""))
                return True
            task.status = "failed"
            task.output_data = {"error": f"联邦委托失败: {resp.text[:200]}"}
        except Exception as e:
            task.status = "failed"
            task.output_data = {"error": f"联邦委托异常: {e}"}
        self.db.save_task(task)
        return False

    # ── 内嵌 Worker: 本机 PM Agent 管理 ─────────────────────

    def _local_start_pm(self, task_id: str, secretary_url: str, task_data: dict = None) -> dict:
        """在本机 Station 进程内直接启动 PM Agent (无需 Worker)。"""
        if self._local_pm_agent and getattr(self._local_pm_agent, '_running', False):
            return {"ok": False, "message": "本机 PM Agent 已在运行"}
        # iter-55: 让位主机 (网络中已有 Secretary, 本机未激活) 也可执行
        # 远程派发的 PM 任务 — 惰性初始化专用 AgentRuntime, 与激活解耦
        if not self.chat_runtime:
            from .agent_runtime import AgentRuntime
            self.chat_runtime = AgentRuntime(
                agent_id=f"worker-{self.state.device_id[:8]}",
                shared_folder_path=str(self.state.shared_folder.path),
            )
            logger.info("让位主机惰性初始化 Worker AgentRuntime (%s)",
                        self.state.device_name)

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

    def _dispatch_queued_task(self) -> bool:
        """iter-57 (补强#5): PM 空闲后接力派发队列中最早 pending 任务。

        并发提交时本机 PM 忙 → 任务排队 (pending); 本机 PM 完成后调用
        本方法接力派发。若 PM 仍在收尾 (completed 上报时线程尚未退出),
        后台线程等待其结束后再派发, 避免请求线程内自调用阻塞
        (压测发现 20 并发提交时 19 个任务瞬时 failed + ReadTimeout)。
        """
        pm = self._local_pm_agent
        if pm and getattr(pm, '_running', False):
            # PM 仍在收尾: 后台等待空闲后接力 (最多 120s), 防重入
            if not getattr(self, '_queued_dispatch_waiting', False):
                self._queued_dispatch_waiting = True

                def _wait_then_dispatch():
                    deadline = time.time() + 120
                    while time.time() < deadline:
                        time.sleep(2)
                        cur = self._local_pm_agent
                        if not cur or not getattr(cur, '_running', False):
                            break
                    self._queued_dispatch_waiting = False
                    self._dispatch_queued_task()

                threading.Thread(target=_wait_then_dispatch,
                                 daemon=True, name="queued-dispatch").start()
            return False

        tasks = self.db.list_tasks()
        pending = [t for t in tasks if getattr(t, 'status', '') == 'pending']
        if not pending:
            return False
        # iter-66 (Bug D 修复): FIFO — 取最早提交的任务 (list_tasks 为 DESC)
        task = min(pending, key=lambda t: getattr(t, 'created_at', 0) or 0)
        secretary_url = f"http://127.0.0.1:{self.state.api_port}"
        result = self._local_start_pm(task.task_id, secretary_url, task.to_dict())
        if not result.get("ok"):
            logger.warning("排队任务接力派发失败: %s", result.get("message"))
            return False

        pm_id = result["pm_id"]
        task.pm_agent_id = pm_id
        task.status = "running"
        self.db.save_task(task)
        self._register_local_pm(pm_id, task)
        logger.info("排队任务已接力派发: %s (%s) → PM %s",
                    task.task_id[:8], task.name, pm_id[:8])
        return True

    def _register_local_pm(self, pm_id: str, task) -> None:
        """本机 PM 启动后的统一登记 (落库 + 映射 + WS 广播)。

        iter-69 (Bug L 修复): 接力派发与 F3.3 本机接管共用此登记, 避免
        接管路径漏登记导致 PM 查不到、无法取消、二次迁移失联。
        """
        from .protocol import PMAgent
        self.db.upsert_pm_agent(PMAgent(
            pm_id=pm_id,
            agent_name=f"PM-{pm_id[:8]}",
            task_id=task.task_id,
            project_id=getattr(task, 'project_id', '') or '',
            device_id=self.state.device_id,
            hostname=self.state.device_name,
            ip="127.0.0.1",
            api_port=self.state.api_port,
            status="starting",
        ))
        self._pm_worker_map[pm_id] = {
            "ip": "127.0.0.1",
            "api_port": self.state.api_port,
            "device_id": self.state.device_id,
            "local": True,
            "task_id": task.task_id,   # iter-66 (Bug B): F3.3 迁移依赖
        }
        self._queue_ws_broadcast("pm_registered", {
            "pm_id": pm_id, "task_id": task.task_id,
            "device_id": self.state.device_id,
            "device_name": self.state.device_name,
        })
        self._queue_ws_broadcast("task_updated", task.to_dict())

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

    def _local_resume_pm(self, task_id: str) -> dict:
        """iter-53: 从执行态快照恢复中断的 PM Agent (断点续跑)。

        重建 PM Agent 实例并注入快照状态, 保留已完成子任务输出,
        重新分发未完成部分; 任务状态重置为 running。
        """
        if self._local_pm_agent and getattr(self._local_pm_agent, '_running', False):
            return {"ok": False, "message": "本机 PM Agent 正在运行, 无法恢复其他任务"}
        # iter-55: 与 _local_start_pm 一致 — 让位主机惰性初始化 runtime
        if not self.chat_runtime:
            from .agent_runtime import AgentRuntime
            self.chat_runtime = AgentRuntime(
                agent_id=f"worker-{self.state.device_id[:8]}",
                shared_folder_path=str(self.state.shared_folder.path),
            )

        snapshot = self.db.get_pm_snapshot_by_task(task_id)
        if not snapshot:
            return {"ok": False, "message": "无执行态快照, 无法恢复"}

        task = self.db.get_task(task_id)
        if not task:
            return {"ok": False, "message": "任务不存在"}

        from .pm_agent import ProjectManagerAgent
        pm_id = snapshot["pm_id"]
        secretary_url = f"http://127.0.0.1:{self.state.api_port}"

        self._local_pm_agent = ProjectManagerAgent(
            pm_id=pm_id,
            agent_runtime=self.chat_runtime,
            secretary_url=secretary_url,
            device_id=self.state.device_id,
            device_name=self.state.device_name,
        )
        if not self._local_pm_agent.resume_from_snapshot(snapshot):
            self._local_pm_agent = None
            return {"ok": False, "message": "快照解析失败, 无法恢复"}

        # 任务状态重置为 running (恢复线程内会继续上报进度)
        task.status = "running"
        task.pm_agent_id = pm_id
        self.db.save_task(task)
        logger.info("本机 PM Agent 已从快照恢复: %s, 任务: %s", pm_id, task_id)
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
            # iter-66 (Bug H): 清理映射, 防止 busy 误判
            self._pm_worker_map.pop(pm_id, None)
            logger.info("本机任务已取消: %s", task_id)
            self.bot_gateway.notify("task_cancelled", {"task_id": task_id, "name": task.name})
            return {"ok": True, "message": "任务已取消"}

        ip = worker.get("ip", "")
        port = worker.get("api_port", 0)
        if not ip or not port:
            return {"ok": False, "message": "Worker 地址信息不完整"}

        try:
            # iter-66 (Bug G): 认证启用时远程 stop/cancel 端点同样要求 Bearer
            from .http_retry import auth_headers
            resp = http_post(
                f"http://{ip}:{port}/role/cancel-pm",
                headers=auth_headers(),
                retries=1,
                timeout=10,
            )
            if resp.status_code == 200:
                self.db.update_task_status(task_id, "cancelled")
                self.db.update_pm_status(pm_id, "cancelled")
                # iter-66 (Bug H): 清理映射, 否则 _is_worker_busy 误判
                # Worker 仍忙 → F3.1 自动扩容永不派发到该 Worker
                self._pm_worker_map.pop(pm_id, None)
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
            # iter-66 (Bug G 同类): 认证启用时远程 pause 端点同样要求 Bearer
            from .http_retry import auth_headers
            resp = http_post(
                f"http://{ip}:{port}/role/pause-pm",
                headers=auth_headers(),
                retries=1,
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

    # ── DAG 图结构读写 (iter-51, F4.3) ────────────────────────────

    def get_task_graph_data(self, task_id: str) -> Optional[dict]:
        """读取任务的 DAG 图结构 JSON (checkpoint 优先, 其次子任务列表重建)。

        Args:
            task_id: 任务 ID

        Returns:
            {"nodes": [...], "edges": [...]} 或 None (任务无图数据)
        """
        import json as _json
        from .task import SubTask, TaskDAG
        ckpt = self.db.get_latest_checkpoint(task_id)
        if ckpt:
            try:
                dag_data = _json.loads(ckpt.get("dag_json", "{}"))
                if dag_data.get("nodes"):
                    return dag_data
            except (ValueError, TypeError):
                pass
        task = self.db.get_task(task_id)
        if task and task.subtasks:
            subtasks = [SubTask.from_dict(st) for st in task.subtasks]
            return TaskDAG(subtasks).to_graph_json()
        return None

    def update_task_graph(self, task_id: str, graph_data: dict) -> dict:
        """保存编辑后的任务 DAG 图结构 (前端编辑器 / 自然语言编辑回写)。

        校验: 任务存在 + 仅 pending 状态可编辑 + 环检测拒绝。
        落盘: 子任务列表 + checkpoint dag_json 同步 (保持 GET 读取路径一致)。

        Args:
            task_id: 任务 ID
            graph_data: {"nodes": [...], "edges": [...]}

        Returns:
            {"ok": bool, "message": str}
        """
        import json as _json
        from .task import TaskDAG
        task = self.db.get_task(task_id)
        if not task:
            return {"ok": False, "message": f"任务 {task_id} 不存在"}
        if task.status != "pending":
            return {"ok": False, "message":
                    f"任务状态 {task.status} 不可编辑 (仅 pending 可编辑)"}
        new_dag = TaskDAG.from_graph_json(graph_data)
        if new_dag.has_cycle():
            return {"ok": False, "message": "图结构存在循环依赖, 无法保存"}
        # 落盘子任务列表
        task.subtasks = [st.to_dict() for st in new_dag.to_subtask_list()]
        self.db.save_task(task)
        # 同步最新 checkpoint 的 dag_json (若存在)
        ckpt = self.db.get_latest_checkpoint(task_id)
        if ckpt:
            try:
                self.db.save_checkpoint(
                    ckpt.get("checkpoint_id", ""),
                    task_id,
                    ckpt.get("phase", "edited"),
                    _json.dumps(new_dag.to_graph_json(), ensure_ascii=False),
                    ckpt.get("context_json", "{}"),
                    ckpt.get("history_json", "{}"),
                )
            except Exception:
                pass  # checkpoint 同步失败不阻断主流程
        logger.info("任务图结构已更新: %s (%d 节点)",
                    task_id, len(new_dag.subtasks))
        return {"ok": True,
                "message": f"DAG 图结构已更新 ({len(new_dag.subtasks)} 节点)"}


    # ── F3.3: PM Agent 故障迁移 ─────────────────────────────────

    def _migrate_orphaned_pms(self, gone_device_ids: list[str]):
        """F3.3: 检测离线主机上的 PM, 将其任务迁移到可用节点或本机接管。

        策略:
        1. 扫描 _pm_worker_map, 找出 device_id 在 gone_ids 中的 PM
        2. 查找该 PM 关联的任务, 重置为 pending
        3. 优先派发到其他空闲在线 Worker, 否则本机接管
        """
        gone_set = set(gone_device_ids)
        orphaned_pms = []

        for pm_id, info in list(self._pm_worker_map.items()):
            if info.get("device_id") in gone_set:
                orphaned_pms.append((pm_id, info))

        if not orphaned_pms:
            return

        logger.warning("[F3.3] 检测到 %d 个 PM 因主机离线而孤立", len(orphaned_pms))

        # 查找可用替代 Worker (在线 + 排除离线 + 排除忙碌, iter-66 修复)
        hosts = self.db.list_hosts()
        available_workers = [
            h for h in hosts
            if getattr(h, 'online', False)
            and getattr(h, 'role', '') == 'worker'
            and getattr(h, 'device_id', '') not in gone_set
            and not self._is_worker_busy(h)
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
                    # iter-66 修复 (Bug A): Database 无 upsert_task, save_task
                    # 自带 ON CONFLICT DO UPDATE 的 upsert 语义
                    self.db.save_task(task)
                    logger.info("[F3.3] 任务 %s 已重置为 pending", task_id[:8])

                    # 尝试迁移
                    if available_workers:
                        target = available_workers[0]
                        ok = self._dispatch_task_to_worker(task, target)
                        logger.info("[F3.3] 任务已迁移到 %s (派发%s)",
                                   getattr(target, 'device_name', ''),
                                   "成功" if ok else "失败")
                    else:
                        # 本机接管
                        logger.info("[F3.3] 无可用 Worker, 本机接管任务 %s", task_id[:8])
                        if not self._start_local_pm_for_task(task_id):
                            # iter-69 (Bug L): 接管失败任务留在 pending,
                            # 由自动扩容/接力派发下一轮兜底重试
                            logger.warning("[F3.3] 接管失败, 任务 %s 留在 "
                                           "pending 等待下轮派发", task_id[:8])

            # 清理映射
            del self._pm_worker_map[pm_id]

    def _start_local_pm_for_task(self, task_id: str) -> bool:
        """F3.3: 本机启动 PM Agent 接管指定任务。

        iter-69 (Bug L 修复): 原实现自行构造 ProjectManagerAgent 且用的是
        早期签名 (task=/runtime=), 真机接管必然 TypeError —— 七节点实压日志
        "[F3.3] 本机接管失败: ProjectManagerAgent.__init__() got an
        unexpected keyword argument 'task'", 任务停在 pending 无人接管。
        改为复用唯一入口 `_local_start_pm` (与接力派发同路径, 带 runtime
        懒初始化 + start_task 真正跑起来), 并补齐 PM 落库 + 映射登记。
        """
        task = self.db.get_task(task_id)
        if not task:
            logger.warning("[F3.3] 本机接管失败: 任务不存在 %s", task_id[:8])
            return False

        secretary_url = f"http://127.0.0.1:{self.state.api_port}"
        try:
            result = self._local_start_pm(task_id, secretary_url,
                                          task.to_dict())
        except Exception as e:
            logger.error("[F3.3] 本机接管失败: %s", e)
            return False
        if not result.get("ok"):
            logger.error("[F3.3] 本机接管失败: %s", result.get("message"))
            return False

        pm_id = result["pm_id"]
        task.pm_agent_id = pm_id
        task.status = "running"
        self.db.save_task(task)
        self._register_local_pm(pm_id, task)
        logger.info("[F3.3] 本机 PM 已接管任务 %s → PM %s",
                    task_id[:8], pm_id[:8])
        return True

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

        # 扩容: 队列积压且有可用 Worker (iter-67 Bug J: 门槛 >= 1)
        # iter-68: 同轮连续派发清空积压 — 原 30s/轮 × N 积压滞后严重
        # (五节点实测 4 积压需 120s); 每次派发后重查队列与空闲 Worker,
        # 派发失败立即停止本轮防死循环 (30s 后下轮再试)
        while queue_depth >= 1 and idle_workers:
            target = idle_workers[0]
            logger.info("[自动扩容] 队列=%d, 激活 Worker: %s",
                       queue_depth, getattr(target, 'device_name', ''))
            ok = self._dispatch_next_task_to_worker(target)
            new_depth = len([t for t in self.db.list_tasks()
                             if getattr(t, 'status', '')
                             in ('pending', 'queued')])
            if not ok or new_depth >= queue_depth:
                # 派发失败或队列未减: 停止本轮 (防死循环)
                break
            queue_depth = new_depth
            hosts = self.db.list_hosts()
            online_workers = [h for h in hosts
                              if getattr(h, 'online', False)
                              and getattr(h, 'role', '') == 'worker']
            idle_workers = [h for h in online_workers
                            if not self._is_worker_busy(h)]

        # 缩容日志 (仅记录, 不主动关闭 Worker)
        if queue_depth == 0 and active_count == 0 and online_workers:
            logger.debug("[缩容观察] 无活跃任务, %d 台 Worker 空闲", len(online_workers))

    def _is_worker_busy(self, host) -> bool:
        """F3.1: 检查 Worker 是否正在执行任务。"""
        device_id = getattr(host, 'device_id', '')
        # 检查是否有活跃 PM 在该 Worker 上
        for pm_info in self._pm_worker_map.values():
            if pm_info.get("device_id") == device_id:
                return True
        return False

    def _dispatch_task_to_worker(self, task, worker_host) -> bool:
        """iter-66 (F3.1/F3.3 修复): 将指定任务派发到指定 Worker 并更新任务状态。

        成功: 置 running + 落库 + 记录 PM→Worker 映射 (含 task_id, F3.3 迁移依赖);
        失败: 返回 False (任务状态由调用方决定, 避免误置 failed 影响迁移重试)。
        """
        task_id = getattr(task, 'task_id', '')
        ip = getattr(worker_host, 'ip', '')
        port = getattr(worker_host, 'api_port', 0)

        if not ip or not port:
            return False

        # 修复 (任务③): 远程 PM 回报地址用本机对目标可达的 IP, 非 127.0.0.1
        from .host_info import pick_reachable_ip
        reach_ip = pick_reachable_ip(ip)
        sec_url = (
            f"http://{reach_ip}:{self.state.api_port}"
            if reach_ip else f"http://127.0.0.1:{self.state.api_port}"
        )
        try:
            # iter-66 (Bug E): 节点间认证启用时 Worker 端 /role/start-pm
            # 要求 Bearer mesh_token, 不带认证头会 401 静默失败
            from .http_retry import auth_headers
            resp = http_post(
                f"http://{ip}:{port}/role/start-pm",
                json={"task_id": task_id, "secretary_url": sec_url,
                      # iter-66 (Bug F): 任务仅存于 Secretary DB, Worker 端
                      # 无法从本地 DB 获取 → 缺 task_data 时 409「无法获取任务详情」
                      "task_data": task.to_dict()},
                headers=auth_headers(),
                timeout=10,
            )
            if resp.status_code == 200:
                pm_data = resp.json()
                pm_id = pm_data.get("pm_id", "")
                task.pm_agent_id = pm_id
                task.status = "running"
                self.db.save_task(task)
                if pm_id:
                    # iter-66 修复 (Bug B): 补 task_id 键, F3.3 迁移依赖
                    self._pm_worker_map[pm_id] = {
                        "ip": ip,
                        "api_port": port,
                        "device_id": getattr(worker_host, 'device_id', ''),
                        "task_id": task_id,
                    }
                    # iter-67 (Bug K): 与其余 5 处派发路径对齐, 落 pm_agents 表
                    # (运维查询任务承载 + 脚本 victim 定位依赖该表)
                    from .protocol import PMAgent
                    self.db.upsert_pm_agent(PMAgent(
                        pm_id=pm_id,
                        agent_name=f"PM-{pm_id[:8]}",
                        task_id=task_id,
                        project_id=getattr(task, 'project_id', '') or '',
                        device_id=getattr(worker_host, 'device_id', ''),
                        hostname=getattr(worker_host, 'device_name', ''),
                        ip=ip,
                        api_port=port,
                        status="starting",
                    ))
                logger.info("[自动扩容] 任务 %s 已派发到 %s", task_id[:8], ip)
                return True
            else:
                # iter-66: 非 200 明确记录状态码, 便于定位 401/404/500
                logger.warning("[自动扩容] 派发被拒: %s → %s:%s/role/start-pm "
                               "HTTP %d (%s)", task_id[:8], ip, port,
                               resp.status_code, resp.text[:120])
        except Exception as e:
            logger.warning("[自动扩容] 派发失败: %s", e)
        return False

    def _next_pending_task(self):
        """iter-66 (Bug D 修复): 取队列中最早提交的 pending 任务 (FIFO)。

        list_tasks 默认 ORDER BY created_at DESC, 直接取 [0] 是 LIFO,
        持续有新任务提交时旧任务会饥饿; 此处显式重排取最早任务。
        """
        all_tasks = self.db.list_tasks()
        pending = [t for t in all_tasks if getattr(t, 'status', '') == 'pending']
        if not pending:
            return None
        return min(pending, key=lambda t: getattr(t, 'created_at', 0) or 0)

    def _dispatch_next_task_to_worker(self, worker_host) -> bool:
        """F3.1: 将队列中最早 pending 任务派发到指定 Worker。

        Returns:
            True = 派发成功 (iter-68 起供扩容批量清空循环判定)
        """
        task = self._next_pending_task()
        if not task:
            return False
        return self._dispatch_task_to_worker(task, worker_host)


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
        # iter-57 (补强#5): 限流双桶阈值配置化 (严格桶防滥用/信任桶保并发)
        from .station_routes_common import configure_rate_limit
        configure_rate_limit(
            self.cfg.observability.api_rate_limit,
            self.cfg.observability.api_rate_limit_trusted)
        # iter-58 (补强#6 F5.2): 多用户权限 — 用户表注入 (空 = 关闭,
        # 所有人持 mesh token 即 boss 向后兼容)
        # iter-63 (团队场景深化): DB 持久化优先 (users 表), config 仅作
        # 首次种子 → token 轮换/角色修改跨重启保留
        from .station_routes_common import (
            configure_users, load_users_from_db, set_users_db,
        )
        set_users_db(self.db)
        configure_users([u.model_dump() for u in self.cfg.security.users])
        load_users_from_db(self.db)
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

        # iter-56 (补强#4 F5.1): React SPA 挂载 (/spa, hash 路由无需 fallback)
        spa_dir = STATIC_DIR / "spa"
        if (spa_dir / "index.html").is_file():
            app.mount("/spa", StaticFiles(directory=str(spa_dir), html=True),
                      name="spa")

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

        # iter-62 (F5.4 移动端 PWA): Service Worker 根路径挂载
        # (scope 默认 /; SW 注册请求不带 Authorization 头, 认证白名单放行)
        @app.get("/sw.js")
        async def service_worker():
            sw_path = STATIC_DIR / "sw.js"
            if sw_path.is_file():
                return FileResponse(
                    sw_path, media_type="application/javascript",
                    headers={"Cache-Control": "no-cache, must-revalidate"})
            return JSONResponse(status_code=404, content={"detail": "sw.js 未找到"})

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
        try:
            guardian = self.shadow_dev_manager.start_guardian()
            logger.info("[Station] 影子开发守护已启动: %s", guardian)
        except Exception as exc:
            logger.warning("[Station] 影子开发守护启动失败: %s", exc)
        try:
            optimizer = self.workstation_optimizer.start_guardian()
            logger.info("[Station] 工作站优化守护已启动: %s", optimizer)
        except Exception as exc:
            logger.warning("[Station] 工作站优化守护启动失败: %s", exc)

        # E6: 主机级单实例守护 - 同版本/更新实例在跑则取消启动;
        # 旧版实例在跑则关闭后由本进程接管 (杜绝端口回退双实例)
        from .singleton import ensure_single_instance, register_cleanup
        from .version_sync import local_version_info
        _ver = local_version_info()
        action = ensure_single_instance(
            self.cfg.secretary.api_port,
            commit=_ver.get("commit", ""),
            commit_time=_ver.get("commit_time", 0.0),
            dev_reload=dev_reload,
        )
        if action != "proceed":
            logger.warning("[E6] 单实例仲裁: %s, 本进程退出", action)
            sys.exit(0)
        register_cleanup()

        # 启动前自检 (复用 secretary 自检: 含 DB 路径 + Web 模板检查)
        from .preflight import run_preflight
        if not run_preflight("secretary", self.cfg):
            logger.critical("自检未通过, 启动中止。请根据上述提示修复后重试。")
            sys.exit(1)

        self.state.api_port = self._find_available_port(self.cfg.secretary.api_port)

        # iter-55: 预加载模型资源 (任何模式) — 让位主机远程派发执行 LLM 任务
        # 需要 Key 就绪; Secretary 当选后 activate_secretary 复用已加载配置
        self._load_model_resources()

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

        # S3: 启动一次性同步 (版本比对 + API Key 对齐, 替代 60s 轮询)
        sync_thread = threading.Thread(
            target=self._startup_sync_once, name="station-startup-sync", daemon=True
        )
        sync_thread.start()
        self._threads.append(sync_thread)

        # F1: 周期角色无关密钥对齐 (主从无关, config_ts 仲裁收敛)
        align_thread = threading.Thread(
            target=self._align_loop, name="station-align", daemon=True
        )
        align_thread.start()
        self._threads.append(align_thread)

        # iter-44: 错误追踪闭环接线 (F1.4 后半) — 每条错误事件推送 (WS 实时刷新面板)
        # + 突发告警 (事件总线 + Bot, 冷却去重在 tracker 内); 异常不影响启动
        # iter-47: + 落盘持久化回调 (error_log 表, 重启不丢诊断历史)
        try:
            from .error_tracker import error_tracker
            from .event_bus import publish_event

            def _on_error_captured(record: dict):
                try:
                    publish_event("error_captured", record)
                except Exception:
                    pass

            def _on_error_persist(record: dict):
                try:
                    self.db.save_error_record(record)
                except Exception:
                    pass

            def _on_error_burst(module: str, count: int, window_secs: float):
                data = {"module": module, "count": count, "window": window_secs}
                try:
                    publish_event("error_burst", data)
                except Exception:
                    pass
                if self.bot_gateway:
                    try:
                        self.bot_gateway.notify("error_burst", data)
                    except Exception as e:
                        logger.warning("[ErrorTracker] 突发告警推送失败: %s", e)

            error_tracker.set_event_callback(_on_error_captured)
            error_tracker.set_alert_callback(_on_error_burst)
            error_tracker.set_persist_callback(_on_error_persist)
        except Exception as e:
            logger.warning("错误追踪接线失败 (no-op): %s", e)

        # iter-50: F4.2 自动自愈守护线程 (周期扫描诊断 + 安全动作自动执行, 默认关)
        try:
            auto_heal_thread = threading.Thread(
                target=self._auto_heal_loop, name="station-auto-heal", daemon=True
            )
            auto_heal_thread.start()
            self._threads.append(auto_heal_thread)
        except Exception as e:
            logger.warning("自动自愈守护启动失败 (no-op): %s", e)

        # F3.1: 启动自动扩缩容监控
        self._start_autoscaler()

        # F3.4 (iter-64): 跨网段联邦轮询线程 (静态 peer, 配置启用才启动)
        if self.cfg.federation.enabled and self.cfg.federation.peers:
            try:
                federation_thread = threading.Thread(
                    target=self._federation_loop, name="station-federation", daemon=True
                )
                federation_thread.start()
                self._threads.append(federation_thread)
            except Exception as e:
                logger.warning("联邦轮询线程启动失败 (no-op): %s", e)

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
        if self.workstation_optimizer:
            try:
                self.workstation_optimizer.stop_guardian()
            except Exception as exc:
                logger.warning("[Station] 工作站优化守护停止失败: %s", exc)
        if self.shadow_dev_manager:
            try:
                self.shadow_dev_manager.stop_guardian()
            except Exception as exc:
                logger.warning("[Station] 影子开发守护停止失败: %s", exc)
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
