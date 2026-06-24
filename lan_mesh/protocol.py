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
MASTER_API_PORT = 45470          # Master HTTP API / Web UI 端口

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
    role: str = "worker"                # master | worker
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

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "HostInfo":
        valid = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**valid)


# ── 主机注册记录 (Master 端数据库) ──────────────────────────────

@dataclass
class HostRecord:
    """Master 端维护的主机注册记录 — 含在线状态与最后心跳时间。"""
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

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "HostRecord":
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

    每个 Worker 启动时生成此卡片并注册到 Master,
    Master 维护 Agent Registry 用于任务匹配与分发。
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

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Task":
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
