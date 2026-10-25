"""Typed shared conversation ingress for Web, conversation threads and Bot adapters."""

from dataclasses import dataclass

from ._vendor.yomkserverpy.YomkServerPy import YomkService
from .service_runtime import ServiceError, get_service_runtime


@dataclass(frozen=True)
class ChatRequest:
    """Transport-neutral chat command; optional fields preserve handler defaults."""

    message: str
    conv_id: str | None = None
    history: list | None = None
    discuss_context: dict | None = None
    pm_thread_id: str | None = None

    def options(self) -> dict:
        """Pass only supplied options, supporting existing minimal Bot handlers."""
        return {key: value for key, value in vars(self).items()
                if key != "message" and value is not None}


class ConversationService(YomkService):
    """Resolve the live Secretary handler for every transport invocation."""

    def __init__(self, server, controller: object):
        super().__init__(server)
        self.controller = controller
        self.set_name("/ConversationService")

    def respond(self, request: ChatRequest, handler: object = None) -> dict:
        """One validated command boundary, also callable from synchronous Bot threads."""
        if not isinstance(request, ChatRequest):
            raise ServiceError(400, "需要 ChatRequest 消息")
        if not isinstance(request.message, str) or not request.message:
            raise ServiceError(400, "消息不能为空")
        if getattr(self.controller, "secretary_active", True) is False:
            raise ServiceError(503, "Secretary 未激活,请在 Station 面板中点击「启动秘书」")
        current = handler if handler is not None else getattr(self.controller, "chat_handler", None)
        if current is None:
            raise ServiceError(503, "聊天处理器未初始化")
        return current.chat(request.message, **request.options())


def respond_to_chat(controller: object, request: ChatRequest, handler: object = None) -> dict:
    """Compose and invoke the same service from HTTP and Bot callbacks."""
    runtime = get_service_runtime(controller)
    runtime.ensure("/ConversationService", lambda server: ConversationService(server, controller),
                   ["respond"])
    return runtime.request_sync("/ConversationService/respond", request=request, handler=handler)
