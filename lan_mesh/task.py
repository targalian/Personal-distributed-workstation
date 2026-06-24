"""
任务 DAG 管理 — 子任务依赖图与拓扑排序

支持:
- 声明子任务间的依赖关系
- 拓扑排序确定执行顺序
- 查找当前可执行的子任务 (依赖已满足)
- 检测循环依赖
"""
from collections import defaultdict, deque
from typing import List, Optional

from .protocol import SubTask


class TaskDAG:
    """子任务有向无环图。

    每个节点是一个 SubTask,边表示依赖关系 (A depends_on B → B 必须先完成)。
    """

    def __init__(self, subtasks: List[SubTask]):
        self.subtasks: dict[str, SubTask] = {st.subtask_id: st for st in subtasks}
        # 邻接表: 被依赖 → 依赖它的任务列表
        self._dependents: dict[str, list[str]] = defaultdict(list)
        # 入度: 每个任务依赖多少个其他任务
        self._in_degree: dict[str, int] = {}

        for st in subtasks:
            deps = st.depends_on or []
            self._in_degree[st.subtask_id] = len(deps)
            for dep_id in deps:
                self._dependents[dep_id].append(st.subtask_id)

    def topological_sort(self) -> List[str]:
        """Kahn 算法拓扑排序,返回执行顺序。

        如果存在循环依赖,返回已排序的部分 (忽略环中节点)。
        """
        in_deg = dict(self._in_degree)
        queue = deque(
            sid for sid, deg in in_deg.items() if deg == 0
        )
        result = []
        while queue:
            sid = queue.popleft()
            result.append(sid)
            for dependent in self._dependents.get(sid, []):
                in_deg[dependent] -= 1
                if in_deg[dependent] == 0:
                    queue.append(dependent)
        return result

    def get_ready_subtasks(self) -> List[SubTask]:
        """返回当前可执行的子任务 (状态为 pending 且所有依赖已完成)。"""
        ready = []
        for sid, st in self.subtasks.items():
            if st.status != "pending":
                continue
            deps = st.depends_on or []
            if all(
                self.subtasks[dep_id].status == "completed"
                for dep_id in deps if dep_id in self.subtasks
            ):
                ready.append(st)
        return ready

    def has_cycle(self) -> bool:
        """检测是否存在循环依赖。"""
        return len(self.topological_sort()) < len(self.subtasks)

    def is_all_completed(self) -> bool:
        """所有子任务是否已完成。"""
        return all(st.status == "completed" for st in self.subtasks.values())

    def has_failed(self) -> bool:
        """是否有子任务失败。"""
        return any(st.status == "failed" for st in self.subtasks.values())

    def update_subtask(self, subtask_id: str, **kwargs):
        """更新子任务状态。"""
        if subtask_id in self.subtasks:
            st = self.subtasks[subtask_id]
            for k, v in kwargs.items():
                if hasattr(st, k):
                    setattr(st, k, v)

    def to_subtask_list(self) -> List[SubTask]:
        """返回子任务列表。"""
        return list(self.subtasks.values())
