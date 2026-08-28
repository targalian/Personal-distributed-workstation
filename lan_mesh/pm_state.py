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

    # ── 执行态持久化 (iter-53): 快照恢复辅助字段 ──
    clarification_question: str = ""   # 最近一次澄清问题 (断点恢复时重新发起)

    # ── 快照序列化 ────────────────────────────────────────────

    def to_snapshot(self) -> dict:
        """iter-53: 序列化可恢复的运行时状态 (线程安全)。

        排除不可序列化字段 (lock/clarification_event), 其余全部快照。
        """
        with self.lock:
            return {
                "plan": self.plan,
                "task": self.task,
                "subtask_outputs": dict(self.subtask_outputs),
                "pending_subtasks": dict(self.pending_subtasks),
                "dispatched": sorted(self.dispatched),
                "task_station": dict(self.task_station),
                "task_agent": dict(self.task_agent),
                "teams": dict(self.teams),
                "subagents": dict(self.subagents),
                "retry_counts": dict(self.retry_counts),
                "max_retries": self.max_retries,
                "global_timeout": self.global_timeout,
                "subtask_timeout": self.subtask_timeout,
                "start_time": self.start_time,
                "subtask_start_times": dict(self.subtask_start_times),
                "clarification_question": self.clarification_question,
            }

    @staticmethod
    def from_snapshot(data: dict) -> "PMState":
        """iter-53: 从快照重建 PMState (lock 与 Event 重新创建)。"""
        st = PMState()
        st.restore_from(data)
        return st

    def restore_from(self, data: dict) -> None:
        """iter-53: 从快照就地恢复 (保持子组件共享引用有效)。

        resume 时 planner/dispatcher/monitor 仍持有本实例引用,
        替换整个对象会切断引用链, 故只重写字段。
        """
        self.plan = data.get("plan", {}) or {}
        self.task = data.get("task", {}) or {}
        self.subtask_outputs = dict(data.get("subtask_outputs", {}) or {})
        self.pending_subtasks = dict(data.get("pending_subtasks", {}) or {})
        self.dispatched = set(data.get("dispatched", []) or [])
        self.task_station = dict(data.get("task_station", {}) or {})
        self.task_agent = dict(data.get("task_agent", {}) or {})
        self.teams = dict(data.get("teams", {}) or {})
        self.subagents = dict(data.get("subagents", {}) or {})
        self.retry_counts = dict(data.get("retry_counts", {}) or {})
        self.max_retries = int(data.get("max_retries", 2))
        self.global_timeout = float(data.get("global_timeout", 3600.0))
        self.subtask_timeout = float(data.get("subtask_timeout", 1800.0))
        self.start_time = float(data.get("start_time", time.time()))
        self.subtask_start_times = dict(data.get("subtask_start_times", {}) or {})
        self.clarification_question = data.get("clarification_question", "")
