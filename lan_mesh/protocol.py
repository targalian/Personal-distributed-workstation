"""
协议定义 - 端口常量、发现数据包、主机信息模型

参考 QuickLAN 的 DiscoveryPacket 设计，扩展了主机硬件配置字段。
"""
import time
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Optional


# ── 端口与时间常量 ──────────────────────────────────────────────

APP_NAME = "lan-mesh"
PROTOCOL_VERSION = 1

DISCOVERY_PORT = 45454          # UDP 广播发现端口 (与 QuickLAN 一致)
WORKER_API_PORT = 45460          # Worker HTTP API 起始端口
SECRETARY_API_PORT = 45470       # Secretary HTTP API / Web UI 端口

PRESENCE_INTERVAL_SECS = 3      # UDP 存在广播间隔
HEARTBEAT_INTERVAL_SECS = 5      # HTTP 心跳间隔
DEVICE_TTL_SECS = 12             # 设备离线判定阈值 (4 次未收到心跳)
PRUNE_INTERVAL_SECS = 5          # 离线清理检查间隔


# ── 发现数据包 (UDP 广播载荷) ───────────────────────────────────

@dataclass
class DiscoveryPacket:
    """UDP 广播发现包 — 携带设备身份与关键配置摘要。

    参考QuickLAN DiscoveryPacket,扩展了主机配置摘要字段,
    使接收方无需额外HTTP请求即可获得基本硬件画像。
    """
    app: str = APP_NAME
    version: int = PROTOCOL_VERSION
    packet_type: str = "presence"       # presence | register
    device_id: str = ""
    device_name: str = ""
    role: str = "worker"                # secretary | worker
    api_port: int = 0
    # ── 主机配置摘要 ──
    hostname: str = ""
    platform: str = ""
    cpu_count: int = 0
    cpu_percent: float = 0.0
    memory_total_mb: int = 0
    memory_percent: float = 0.0
    disk_total_gb: int = 0
    disk_percent: float = 0.0
    shared_folder: str = ""
    ip_addresses: list = field(default_factory=list)
    # ── S2: 代码版本 (升级提醒) ──
    code_version: str = ""              # git 短 commit
    version_ts: float = 0.0             # commit 提交时间戳 (版本全序比较依据)

    def is_lanmesh(self) -> bool:
        return self.app == APP_NAME and self.version == PROTOCOL_VERSION

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "DiscoveryPacket":
        valid = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**valid)


# ── 完整主机信息 (HTTP API 载荷) ────────────────────────────────

@dataclass
class HostInfo:
    """完整主机配置信息 — 通过 HTTP API 获取的详细画像。"""
    device_id: str = ""
    device_name: str = ""
    role: str = "worker"
    hostname: str = ""
    platform: str = ""
    platform_release: str = ""
    architecture: str = ""
    python_version: str = ""
    # CPU
    cpu_count: int = 0
    cpu_percent: float = 0.0
    cpu_freq_mhz: float = 0.0
    # 内存
    memory_total_mb: int = 0
    memory_available_mb: int = 0
    memory_percent: float = 0.0
    # 磁盘
    disk_total_gb: int = 0
    disk_used_gb: int = 0
    disk_free_gb: int = 0
    disk_percent: float = 0.0
    # 网络
    ip_addresses: list = field(default_factory=list)
    mac_address: str = ""
    # 共享
    shared_folder: str = ""
    shared_file_count: int = 0
    # 运行时
    api_port: int = 0
    uptime_seconds: float = 0.0
    timestamp: float = field(default_factory=time.time)
    # ── S2: 代码版本 (升级提醒) ──
    code_version: str = ""
    version_ts: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "HostInfo":
        valid = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**valid)


# ── 主机注册记录 (Secretary 端数据库) ────────────────────────────

@dataclass
class HostRecord:
    """Secretary 端维护的主机注册记录 — 含在线状态与最后心跳时间。"""
    device_id: str = ""
    device_name: str = ""
    role: str = "worker"
    hostname: str = ""
    platform: str = ""
    ip: str = ""
    api_port: int = 0
    # 配置摘要
    cpu_count: int = 0
    memory_total_mb: int = 0
    disk_total_gb: int = 0
    # 实时状态
    cpu_percent: float = 0.0
    memory_percent: float = 0.0
    disk_percent: float = 0.0
    shared_folder: str = ""
    shared_file_count: int = 0
    # 状态
    online: bool = True
    registered_at: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    latency_ms: Optional[float] = None
    # 主机评级 (Station Director)
    rating_tier: str = ""       # S/A/B/C/D
    rating_score: int = 0       # 综合得分 (0~100)
    rating_summary: str = ""    # 人类可读摘要
    # ── S2/S3: 代码版本 (跨主机版本统计与升级提醒) ──
    code_version: str = ""      # git 短 commit
    version_ts: float = 0.0     # commit 提交时间戳

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "HostRecord":
        valid = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**valid)


# ── 主机事件记录 (出入站历史) ──────────────────────────────────────

@dataclass
class HostEvent:
    """主机事件记录 — 记录主机出入站、评级变更等事件。

    event_type:
    - "join": 主机首次注册入站
    - "leave": 主机离线
    - "register": 主机重新注册 (已存在记录的再次入站)
    - "rating_change": 评级变更
    """
    device_id: str = ""
    event_type: str = ""       # "join" | "leave" | "register" | "rating_change"
    timestamp: float = field(default_factory=time.time)
    detail: str = ""           # 事件详情 (如 "A->S" 评级变更)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "HostEvent":
        valid = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**valid)


@dataclass
class NetworkStatus:
    """本机网络状态快照。"""
    udp_port: int = DISCOVERY_PORT
    api_port: int = 0
    local_ips: list = field(default_factory=list)
    broadcast_targets: list = field(default_factory=list)


# ── Agent 能力声明 (借鉴 A2A Agent Card) ─────────────────────────

@dataclass
class Skill:
    """Agent 技能声明 — 描述 Agent 能处理的一类任务。"""
    name: str = ""
    description: str = ""
    input_schema: dict = field(default_factory=dict)   # JSON Schema 描述输入
    tags: list = field(default_factory=list)            # 能力标签

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Skill":
        valid = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**valid)


@dataclass
class ToolDef:
    """工具定义 — Agent 可调用的外部工具 (MCP 兼容)。"""
    name: str = ""
    description: str = ""
    mcp_compatible: bool = True
    input_schema: dict = field(default_factory=dict)   # JSON Schema

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ToolDef":
        valid = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**valid)


class AgentStatus(str, Enum):
    """Agent 运行状态。"""
    IDLE = "idle"
    BUSY = "busy"
    OFFLINE = "offline"


@dataclass
class AgentCard:
    """Agent 能力卡片 — 借鉴 A2A 协议的 Agent Card。

    每个 Worker 启动时生成此卡片并注册到 Secretary,
    Secretary 维护 Agent Registry 用于任务匹配与分发。
    """
    agent_id: str = ""
    agent_name: str = ""
    version: str = "0.1.0"
    # 宿主信息
    device_id: str = ""
    hostname: str = ""
    ip: str = ""
    api_port: int = 0
    # 能力声明
    skills: list = field(default_factory=list)       # List[Skill.to_dict()]
    tools: list = field(default_factory=list)        # List[ToolDef.to_dict()]
    model_preferences: list = field(default_factory=list)
    max_concurrent_tasks: int = 5
    # 运行时
    status: str = "idle"
    current_task_count: int = 0
    registered_at: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "AgentCard":
        valid = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**valid)


# ── 任务模型 (借鉴 A2A Task lifecycle + DAG) ─────────────────────

class TaskStatus(str, Enum):
    """任务状态机。"""
    PENDING = "pending"
    ASSIGNED = "assigned"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class SubTask:
    """子任务 — DAG 中的一个节点。"""
    subtask_id: str = ""
    parent_task_id: str = ""
    name: str = ""
    description: str = ""
    required_skill: str = ""               # 匹配 AgentCard.skills[].name
    input_data: dict = field(default_factory=dict)
    output_data: dict = field(default_factory=dict)
    status: str = "pending"
    assigned_agent_id: str = ""
    depends_on: list = field(default_factory=list)  # 前置 subtask_id 列表
    condition_expr: str = ""               # 条件边表达式 (如 "status == 'completed' and score > 0.8")
    created_at: float = field(default_factory=time.time)
    started_at: float = 0.0
    completed_at: float = 0.0
    error: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "SubTask":
        valid = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**valid)


@dataclass
class Task:
    """顶层任务 — 用户提交的完整任务。"""
    task_id: str = ""
    name: str = ""
    description: str = ""
    input_data: dict = field(default_factory=dict)
    output_data: dict = field(default_factory=dict)
    status: str = "pending"
    subtasks: list = field(default_factory=list)   # List[SubTask.to_dict()]
    created_at: float = field(default_factory=time.time)
    completed_at: float = 0.0
    created_by: str = "user"
    project_id: str = ""               # 关联的项目 ID (项目隔离)
    pm_agent_id: str = ""              # 接管此任务的 PM Agent ID

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Task":
        valid = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**valid)


# ── 模型路由相关 (Phase 2) ─────────────────────────────────────────


class DifficultyLevel(str, Enum):
    """任务难度分级 (L1-L4)。"""
    L1 = "L1"   # 极简: 关键词提取、格式转换
    L2 = "L2"   # 常规: 文档摘要、邮件撰写
    L3 = "L3"   # 复杂: 代码生成、多步逻辑推理
    L4 = "L4"   # 专家级: 大型架构设计、深度分析


@dataclass
class RoutingResult:
    """模型路由决策结果 — 由 ModelRouter 返回。"""
    selected_model: str = ""           # 推荐使用的模型 ID
    provider: str = ""                 # 模型厂商 (openai / deepseek / anthropic / qwen)
    base_url: str = ""                 # API Base URL
    difficulty: str = "L2"             # 难度级别 (L1/L2/L3/L4)
    score: float = 0.0                 # 综合评分
    candidates: list = field(default_factory=list)  # 所有候选模型评分 [{model, score}]
    fallback_chain: list = field(default_factory=list)  # 降级链 [model_id, ...]
    strategy: str = "balanced"         # 路由策略 (cost_first / quality_first / balanced)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "RoutingResult":
        valid = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**valid)


# ── 项目隔离模型 (Phase 3) ───────────────────────────────────────


class ProjectStatus(str, Enum):
    """项目状态。"""
    ACTIVE = "active"
    SUSPENDED = "suspended"
    ARCHIVED = "archived"


@dataclass
class Project:
    """项目 — 每个项目拥有独立的工作空间、预算配额和模型限制。

    不同项目之间上下文隔离,预算独立,互不影响。
    """
    project_id: str = ""
    name: str = ""
    description: str = ""
    workspace_path: str = ""              # 独立工作空间目录
    budget_limit_usd: float = 0.0          # 月度预算上限 (美元)
    budget_used_usd: float = 0.0           # 已消费金额 (美元)
    allowed_models: list = field(default_factory=list)   # 允许使用的模型 ID 列表 (空=全部允许)
    routing_strategy: str = "balanced"    # cost_first | quality_first | balanced
    status: str = "active"                # active | suspended | archived
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Project":
        valid = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**valid)


@dataclass
class UsageRecord:
    """模型调用消费记录 — 用于项目预算追踪。"""
    project_id: str = ""
    task_id: str = ""
    subtask_id: str = ""
    model: str = ""                       # 调用的模型名称
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0                  # 本次调用成本 (美元)
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "UsageRecord":
        valid = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**valid)


# ── 技能库记录 (Skill Registry) ─────────────────────────────────


@dataclass
class SkillRecord:
    """技能库记录 — 描述一个可分发的能力/知识包。

    每个技能对应 skills/ 目录下的一个子目录,
    包含 SKILL.md (主内容 + YAML front matter) 和可选的 reference.md。
    """
    skill_id: str = ""               # 唯一标识 (如 "multi-agent-architect")
    name: str = ""                   # 显示名称
    description: str = ""            # 简短描述
    category: str = "general"        # 分类: architecture | coding | ops | management | general
    tags: list = field(default_factory=list)  # 能力标签
    default_access: list = field(default_factory=lambda: ["all"])  # 默认可访问的角色列表
    content_path: str = ""           # 相对于 skills_dir 的路径
    origin: str = "builtin"          # iter-61: 来源标记 builtin(内置) | market(第三方市场安装)
    version: str = "1.0"
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "SkillRecord":
        valid = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**valid)


# ── 项目经理 Agent 模型 (PM Agent 架构演进) ───────────────────────


class PMAgentStatus(str, Enum):
    """项目经理 Agent 运行状态。"""
    STARTING = "starting"        # 刚注册，尚未开始规划
    PLANNING = "planning"        # 正在用 skill 分析任务、决策团队架构
    EXECUTING = "executing"      # 团队已创建，正在执行任务
    MONITORING = "monitoring"    # 任务已分发，正在监控进度
    COMPLETED = "completed"      # 任务完成
    FAILED = "failed"            # 任务失败


@dataclass
class TeamMember:
    """团队成员 — PM 创建的子 Agent。"""
    member_id: str = ""
    team_id: str = ""
    agent_id: str = ""               # 对应的 AgentCard.agent_id
    agent_name: str = ""
    device_id: str = ""              # 所在 work_station
    role: str = "worker"             # worker | reviewer | lead
    skills: list = field(default_factory=list)
    current_task: str = ""
    status: str = "idle"             # idle | busy | offline
    progress: float = 0.0            # 0.0 ~ 1.0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "TeamMember":
        valid = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**valid)


@dataclass
class AgentTeam:
    """Agent 团队 — PM 创建的子 Agent 团队。"""
    team_id: str = ""
    pm_id: str = ""                  # 所属 PM
    team_name: str = ""
    team_type: str = ""              # single | parallel | pipeline | nested
    device_id: str = ""              # 所在 work_station
    parent_team_id: str = ""         # 父团队 ID (嵌套时)
    members: list = field(default_factory=list)  # List[TeamMember.to_dict()]
    status: str = "pending"          # pending | active | completed | failed
    current_task: str = ""
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "AgentTeam":
        valid = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**valid)


@dataclass
class PMAgent:
    """项目经理 Agent — 接管任务开发的管理型 Agent。

    由 Secretary 注册到合适的 work_station 上运行，
    使用 multi-agent-architect skill 自主决策团队架构、
    分解任务、创建子 Agent / 团队、管理进度反馈。
    """
    pm_id: str = ""
    agent_name: str = ""
    task_id: str = ""                # 接管的任务 ID
    project_id: str = ""
    device_id: str = ""              # 所在 work_station
    hostname: str = ""
    ip: str = ""
    api_port: int = 0
    status: str = "starting"         # starting | planning | executing | monitoring | completed | failed
    team_structure: dict = field(default_factory=dict)   # 团队架构 JSON
    task_list: list = field(default_factory=list)         # 分解的任务列表
    collaboration_mode: str = ""     # single | orchestrator | teams | bus | shared_state
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "PMAgent":
        valid = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**valid)


@dataclass
class ProgressReport:
    """进度报告 — 子 Agent / 团队向 PM 报告。"""
    report_id: str = ""
    pm_id: str = ""
    reporter_id: str = ""            # member_id 或 team_id
    reporter_type: str = ""          # member | team
    task_name: str = ""
    progress: float = 0.0            # 0.0 ~ 1.0
    status: str = "in_progress"      # in_progress | completed | blocked | failed
    message: str = ""
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ProgressReport":
        valid = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**valid)
