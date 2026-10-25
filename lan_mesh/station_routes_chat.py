"""HTTP contract assembly for independently registered Yomk interaction services."""

from fastapi import APIRouter
from .service_context import StationServiceContext
from .service_http import add_service_route
from .service_runtime import get_service_runtime
from .station_routes_common import _broadcast
from .conversation_api_service import ConversationApiService


def build_chat_routes(controller: object) -> APIRouter:
    """Assemble unchanged HTTP contracts over the domain request boundary."""
    router = APIRouter()
    runtime = get_service_runtime(controller)
    context = StationServiceContext(controller, _broadcast)
    services = {
        "ConversationApiService": ConversationApiService(runtime.server, context),
    }
    contracts = [
        ("ConversationApiService", "secretary_chat", "POST", "/api/secretary/chat"),
        ("ConversationApiService", "secretary_chat_history", "GET", "/api/secretary/chat/history"),
        ("ConversationApiService", "secretary_chat_history_clear", "DELETE", "/api/secretary/chat/history"),
        ("ConversationApiService", "list_conversations", "GET", "/api/conversations"),
        ("ConversationApiService", "create_conversation", "POST", "/api/conversations"),
        ("ConversationApiService", "get_conversation_messages", "GET", "/api/conversations/{conv_id}/messages"),
        ("ConversationApiService", "send_conversation_message", "POST", "/api/conversations/{conv_id}/messages"),
        ("ConversationApiService", "delete_conversation", "DELETE", "/api/conversations/{conv_id}"),
        ("ConversationApiService", "rename_conversation", "PUT", "/api/conversations/{conv_id}/title"),
        ("ConversationApiService", "list_pm_threads", "GET", "/api/conversations/{conv_id}/pm-threads"),
        ("ConversationApiService", "attach_pm_thread", "POST", "/api/conversations/{conv_id}/pm-threads"),
        ("ConversationApiService", "detach_pm_thread", "DELETE", "/api/conversations/{conv_id}/pm-threads/{pm_id}"),
        ("ConversationApiService", "get_pm_thread_messages", "GET", "/api/pm-threads/{pm_id}/messages"),
        ("ConversationApiService", "send_pm_thread_message", "POST", "/api/pm-threads/{pm_id}/messages"),
        ("ConversationApiService", "bot_message_ingress", "POST", "/api/bot/message"),
    ]
    for name, service in services.items():
        operations = [op for owner, op, _, _ in contracts if owner == name]
        services[name] = runtime.ensure(f"/{name}", lambda server, s=service: s, operations)
    for owner, operation, method, path in contracts:
        add_service_route(router, runtime, path, method, f"/{owner}",
                          getattr(services[owner], operation))
    return router
