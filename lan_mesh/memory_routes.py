"""Additional memory/history routes; the existing conversation endpoints stay intact."""

from fastapi import APIRouter

from .memory_api_service import MemoryApiService
from .service_context import StationServiceContext
from .service_http import add_service_route
from .service_runtime import get_service_runtime
from .station_routes_common import _broadcast


def build_memory_routes(controller: object) -> APIRouter:
    """Register a native service behind the existing mesh authentication middleware."""
    router = APIRouter()
    runtime = get_service_runtime(controller)
    contracts = [
        ('execution_context', 'GET', '/api/tasks/{task_id}/memory-context'),
        ('search_history', 'GET', '/api/conversations/{conv_id}/history/search'),
        ('read_source', 'GET', '/api/conversations/{conv_id}/history/sources/{anchor}'),
        ('rebuild_index', 'POST', '/api/history/rebuild-index'),
        ('list_memories', 'GET', '/api/conversations/{conv_id}/memories'),
        ('create_memory', 'POST', '/api/conversations/{conv_id}/memories'),
        ('revise_memory', 'PATCH', '/api/conversations/{conv_id}/memories/{memory_id}'),
        ('memory_revisions', 'GET', '/api/conversations/{conv_id}/memories/{memory_id}/revisions'),
    ]
    service = runtime.ensure('/MemoryApiService', lambda server: MemoryApiService(
        server, StationServiceContext(controller, _broadcast)), [op for op, _, _ in contracts])
    for operation, method, path in contracts:
        add_service_route(router, runtime, path, method, '/MemoryApiService', getattr(service, operation))
    return router
