"""Administrator-only private backup writes; receivers can remain standby Stations."""

from fastapi import APIRouter

from .private_backup_service import PrivateBackupService
from .service_http import add_service_route
from .service_runtime import get_service_runtime


def build_private_backup_routes(owner: object) -> APIRouter:
    router, runtime = APIRouter(), get_service_runtime(owner)
    name = '/PrivateBackupService'
    service = runtime.ensure(name, lambda server: PrivateBackupService(server, owner),
                             list(PrivateBackupService.OPERATIONS))
    contracts = [
        ('export_memory', 'POST', '/api/station/memory-backups/export'),
        ('restore_memory', 'POST', '/api/station/memory-backups/{bundle_id}/restore'),
        ('inventory', 'GET', '/api/station/private-bundles'),
        ('peers', 'GET', '/api/station/private-bundle-peers'),
        ('status', 'GET', '/api/station/private-bundles/{bundle_id}'),
        ('receive', 'POST', '/api/station/private-bundles/{bundle_id}/chunks'),
        ('push', 'POST', '/api/station/private-bundles/{bundle_id}/push'),
        ('delete', 'POST', '/api/station/private-bundles/{bundle_id}/delete'),
    ]
    for operation, method, path in contracts:
        add_service_route(router, runtime, path, method, name, getattr(service, operation))
    return router
