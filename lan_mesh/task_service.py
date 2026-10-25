"""TaskService: Yomk service for the Station task domain."""


from starlette.concurrency import run_in_threadpool

from .logger import get_logger
from .service_context import StationService, StationServiceContext
from .service_runtime import ServiceError
from ._vendor.yomkserverpy.YomkServerPy import YomkServer

logger = get_logger("task_service")


class TaskService(StationService):
    """Task domain operations, callable independently of the HTTP layer."""

    def __init__(self, server: YomkServer, context: StationServiceContext):
        super().__init__(server, context, "/TaskService")

    async def list_tasks(self, status: str = None, limit: int = 50) -> dict:
        controller = self.context.controller
        db = controller.db
        self.require_secretary()
        tasks = db.list_tasks(status=status, limit=limit)
        return {"tasks": [t.to_dict() for t in tasks], "total": len(tasks)}

    async def get_task(self, task_id: str) -> dict:
        controller = self.context.controller
        db = controller.db
        self.require_secretary()
        task = db.get_task(task_id)
        if not task:
            raise ServiceError(status_code=404, detail="任务不存在")
        return task.to_dict()

    async def cancel_task(self, task_id: str) -> dict:
        """取消指定任务及其 PM Agent。"""
        controller = self.context.controller
        db = controller.db
        state = controller.state
        self.require_secretary()
        # iter-66 (Bug I): 控制命令含跨节点 HTTP 调用, 同步执行会阻塞事件循环
        # → Worker 的状态上报请求无法进入 → 跨节点死锁级联超时
        result = await run_in_threadpool(controller.cancel_task, task_id)
        if result.get("ok"):
            await self.context.broadcast(state, "task_cancelled", {
                "task_id": task_id, "message": result.get("message", ""),
            })
            return result
        raise ServiceError(status_code=409, detail=result.get("message", "取消失败"))

    async def pause_task(self, task_id: str) -> dict:
        """暂停指定任务及其 PM Agent。"""
        controller = self.context.controller
        db = controller.db
        state = controller.state
        self.require_secretary()
        # iter-66 (Bug I 同类): 见 cancel_task 说明
        result = await run_in_threadpool(controller.pause_task, task_id)
        if result.get("ok"):
            await self.context.broadcast(state, "task_paused", {
                "task_id": task_id, "message": result.get("message", ""),
            })
            return result
        raise ServiceError(status_code=409, detail=result.get("message", "暂停失败"))

    async def delete_task(self, task_id: str) -> dict:
        """彻底删除任务及关联 PM Agent 记录。若任务仍在运行, 先取消再删除。"""
        controller = self.context.controller
        db = controller.db
        state = controller.state
        self.require_secretary()
        task = db.get_task(task_id)
        if not task:
            raise ServiceError(status_code=404, detail="任务不存在")
        if task.status in ("running", "monitoring"):
            # iter-66 (Bug I 同类): 取消含跨节点调用, 丢线程池避免阻塞事件循环
            await run_in_threadpool(controller.cancel_task, task_id)
        if task.pm_agent_id and task.pm_agent_id in controller._pm_worker_map:
            del controller._pm_worker_map[task.pm_agent_id]
        # 联动清理: 从对话中解绑 PM 线程
        pm_id = task.pm_agent_id or ""
        if pm_id:
            ch = getattr(controller, 'chat_handler', None)
            if ch:
                conv_id = ch.find_conv_by_pm(pm_id)
                if conv_id:
                    ch.detach_pm_thread(conv_id, pm_id)
                # 清理 PM 线程消息 (内存 + JSONL 文件)
                ch.delete_pm_thread(pm_id)
            # 清理本机 PM Agent 残留 (含子 Agent 与线程)
            if getattr(controller, '_local_pm_agent', None) and controller._local_pm_agent.pm_id == pm_id:
                await run_in_threadpool(controller._local_stop_pm)
        ok = db.delete_task(task_id)
        if ok:
            await self.context.broadcast(state, "task_deleted", {"task_id": task_id, "name": task.name})
            return {"ok": True, "message": f"任务 '{task.name}' 已删除"}
        raise ServiceError(status_code=500, detail="删除失败")

    async def resume_task(self, task_id: str) -> dict:
        """iter-53: 断点恢复 — 从 PM 执行态快照续跑中断的任务。

        流程:
        1. 查找任务与最新快照 (无快照则 404)
        2. 任务状态重置为 running
        3. 本机重建 PM Agent 并从快照恢复 (保留已完成子任务输出,
           重新分发未完成部分)
        """
        controller = self.context.controller
        db = controller.db
        state = controller.state
        self.require_secretary()
        task = db.get_task(task_id)
        if not task:
            raise ServiceError(status_code=404, detail="任务不存在")

        snapshot = db.get_pm_snapshot_by_task(task_id)
        if not snapshot:
            raise ServiceError(status_code=404, detail="无执行态快照, 无法恢复")

        result = await run_in_threadpool(controller._local_resume_pm, task_id)
        if not result.get("ok"):
            raise ServiceError(status_code=409, detail=result.get("message", "恢复失败"))

        task = db.get_task(task_id)
        await self.context.broadcast(state, "task_updated", task.to_dict())
        await self.context.broadcast(state, "pm_resumed", {
            "pm_id": result.get("pm_id", ""), "task_id": task_id,
            "phase": snapshot.get("phase", ""),
        })
        return {
            "ok": True, "task_id": task_id,
            "pm_id": result.get("pm_id", ""),
            "message": f"任务已从快照恢复 (阶段: {snapshot.get('phase', '')})",
        }

    async def get_task_cost_estimate(self, task_id: str) -> dict:
        """F4.4 (iter-52): 任务 Token 预算预估详情 (提交时落盘 + 实时适配刷新)。"""
        controller = self.context.controller
        db = controller.db
        self.require_secretary()
        task = db.get_task(task_id)
        if not task:
            raise ServiceError(status_code=404, detail="任务不存在")
        from .budget_advisor import build_task_cost_estimate
        est = build_task_cost_estimate(
            task.name, task.description, db,
            project_id=task.project_id or "",
            project_manager=controller.project_manager)
        est["task_id"] = task_id
        est["saved_estimate"] = (task.input_data or {}).get("_cost_estimate")
        return est

    async def get_task_graph(self, task_id: str) -> dict:
        """获取任务的 DAG 图结构 JSON (供前端 SVG 渲染)。

        返回: {nodes: [{id, name, status, skill, x, y, ...}], edges: [{source, target, condition}]}
        """
        controller = self.context.controller
        db = controller.db
        self.require_secretary()
        graph = controller.get_task_graph_data(task_id)
        if graph:
            return graph
        raise ServiceError(status_code=404, detail="任务无 DAG 图数据")

    async def update_task_graph(self, task_id: str, payload: dict) -> dict:
        """iter-51 (F4.3): 保存编辑后的 DAG 图结构 (前端图编辑器/自然语言编辑回写)。

        接收: {nodes: [...], edges: [...]}
        验证: 任务存在 + 仅 pending 可编辑 + 环检测, 失败则拒绝保存。
        """
        controller = self.context.controller
        db = controller.db
        state = controller.state
        self.require_secretary()
        if not isinstance(payload, dict) or "nodes" not in payload or "edges" not in payload:
            raise ServiceError(status_code=400, detail="图数据需包含 nodes 与 edges")
        result = controller.update_task_graph(task_id, payload)
        if result.get("ok"):
            await self.context.broadcast(state, "task_graph_updated", {
                "task_id": task_id, "message": result.get("message", ""),
            })
            return result
        raise ServiceError(status_code=409, detail=result.get("message", "保存失败"))

    async def list_checkpoints(self, task_id: str) -> dict:
        """查看任务的 checkpoint 列表。"""
        controller = self.context.controller
        db = controller.db
        self.require_secretary()
        checkpoints = db.list_checkpoints(task_id)
        return {"checkpoints": checkpoints, "total": len(checkpoints)}

    async def inject_pm_input(self, pm_id: str, payload: dict) -> dict:
        """向 PM Agent 注入来自 Boss/秘书的回复 (反向沟通通道)。

        当 PM 上报 awaiting_input 状态后, 秘书/Web UI 可通过此端点
        将 Boss 的决策回复注入到 PM Agent。
        """
        controller = self.context.controller
        db = controller.db
        state = controller.state
        self.require_secretary()
        result = await run_in_threadpool(controller.inject_input_to_pm, pm_id, payload)
        if result.get("ok"):
            await self.context.broadcast(state, "pm_input_injected", {
                "pm_id": pm_id, "message": "回复已注入 PM Agent",
            })
            return result
        raise ServiceError(status_code=409, detail=result.get("message", "注入失败"))
