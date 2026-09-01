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
import socket
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Set

from .config import AppConfig, get_db_path
from .database import Database
from .discovery import DiscoveryService
from .logger import get_logger

logger = get_logger("station")
from .host_info import load_or_create_device_id
from .shared_folder import SharedFolderManager
from .station_director import StationDirector
from .skill_registry import SkillRegistry
from .bot_gateway import BotGateway
from .cloud_sync import CloudSyncManager

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


# Web UI 路径常量: 定义在 station_lifecycle (_create_app 的直接消费者), 此处 re-export
# 以保持 `from lan_mesh.station_controller import STATIC_DIR` 等既有 import 路径不变。
from .station_lifecycle import (  # noqa: F401
    WEB_DIR,
    TEMPLATES_DIR,
    STATIC_DIR,
)


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
