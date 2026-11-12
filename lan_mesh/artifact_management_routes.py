"""Additional scoped artifact management endpoints on Station and standalone Worker."""

from fastapi import APIRouter

from .artifact_management_service import ArtifactManagementService
from .service_http import add_service_route
from .service_runtime import get_service_runtime


def build_artifact_management_routes(owner: object) -> APIRouter:
    router, runtime = APIRouter(), get_service_runtime(owner)
    name = "/ArtifactManagementService"
    service = runtime.ensure(
        name, lambda server: ArtifactManagementService(server, owner), list(ArtifactManagementService.OPERATIONS)
    )
    contracts = [
        ("inventory", "GET", "/api/runtime/artifact-runs"),
        ("retention", "POST", "/api/runtime/artifact-runs/{execution_id}/retention"),
        ("preview", "POST", "/api/runtime/artifact-cleanup/preview"),
        ("cleanup", "POST", "/api/runtime/artifact-cleanup/execute"),
        ("export", "POST", "/api/runtime/artifact-runs/{execution_id}/export"),
        ("restore", "POST", "/api/runtime/artifact-replicas/{bundle_id}/restore"),
    ]
    for operation, method, path in contracts:
        add_service_route(router, runtime, path, method, name, getattr(service, operation))
    return router
