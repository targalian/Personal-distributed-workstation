"""HTTP contract assembly for independently registered Yomk interaction services."""

from fastapi import APIRouter
from .resource_usage_batch import apply_usage_batch
from .service_context import StationServiceContext
from .service_http import add_service_route
from .service_runtime import get_service_runtime
from .station_routes_common import _broadcast
from .resource_api_service import ResourceApiService
from .secret_exchange_service import SecretExchangeService


def build_resource_routes(controller: object) -> APIRouter:
    """Assemble unchanged HTTP contracts over the domain request boundary."""
    router = APIRouter()
    runtime = get_service_runtime(controller)
    context = StationServiceContext(controller, _broadcast)
    services = {
        "ResourceApiService": ResourceApiService(runtime.server, context),
        "SecretExchangeService": SecretExchangeService(runtime.server, context),
    }
    contracts = [
        ("ResourceApiService", "get_model_resources", "GET", "/api/resources"),
        ("ResourceApiService", "record_model_usage", "POST", "/api/resources/usage"),
        ("ResourceApiService", "get_cost_by_task", "GET", "/api/resources/cost"),
        ("ResourceApiService", "probe_model_balances", "POST", "/api/resources/probe"),
        ("ResourceApiService", "get_resources_config", "GET", "/api/resources/config"),
        ("ResourceApiService", "save_resources_config", "POST", "/api/resources/config"),
        ("SecretExchangeService", "fetch_secrets", "GET", "/api/secrets/fetch"),
        ("SecretExchangeService", "receive_secrets", "POST", "/api/secrets/receive"),
        ("SecretExchangeService", "sync_secrets_all", "POST", "/api/secrets/sync-all"),
        ("ResourceApiService", "test_resource_key", "POST", "/api/resources/test-key"),
        ("ResourceApiService", "check_resource_alerts", "POST", "/api/resources/alerts/check"),
        ("ResourceApiService", "get_rotation_plan", "GET", "/api/resources/rotation"),
        ("ResourceApiService", "get_recent_events", "GET", "/api/events/recent"),
        ("ResourceApiService", "get_role_cards", "GET", "/api/roles"),
    ]
    for name, service in services.items():
        operations = [op for owner, op, _, _ in contracts if owner == name]
        services[name] = runtime.ensure(f"/{name}", lambda server, s=service: s, operations)
    for owner, operation, method, path in contracts:
        add_service_route(router, runtime, path, method, f"/{owner}",
                          getattr(services[owner], operation))
    return router
