"""
PM Agent 共享状态容器

所有子模块 (planner / dispatcher / monitor) 通过此 dataclass 访问共享状态,
由 ProjectManagerAgent 统一持有, 线程安全通过 state.lock 保护。
"""
import threading
import time
from dataclasses import dataclass, field


@dataclass
class PMState:
    """PM Agent 各子模块共享的运行时状态。

    所有可变字段的读写必须在 ``with state.lock:`` 内进行。
    """

    # ── 线程安全锁 ──
    lock: threading.RLock = field(default_factory=threading.RLock)

    # ── 规划结果 ──
    plan: dict = field(default_factory=dict)
    task: dict = field(default_factory=dict)

    # ── 子任务运行时状态 ──
    subtask_outputs: dict = field(default_factory=dict)    # task_name → output
    pending_subtasks: dict = field(default_factory=dict)   # task_name → {sub, station, agent_info}
    dispatched: set = field(default_factory=set)           # 已分发的 task_name
    task_station: dict = field(default_factory=dict)       # task_name → station dict
    task_agent: dict = field(default_factory=dict)         # task_name → agent_info dict

    # ── 团队 / 子 Agent ──
    teams: dict = field(default_factory=dict)              # team_id → team_info
    subagents: dict = field(default_factory=dict)          # member_id → member_info

    # ── 失败接管 ──
    retry_counts: dict = field(default_factory=dict)       # task_name → 已重试次数
    max_retries: int = 2

    # ── 超时保护 ──
    global_timeout: float = 3600.0
    subtask_timeout: float = 1800.0
    start_time: float = field(default_factory=time.time)
    subtask_start_times: dict = field(default_factory=dict)  # task_name → 开始时间

    # ── 反向沟通 ──
    clarification_event: threading.Event = field(default_factory=threading.Event)
    clarification_response: dict = field(default_factory=dict)
    clarification_timeout: float = 600.0
