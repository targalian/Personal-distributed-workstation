"""
PM 远程反向控制与任务 DAG 图读写 — 取消/暂停/注入/图更新。

拆分自 station_controller.py (iter-75 Phase 3)。这里的方法面向「PM 跑在别的
节点上」的场景: Secretary 收到 Boss 指令后经 HTTP 反向下发到 PM 所在 Worker,
iter-105 起三项 PM 控制委托 PmControlService；DAG 图读写保留在此兼容层。
"""
from typing import Optional

from .http_retry import http_post
from .logger import get_logger

logger = get_logger("station")


class StationPmControlMixin:
    """PM 远程控制与 DAG 图读写。

    依赖 StationController 提供的属性: db / state / _pm_worker_map /
    pm_agents / project_manager 等。
    """

    # ── 优化7: 反向沟通 ──

    def inject_input_to_pm(self, pm_id: str, input_data: dict) -> dict:
        """Compatibility entry routed through the shared PM control service."""
        from .pm_control_service import call_pm_control
        return call_pm_control(self, "inject_input_to_pm", pm_id=pm_id, input_data=input_data)

    # ── 优化8: 取消/暂停任务 ──

    def cancel_task(self, task_id: str) -> dict:
        """Compatibility entry routed through the shared PM control service."""
        from .pm_control_service import call_pm_control
        return call_pm_control(self, "cancel_task", task_id=task_id)

    def pause_task(self, task_id: str) -> dict:
        """Compatibility entry routed through the shared PM control service."""
        from .pm_control_service import call_pm_control
        return call_pm_control(self, "pause_task", task_id=task_id)

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
