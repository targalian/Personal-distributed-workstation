"""Collaboration HTTP adapters; existing mesh/user authorization applies unchanged."""

from fastapi import APIRouter
from .collaboration_service import CollaborationService
from .service_context import StationServiceContext
from .service_http import add_service_route
from .service_runtime import get_service_runtime


def build_collaboration_routes(controller: object) -> APIRouter:
    router, runtime = APIRouter(), get_service_runtime(controller)
    service = runtime.ensure('/CollaborationService', lambda server: CollaborationService(
        server, StationServiceContext(controller, lambda *a: None), '/CollaborationService'),
        ['overview', 'takeover', 'release', 'reply'])
    for method, suffix, operation in [('GET', '', 'overview'), ('POST', '/takeover', 'takeover'),
                                     ('POST', '/release', 'release'), ('POST', '/reply', 'reply')]:
        add_service_route(router, runtime, '/api/collaboration/{task_id}' + suffix, method,
                          '/CollaborationService', getattr(service, operation))
    return router
