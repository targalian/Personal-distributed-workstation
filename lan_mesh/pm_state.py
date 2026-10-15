"""
PM Agent 共享状态容器

所有子模块 (planner / dispatcher / monitor) 通过此 dataclass 访问共享状态,
由 ProjectManagerAgent 统一持有, 线程安全通过 state.lock 保护。
"""
import copy
import threading
import time
from dataclasses import dataclass, field
from typing import Callable


@dataclass
class PMState:
    """PM Agent 各子模块共享的运行时状态。

    所有可变字段的读写必须在 ``with state.lock:`` 内进行。
    """

    # ── 线程安全锁 ──
    lock: threading.RLock = field(default_factory=threading.RLock)
    stop_event: threading.Event = field(default_factory=threading.Event)
    stop_reason: str = ""
    monitoring_ready: threading.Event = field(default_factory=threading.Event)

    # ── 规划结果 ──
    plan: dict = field(default_factory=dict)
    task: dict = field(default_factory=dict)

    # ── 子任务运行时状态 ──
    subtask_outputs: dict = field(default_factory=dict)    # task_name → output
    pending_subtasks: dict = field(default_factory=dict)   # task_name → {sub, station, agent_info}
    dispatched: set = field(default_factory=set)           # 已分发的 task_name
    task_station: dict = field(default_factory=dict)       # task_name → station dict
    task_agent: dict = field(default_factory=dict)         # task_name → agent_info dict
    execution_attempts: dict = field(default_factory=dict)  # task_name → current execution identity/phase
    execution_history: dict = field(default_factory=dict)   # execution_id → superseded terminal attempt

    # ── 团队 / 子 Agent ──
    teams: dict = field(default_factory=dict)              # team_id → team_info
    subagents: dict = field(default_factory=dict)          # member_id → member_info

    # ── 失败接管 ──
    retry_counts: dict = field(default_factory=dict)       # task_name → 已重试次数
    max_retries: int = 2
    cross_station_attempted: set = field(default_factory=set)
    local_takeover_tasks: set = field(default_factory=set)
    terminal_failures: dict = field(default_factory=dict)
    blocked_subtasks: dict = field(default_factory=dict)

    # ── 超时保护 ──
    global_timeout: float = 3600.0
    subtask_timeout: float = 1800.0
    start_time: float = field(default_factory=time.time)
    subtask_start_times: dict = field(default_factory=dict)  # task_name → 开始时间

    # ── 反向沟通 ──
    clarification_event: threading.Event = field(default_factory=threading.Event)
    clarification_response: dict = field(default_factory=dict)
    clarification_request: dict = field(default_factory=dict)
    clarification_timeout: float = 600.0

    phase: str = "created"
    direct_execution: dict = field(default_factory=dict)
    delivery: dict = field(default_factory=dict)
    recovery: dict = field(default_factory=dict)
    persist: Callable[[str], bool] = field(default=lambda phase="": True, repr=False)

    def snapshot(self) -> dict:
        """Caller holds lock; omit locks/events/callbacks from the durable state."""
        names = ("phase", "plan", "task", "subtask_outputs", "pending_subtasks", "task_station", "task_agent",
                 "execution_attempts", "execution_history", "teams", "subagents", "retry_counts", "max_retries", "terminal_failures",
                 "blocked_subtasks", "global_timeout", "subtask_timeout", "start_time", "subtask_start_times",
                 "clarification_response", "clarification_request", "clarification_timeout", "stop_reason", "direct_execution", "delivery", "recovery")
        return {"format": 1, **copy.deepcopy({name: getattr(self, name) for name in names}),
                "dispatched": sorted(self.dispatched), "cross_station_attempted": sorted(self.cross_station_attempted),
                "local_takeover_tasks": sorted(self.local_takeover_tasks),
                "stopped": self.stop_event.is_set(), "monitoring_ready": self.monitoring_ready.is_set()}

    def restore(self, snapshot: dict) -> None:
        """Restore durable fields into the state already shared by all PM components."""
        for name in self.snapshot():
            if name in ("format", "stopped", "monitoring_ready") or name not in snapshot:
                continue
            value = copy.deepcopy(snapshot[name])
            setattr(self, name, set(value) if isinstance(getattr(self, name), set) else value)
        self.stop_event.clear()
        self.monitoring_ready.clear()
        if self.clarification_response:
            self.clarification_event.set()
