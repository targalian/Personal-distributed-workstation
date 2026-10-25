"""Station task HTTP contracts, backed by independently registered Yomk services."""

from fastapi import APIRouter

from .agent_service import AgentService
from .delivery_service import DeliveryService, _reflow_blueprint  # compatibility re-export
from .task_submission_service import TaskSubmissionService
from .task_service import TaskService
from .task_memory_service import TaskMemoryService
from .service_context import StationServiceContext
from .service_http import add_service_route
from .service_runtime import get_service_runtime
from .station_routes_common import _broadcast


def build_task_routes(controller: object) -> APIRouter:
    """Preserve existing API paths, validation and role gates over Yomk requests."""
    router = APIRouter()
    runtime = get_service_runtime(controller)
    context = StationServiceContext(controller, _broadcast)
    services = {
        "AgentService": AgentService(runtime.server, context),
        "TaskSubmissionService": TaskSubmissionService(runtime.server, context),
        "TaskService": TaskService(runtime.server, context),
        "DeliveryService": DeliveryService(runtime.server, context),
        "TaskMemoryService": TaskMemoryService(runtime.server, context),
    }
    contracts = [
        ("AgentService", "register_agent", "POST", "/api/agents/register"),
        ("AgentService", "list_agents", "GET", "/api/agents"),
        ("AgentService", "get_agent", "GET", "/api/agents/{agent_id}"),
        ("TaskSubmissionService", "submit_task", "POST", "/api/tasks"),
        ("TaskService", "list_tasks", "GET", "/api/tasks"),
        ("TaskService", "get_task", "GET", "/api/tasks/{task_id}"),
        ("TaskService", "cancel_task", "POST", "/api/tasks/{task_id}/cancel"),
        ("TaskService", "pause_task", "POST", "/api/tasks/{task_id}/pause"),
        ("TaskService", "delete_task", "DELETE", "/api/tasks/{task_id}"),
        ("TaskService", "resume_task", "POST", "/api/tasks/{task_id}/resume"),
        ("TaskService", "get_task_cost_estimate", "GET", "/api/tasks/{task_id}/cost-estimate"),
        ("TaskService", "get_task_graph", "GET", "/api/tasks/{task_id}/graph"),
        ("TaskService", "update_task_graph", "PUT", "/api/tasks/{task_id}/graph"),
        ("TaskService", "list_checkpoints", "GET", "/api/tasks/{task_id}/checkpoints"),
        ("TaskService", "inject_pm_input", "POST", "/api/pm/{pm_id}/inject-input"),
        ("DeliveryService", "receive_pm_delivery", "POST", "/api/pm/{pm_id}/deliver"),
        ("DeliveryService", "accept_delivery", "POST", "/api/tasks/{task_id}/accept"),
        ("DeliveryService", "reject_delivery", "POST", "/api/tasks/{task_id}/reject"),
        ("TaskMemoryService", "receive_task_memory", "POST", "/api/pm/{pm_id}/task-memory"),
        ("TaskMemoryService", "get_task_memory_stats", "GET", "/api/task-memory/stats"),
        ("TaskMemoryService", "task_memory_overview", "GET", "/api/task-memory/overview"),
    ]
    for name, service in services.items():
        operations = {op: getattr(service, op) for owner, op, _, _ in contracts if owner == name}
        runtime.register(service, operations)
    for owner, operation, method, path in contracts:
        add_service_route(router, runtime, path, method, f"/{owner}",
                          getattr(services[owner], operation))
    router.add_event_handler("shutdown", runtime.close)
    return router
