"""Read attempt-level usage without changing existing resource endpoint signatures."""

from fastapi import APIRouter

from .service_context import StationService, StationServiceContext
from .service_http import add_service_route
from .service_runtime import ServiceError, get_service_runtime


class UsageAuditService(StationService):
    """A scoped view of durable call records, including unknown usage counts."""

    def attempts(self, task_id: str, project_id: str, limit: int = 100) -> dict:
        self.require_secretary()
        database = self.context.controller.db
        if database is None:
            raise ServiceError(503, '资源数据库尚未初始化')
        return {'items': database.repositories.resources.query_attempt_usage(task_id, project_id, limit)}


def build_usage_audit_routes(controller: object) -> APIRouter:
    router, runtime = APIRouter(), get_service_runtime(controller)
    service = runtime.ensure('/UsageAuditService', lambda server: UsageAuditService(
        server, StationServiceContext(controller, lambda *a: None), '/UsageAuditService'), ['attempts'])
    add_service_route(router, runtime, '/api/resources/usage/attempts', 'GET',
                      '/UsageAuditService', service.attempts)
    return router
