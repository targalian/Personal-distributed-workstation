"""
PM 远程反向控制与任务 DAG 图读写 — 取消/暂停/注入/图更新。

拆分自 station_controller.py (iter-75 Phase 3)。这里的方法面向「PM 跑在别的
节点上」的场景: Secretary 收到 Boss 指令后经 HTTP 反向下发到 PM 所在 Worker,
本机 PM 则委托 _local_* 路径。DAG 图读写同属任务控制面, 故一并归此。
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
