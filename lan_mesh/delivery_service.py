"""DeliveryService: Yomk service for the Station task domain."""

import time


from .logger import get_logger
from .service_context import StationService, StationServiceContext
from .service_runtime import ServiceError
from ._vendor.yomkserverpy.YomkServerPy import YomkServer

logger = get_logger("task_service")

def _reflow_blueprint(controller, task_id: str, task_name: str,
                      acceptance_review: dict) -> bool:
    """iter-95: 把交付结论回流到任务所属项目的蓝图路线图。

    PM 依蓝图执行 → 交付结论回写蓝图对应阶段 → Boss 据此提修改, 形成闭环。
    任务无 project_id (或项目无蓝图) 时静默跳过; 任何异常都只记日志,
    绝不让蓝图回流失败影响交付物入库。
    """
    manager = getattr(controller, "project_manager", None)
    if not manager:
        return False
    try:
        task = controller.db.get_task(task_id)
        project_id = getattr(task, "project_id", "") if task else ""
        if not project_id:
            return False
        updated = manager.record_delivery_to_blueprint(
            project_id, task_name, acceptance_review, task_id=task_id)
        return updated is not None
    except Exception as e:
        logger.error("交付结论回流蓝图失败 (不影响交付): %s", e)
        return False


class DeliveryService(StationService):
    """Task domain operations, callable independently of the HTTP layer."""

    def __init__(self, server: YomkServer, context: StationServiceContext):
        super().__init__(server, context, "/DeliveryService")

    async def receive_pm_delivery(self, pm_id: str, payload: dict) -> dict:
        """接收 PM Agent 的最终交付物 (任务完成后调用)。

        存储交付物 → WebSocket 广播 → Bot 推送 → 聊天窗口通知 Boss。
        Boss 可随后「验收」或「退回」。
        """
        controller = self.context.controller
        db = controller.db
        state = controller.state
        self.require_secretary()
        task_id = payload.get("task_id", "")
        task_name = payload.get("task_name", "")
        deliverable = payload.get("deliverable", "")
        summary = payload.get("summary", "")
        subtask_stats = payload.get("subtask_stats", {})
        acceptance_review = payload.get("acceptance_review", {})

        # 存储交付物到 DB (追加到 task output_data)
        if task_id:
            task = db.get_task(task_id)
            if task:
                if not task.output_data:
                    task.output_data = {}
                task.output_data["_delivery"] = {
                    "pm_id": pm_id,
                    "deliverable": deliverable,
                    "summary": summary,
                    "subtask_stats": subtask_stats,
                    "delivered_at": payload.get("delivered_at", time.time()),
                    "accepted": None,  # 等待 Boss 验收
                    "acceptance_review": acceptance_review,  # iter-90: 蓝图验收自检
                }
                db.save_task(task)

        # iter-95: 交付结论回流项目蓝图 (蓝图↔项目闭环, 异常不阻断交付)
        blueprint_updated = False
        if task_id:
            blueprint_updated = _reflow_blueprint(
                controller, task_id, task_name, acceptance_review)

        # WebSocket 广播
        await self.context.broadcast(state, "task_delivered", {
            "pm_id": pm_id,
            "task_id": task_id,
            "task_name": task_name,
            "summary": summary,
            "subtask_stats": subtask_stats,
            "acceptance_review": acceptance_review,
            "blueprint_updated": blueprint_updated,
        })

        # Bot 推送
        controller.bot_gateway.notify("task_delivered", {
            "name": task_name, "task_id": task_id[:12] if task_id else "",
        })

        logger.info("PM %s 交付物已接收: %s", pm_id[:12], task_name)
        print(f"[Task] 交付物已接收: {task_id}")
        return {"ok": True, "task_id": task_id, "message": "交付物已接收, 等待 Boss 验收"}

    async def accept_delivery(self, task_id: str) -> dict:
        """Boss 验收交付物。"""
        controller = self.context.controller
        db = controller.db
        state = controller.state
        self.require_secretary()
        task = db.get_task(task_id)
        if not task:
            raise ServiceError(status_code=404, detail="任务不存在")
        if not task.output_data or "_delivery" not in task.output_data:
            raise ServiceError(status_code=409, detail="该任务尚无交付物")
        task.output_data["_delivery"]["accepted"] = True
        task.status = "accepted"
        db.save_task(task)
        await self.context.broadcast(state, "task_updated", {"task_id": task_id, "status": "accepted"})
        logger.info("任务 %s 交付物已验收", task_id[:12])
        return {"ok": True, "message": "已验收"}

    async def reject_delivery(self, task_id: str, payload: dict = None) -> dict:
        """Boss 退回交付物。"""
        controller = self.context.controller
        db = controller.db
        state = controller.state
        self.require_secretary()
        task = db.get_task(task_id)
        if not task:
            raise ServiceError(status_code=404, detail="任务不存在")
        if not task.output_data or "_delivery" not in task.output_data:
            raise ServiceError(status_code=409, detail="该任务尚无交付物")
        reason = (payload or {}).get("reason", "")
        task.output_data["_delivery"]["accepted"] = False
        task.output_data["_delivery"]["reject_reason"] = reason
        task.status = "rejected"
        db.save_task(task)
        await self.context.broadcast(state, "task_updated", {"task_id": task_id, "status": "rejected"})
        logger.info("任务 %s 交付物被退回: %s", task_id[:12], reason[:100])
        return {"ok": True, "message": "已退回"}
