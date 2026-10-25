"""Task submission HTTP service adapting to the shared synchronous scheduling service."""

from ._vendor.yomkserverpy.YomkServerPy import YomkServer
from .service_context import StationService, StationServiceContext
from .service_runtime import get_service_runtime
from .task_scheduling_service import get_scheduling_service


class TaskSubmissionService(StationService):
    """Preserve the HTTP contract while sharing scheduling with chat and queues."""

    def __init__(self, server: YomkServer, context: StationServiceContext):
        super().__init__(server, context, "/TaskSubmissionService")

    async def submit_task(self, payload: dict) -> dict:
        """提交新任务 — 创建 Task 记录并分配 PM Agent。

        新流程 (PM Agent 架构):
        1. 创建 Task 记录 (status=pending, pm_agent_id="")
        2. 选择合适 work_station (按评级/在线状态)
        3. POST 到目标 Worker /role/start-pm (携带 task_id, secretary_url)
        4. 更新 Task.pm_agent_id, status=running
        5. WebSocket 广播 task_submitted + pm_registered
        """
        self.require_secretary()
        get_scheduling_service(self.context.controller)
        return await get_service_runtime(self.context.controller).request(
            "/TaskSchedulingService/submit_task", payload=payload, check_secretary=True)
