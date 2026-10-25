"""HTTP contract assembly for independently registered Yomk interaction services."""

from fastapi import APIRouter
from .service_context import StationServiceContext
from .service_http import add_service_route
from .service_runtime import get_service_runtime
from .station_routes_common import _broadcast
from .project_api_service import ProjectApiService
from .capability_api_service import CapabilityApiService


def build_project_routes(controller: object) -> APIRouter:
    """Assemble unchanged HTTP contracts over the domain request boundary."""
    router = APIRouter()
    runtime = get_service_runtime(controller)
    context = StationServiceContext(controller, _broadcast)
    services = {
        "ProjectApiService": ProjectApiService(runtime.server, context),
        "CapabilityApiService": CapabilityApiService(runtime.server, context),
    }
    contracts = [
        ("ProjectApiService", "create_project", "POST", "/api/projects"),
        ("ProjectApiService", "list_projects", "GET", "/api/projects"),
        ("ProjectApiService", "get_project", "GET", "/api/projects/{project_id}"),
        ("ProjectApiService", "update_project", "PUT", "/api/projects/{project_id}"),
        ("ProjectApiService", "get_project_blueprint", "GET", "/api/projects/{project_id}/blueprint"),
        ("ProjectApiService", "update_project_blueprint", "PUT", "/api/projects/{project_id}/blueprint"),
        ("ProjectApiService", "archive_project", "DELETE", "/api/projects/{project_id}"),
        ("ProjectApiService", "get_usage", "GET", "/api/projects/{project_id}/usage"),
        ("CapabilityApiService", "list_all_tools", "GET", "/tools/list"),
        ("CapabilityApiService", "call_tool", "POST", "/tools/call"),
        ("CapabilityApiService", "list_mcp_servers", "GET", "/tools/servers"),
        ("CapabilityApiService", "register_mcp_server", "POST", "/tools/servers"),
        ("CapabilityApiService", "unregister_mcp_server", "DELETE", "/tools/servers/{name}"),
        ("CapabilityApiService", "route_dry_run", "POST", "/api/route/dry-run"),
        ("CapabilityApiService", "list_models", "GET", "/api/models"),
        ("CapabilityApiService", "list_skills", "GET", "/api/station/skills"),
        ("CapabilityApiService", "skill_stats", "GET", "/api/station/skills/stats"),
        ("CapabilityApiService", "scan_skills", "GET", "/api/station/skills/scan"),
        ("CapabilityApiService", "list_skill_market", "GET", "/api/station/skills/market"),
        ("CapabilityApiService", "install_skill_from_market", "POST", "/api/station/skills/market/install"),
        ("CapabilityApiService", "uninstall_skill", "DELETE", "/api/station/skills/{skill_id}"),
        ("CapabilityApiService", "download_skill_package", "GET", "/api/station/skills/download"),
        ("CapabilityApiService", "get_skills_for_role", "GET", "/api/station/skills/role/{role}"),
        ("CapabilityApiService", "get_skill_detail", "GET", "/api/station/skills/{skill_id}"),
        ("CapabilityApiService", "assign_skill", "POST", "/api/station/skills/{skill_id}/assign"),
        ("CapabilityApiService", "revoke_skill", "DELETE", "/api/station/skills/{skill_id}/assign"),
        ("CapabilityApiService", "list_bot_channels", "GET", "/api/station/bot/channels"),
        ("CapabilityApiService", "add_bot_channel", "POST", "/api/station/bot/channels"),
        ("CapabilityApiService", "remove_bot_channel", "DELETE", "/api/station/bot/channels/{channel_type}"),
        ("CapabilityApiService", "test_bot_channel", "POST", "/api/station/bot/test/{channel_type}"),
    ]
    for name, service in services.items():
        operations = [op for owner, op, _, _ in contracts if owner == name]
        services[name] = runtime.ensure(f"/{name}", lambda server, s=service: s, operations)
    for owner, operation, method, path in contracts:
        add_service_route(router, runtime, path, method, f"/{owner}",
                          getattr(services[owner], operation))
    return router
