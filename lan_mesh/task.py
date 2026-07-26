"""
任务 DAG 管理 — 子任务依赖图与拓扑排序 (增强版: 条件边 + 动态路由 + 图序列化)

支持:
- 声明子任务间的依赖关系
- 拓扑排序确定执行顺序
- 查找当前可执行的子任务 (依赖已满足)
- 检测循环依赖
- 条件边: 根据运行时上下文动态决定是否激活某条边
- 动态图操作: 运行时添加/删除节点和边
- JSON 序列化: 导出/导入图结构 (供前端渲染和 checkpoint 恢复)
"""
import json
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field, asdict
from typing import List, Optional

from .protocol import SubTask


# ── 条件边定义 ───────────────────────────────────────────────────

@dataclass
class ConditionalEdge:
    """条件边 — 连接两个子任务节点, 可附带条件表达式。

    condition 为 Python 表达式字符串, 变量来自源节点的 output_data。
    例如: "status == 'completed' and score > 0.8"
    空 condition 表示无条件 (始终激活)。
    """
    source_id: str = ""
    target_id: str = ""
    condition: str = ""          # Python 表达式, 空 = 无条件
    description: str = ""        # 人类可读描述

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ConditionalEdge":
        valid = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**valid)

    def evaluate(self, context: dict) -> bool:
        """求值条件表达式。

        Args:
            context: 源节点的 output_data + 全局上下文合并后的字典

        Returns:
            True = 边激活 (目标节点可执行), False = 边不激活
        """
        if not self.condition:
            return True
        try:
            return bool(eval(self.condition, {"__builtins__": {}}, context))
        except Exception:
            # 表达式求值失败时默认激活 (容错)
            return True


class TaskDAG:
    """子任务有向无环图 (增强版)。

    每个节点是一个 SubTask, 边表示依赖关系 (A depends_on B → B 必须先完成)。
    支持条件边、动态增删节点/边、JSON 序列化。
    """

    def __init__(self, subtasks: List[SubTask]):
        self.subtasks: dict[str, SubTask] = {st.subtask_id: st for st in subtasks}
        # 邻接表: 被依赖 → 依赖它的任务列表
        self._dependents: dict[str, list[str]] = defaultdict(list)
        # 入度: 每个任务依赖多少个其他任务
        self._in_degree: dict[str, int] = {}
        # 条件边注册表: (source_id, target_id) → ConditionalEdge
        self._conditional_edges: dict[tuple[str, str], ConditionalEdge] = {}

        for st in subtasks:
            deps = st.depends_on or []
            self._in_degree[st.subtask_id] = len(deps)
            for dep_id in deps:
                self._dependents[dep_id].append(st.subtask_id)
                # 如果子任务自带 condition_expr, 自动注册为条件边
                if st.condition_expr:
                    edge = ConditionalEdge(
                        source_id=dep_id,
                        target_id=st.subtask_id,
                        condition=st.condition_expr,
                    )
                    self._conditional_edges[(dep_id, st.subtask_id)] = edge

    # ── 基础图算法 ───────────────────────────────────────────────

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

    def get_ready_subtasks(self, context: dict = None) -> List[SubTask]:
        """返回当前可执行的子任务 (状态为 pending 且所有依赖已完成且条件边满足)。

        Args:
            context: 可选的全局上下文, 用于条件边求值。
                     如果为 None, 则条件边默认激活。
        """
        ready = []
        for sid, st in self.subtasks.items():
            if st.status != "pending":
                continue
            deps = st.depends_on or []
            # 检查所有依赖是否已完成
            deps_met = all(
                self.subtasks[dep_id].status == "completed"
                for dep_id in deps if dep_id in self.subtasks
            )
            if not deps_met:
                continue
            # 检查条件边是否满足
            conditions_met = True
            if context is not None:
                for dep_id in deps:
                    edge = self._conditional_edges.get((dep_id, sid))
                    if edge:
                        # 合并源节点 output_data + 全局 context
                        edge_ctx = dict(context)
                        src = self.subtasks.get(dep_id)
                        if src and src.output_data:
                            edge_ctx.update(src.output_data)
                        if not edge.evaluate(edge_ctx):
                            conditions_met = False
                            break
            if conditions_met:
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

    # ── 动态图操作 ───────────────────────────────────────────────

    def add_node(self, subtask: SubTask) -> bool:
        """运行时添加节点。

        Args:
            subtask: 新的子任务节点

        Returns:
            True 添加成功, False 添加失败 (如产生环)
        """
        if subtask.subtask_id in self.subtasks:
            return False  # 已存在

        # 临时添加并检测环
        self.subtasks[subtask.subtask_id] = subtask
        deps = subtask.depends_on or []
        self._in_degree[subtask.subtask_id] = len(deps)
        for dep_id in deps:
            self._dependents[dep_id].append(subtask.subtask_id)
            if subtask.condition_expr:
                self._conditional_edges[(dep_id, subtask.subtask_id)] = ConditionalEdge(
                    source_id=dep_id,
                    target_id=subtask.subtask_id,
                    condition=subtask.condition_expr,
                )

        if self.has_cycle():
            # 回滚
            self._remove_node_internal(subtask.subtask_id)
            return False
        return True

    def remove_node(self, subtask_id: str) -> bool:
        """运行时删除节点及其关联边。

        Args:
            subtask_id: 要删除的子任务 ID

        Returns:
            True 删除成功, False 节点不存在
        """
        if subtask_id not in self.subtasks:
            return False
        self._remove_node_internal(subtask_id)
        return True

    def _remove_node_internal(self, subtask_id: str):
        """内部删除节点实现。"""
        st = self.subtasks.pop(subtask_id, None)
        if not st:
            return

        # 删除入边 (该节点依赖别人的)
        for dep_id in (st.depends_on or []):
            if subtask_id in self._dependents.get(dep_id, []):
                self._dependents[dep_id].remove(subtask_id)
            self._conditional_edges.pop((dep_id, subtask_id), None)

        # 删除出边 (别人依赖该节点的)
        for dependent_id in list(self._dependents.get(subtask_id, [])):
            dep_st = self.subtasks.get(dependent_id)
            if dep_st and subtask_id in (dep_st.depends_on or []):
                dep_st.depends_on.remove(subtask_id)
                self._in_degree[dependent_id] = max(0, self._in_degree.get(dependent_id, 1) - 1)
            self._conditional_edges.pop((subtask_id, dependent_id), None)

        # 清理邻接表
        self._dependents.pop(subtask_id, None)
        self._in_degree.pop(subtask_id, None)

    def add_edge(self, source_id: str, target_id: str, condition: str = "",
                 description: str = "") -> bool:
        """添加边 (可选条件)。

        Args:
            source_id: 源节点 ID (被依赖方)
            target_id: 目标节点 ID (依赖方)
            condition: 条件表达式 (空 = 无条件)
            description: 边的描述

        Returns:
            True 添加成功, False 失败 (节点不存在或产生环)
        """
        if source_id not in self.subtasks or target_id not in self.subtasks:
            return False
        # 检查边是否已存在
        if target_id in self._dependents.get(source_id, []):
            return False

        # 临时添加
        self._dependents[source_id].append(target_id)
        self._in_degree[target_id] = self._in_degree.get(target_id, 0) + 1
        target_st = self.subtasks[target_id]
        if source_id not in (target_st.depends_on or []):
            target_st.depends_on.append(source_id)

        if self.has_cycle():
            # 回滚
            self._dependents[source_id].remove(target_id)
            self._in_degree[target_id] -= 1
            target_st.depends_on.remove(source_id)
            return False

        # 注册条件边
        if condition:
            self._conditional_edges[(source_id, target_id)] = ConditionalEdge(
                source_id=source_id,
                target_id=target_id,
                condition=condition,
                description=description,
            )
            target_st.condition_expr = condition
        return True

    def remove_edge(self, source_id: str, target_id: str) -> bool:
        """删除边。

        Args:
            source_id: 源节点 ID
            target_id: 目标节点 ID

        Returns:
            True 删除成功, False 边不存在
        """
        if target_id not in self._dependents.get(source_id, []):
            return False

        self._dependents[source_id].remove(target_id)
        self._in_degree[target_id] = max(0, self._in_degree.get(target_id, 1) - 1)

        target_st = self.subtasks.get(target_id)
        if target_st and source_id in (target_st.depends_on or []):
            target_st.depends_on.remove(source_id)

        self._conditional_edges.pop((source_id, target_id), None)
        if target_st:
            target_st.condition_expr = ""
        return True

    def evaluate_conditional_edges(self, context: dict) -> List[str]:
        """根据上下文求值所有条件边, 返回应激活的目标节点 ID 列表。

        Args:
            context: 全局上下文 (子任务输出、环境变量等)

        Returns:
            激活的目标节点 ID 列表
        """
        activated = []
        for (src_id, tgt_id), edge in self._conditional_edges.items():
            src = self.subtasks.get(src_id)
            edge_ctx = dict(context)
            if src and src.output_data:
                edge_ctx.update(src.output_data)
            if edge.evaluate(edge_ctx):
                activated.append(tgt_id)
        return activated

    # ── JSON 序列化 (前端渲染 + checkpoint) ─────────────────────

    def to_graph_json(self) -> dict:
        """导出图结构为 JSON (供前端 SVG 渲染和 checkpoint 存储)。

        Returns:
            {
                "nodes": [{id, name, status, skill, description, x, y, ...}],
                "edges": [{source, target, condition, description}]
            }
        """
        nodes = []
        for idx, (sid, st) in enumerate(self.subtasks.items()):
            nodes.append({
                "id": sid,
                "name": st.name,
                "status": st.status,
                "skill": st.required_skill,
                "description": st.description,
                "input_data": st.input_data,
                "output_data": st.output_data,
                "assigned_agent_id": st.assigned_agent_id,
                "condition_expr": st.condition_expr,
                # 默认布局: 按拓扑层级排列
                "x": 80 + (idx % 4) * 220,
                "y": 80 + (idx // 4) * 140,
            })

        edges = []
        for sid, st in self.subtasks.items():
            for dep_id in (st.depends_on or []):
                edge_info = {
                    "source": dep_id,
                    "target": sid,
                    "condition": "",
                    "description": "",
                }
                cond_edge = self._conditional_edges.get((dep_id, sid))
                if cond_edge:
                    edge_info["condition"] = cond_edge.condition
                    edge_info["description"] = cond_edge.description
                edges.append(edge_info)

        return {"nodes": nodes, "edges": edges}

    @classmethod
    def from_graph_json(cls, data: dict) -> "TaskDAG":
        """从 JSON 重建 DAG (供图编辑保存和 checkpoint 恢复)。

        Args:
            data: {"nodes": [...], "edges": [...]}

        Returns:
            重建的 TaskDAG 实例
        """
        nodes = data.get("nodes", [])
        edges = data.get("edges", [])

        # 构建依赖关系映射
        deps_map: dict[str, list[str]] = defaultdict(list)
        cond_map: dict[str, str] = {}  # target_id → condition_expr
        for edge in edges:
            src = edge.get("source", "")
            tgt = edge.get("target", "")
            if src and tgt:
                deps_map[tgt].append(src)
                if edge.get("condition"):
                    cond_map[tgt] = edge["condition"]

        subtasks = []
        for node in nodes:
            st = SubTask(
                subtask_id=node.get("id", str(uuid.uuid4())),
                name=node.get("name", ""),
                description=node.get("description", ""),
                required_skill=node.get("skill", ""),
                status=node.get("status", "pending"),
                input_data=node.get("input_data", {}),
                output_data=node.get("output_data", {}),
                assigned_agent_id=node.get("assigned_agent_id", ""),
                depends_on=deps_map.get(node.get("id", ""), []),
                condition_expr=cond_map.get(node.get("id", ""), ""),
            )
            subtasks.append(st)

        return cls(subtasks)
