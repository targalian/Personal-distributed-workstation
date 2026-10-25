"""ConversationApiService: transport-neutral interaction operations behind the original API."""

from starlette.concurrency import run_in_threadpool
from .logger import get_logger
from .service_context import StationService, StationServiceContext
from .service_runtime import ServiceError
from .conversation_service import ChatRequest, respond_to_chat

logger = get_logger("station_api")

class ConversationApiService(StationService):
    """Resolve current components per call and preserve legacy response contracts."""

    def __init__(self, server, context: StationServiceContext):
        super().__init__(server, context, "/ConversationApiService")

    async def secretary_chat(self, payload: dict) -> dict:
        """与秘书对话 — 处理用户消息并返回回复 (向后兼容, 内部转发到多对话)。"""
        controller = self.context.controller
        state = controller.state
        db = getattr(controller, 'db', None)
        self.require_secretary()
        chat_handler = getattr(controller, 'chat_handler', None)
        if not chat_handler:
            raise ServiceError(status_code=503, detail='聊天处理器未初始化')
        message = payload.get('message', '')
        if not message:
            raise ServiceError(status_code=400, detail='消息不能为空')
        conv_id = payload.get('conv_id', '')
        history = payload.get('history')
        raw_context = payload.get('discuss_context')
        discuss_context = raw_context if isinstance(raw_context, dict) else None
        result = await run_in_threadpool(respond_to_chat, controller, ChatRequest(message=message, conv_id=conv_id, history=history, discuss_context=discuss_context))
        await self.context.broadcast(state, 'chat_reply', result)
        return result

    async def secretary_chat_history(self, limit: int=50) -> dict:
        """返回当前活跃对话的聊天历史。"""
        controller = self.context.controller
        state = controller.state
        db = getattr(controller, 'db', None)
        self.require_secretary()
        chat_handler = getattr(controller, 'chat_handler', None)
        if not chat_handler:
            return {'history': [], 'total': 0}
        history = await run_in_threadpool(chat_handler.get_history, limit)
        return {'history': history, 'total': len(history)}

    async def secretary_chat_history_clear(self) -> dict:
        """清空当前对话历史。"""
        controller = self.context.controller
        state = controller.state
        db = getattr(controller, 'db', None)
        self.require_secretary()
        chat_handler = getattr(controller, 'chat_handler', None)
        if not chat_handler:
            return {'ok': True, 'message': '聊天处理器未初始化'}
        await run_in_threadpool(chat_handler.clear_history)
        return {'ok': True, 'message': '聊天历史已清空'}

    async def list_conversations(self) -> dict:
        """对话列表。"""
        controller = self.context.controller
        state = controller.state
        db = getattr(controller, 'db', None)
        self.require_secretary()
        ch = getattr(controller, 'chat_handler', None)
        if not ch:
            return {'conversations': []}
        return {'conversations': await run_in_threadpool(ch.list_conversations)}

    async def create_conversation(self, payload: dict) -> dict:
        """新建对话。"""
        controller = self.context.controller
        state = controller.state
        db = getattr(controller, 'db', None)
        self.require_secretary()
        ch = getattr(controller, 'chat_handler', None)
        if not ch:
            raise ServiceError(status_code=503, detail='聊天处理器未初始化')
        title = payload.get('title', '新对话')
        project_id = payload.get('project_id', '')
        conv = await run_in_threadpool(ch.create_conversation, title, project_id)
        await self.context.broadcast(state, 'conversation_created', conv)
        return conv

    async def get_conversation_messages(self, conv_id: str, limit: int=100) -> dict:
        """获取对话消息。"""
        controller = self.context.controller
        state = controller.state
        db = getattr(controller, 'db', None)
        self.require_secretary()
        ch = getattr(controller, 'chat_handler', None)
        if not ch:
            return {'messages': []}
        messages = await run_in_threadpool(ch.get_messages, conv_id, limit)
        return {'messages': messages, 'conv_id': conv_id}

    async def send_conversation_message(self, conv_id: str, payload: dict) -> dict:
        """在指定对话中发送消息。

        方案C: 若 payload 含 pm_thread_id, 则消息直接路由到 PM 线程 (L2)。
        """
        controller = self.context.controller
        state = controller.state
        db = getattr(controller, 'db', None)
        self.require_secretary()
        ch = getattr(controller, 'chat_handler', None)
        if not ch:
            raise ServiceError(status_code=503, detail='聊天处理器未初始化')
        message = payload.get('message', '')
        if not message:
            raise ServiceError(status_code=400, detail='消息不能为空')
        pm_thread_id = payload.get('pm_thread_id', '')
        result = await run_in_threadpool(respond_to_chat, controller, ChatRequest(message, conv_id=conv_id, pm_thread_id=pm_thread_id))
        await self.context.broadcast(state, 'chat_reply', result)
        return result

    async def delete_conversation(self, conv_id: str) -> dict:
        """删除对话。"""
        controller = self.context.controller
        state = controller.state
        db = getattr(controller, 'db', None)
        self.require_secretary()
        ch = getattr(controller, 'chat_handler', None)
        if not ch:
            return {'ok': False}
        ok = await run_in_threadpool(ch.delete_conversation, conv_id)
        return {'ok': ok}

    async def rename_conversation(self, conv_id: str, payload: dict) -> dict:
        """重命名对话。"""
        controller = self.context.controller
        state = controller.state
        db = getattr(controller, 'db', None)
        self.require_secretary()
        ch = getattr(controller, 'chat_handler', None)
        if not ch:
            return {'ok': False}
        title = payload.get('title', '')
        if not title:
            raise ServiceError(status_code=400, detail='标题不能为空')
        ok = await run_in_threadpool(ch.rename_conversation, conv_id, title)
        return {'ok': ok}

    async def list_pm_threads(self, conv_id: str) -> dict:
        """获取对话关联的 PM 线程列表。"""
        controller = self.context.controller
        state = controller.state
        db = getattr(controller, 'db', None)
        self.require_secretary()
        ch = getattr(controller, 'chat_handler', None)
        if not ch:
            return {'threads': []}
        threads = await run_in_threadpool(ch.get_pm_threads, conv_id)
        return {'threads': threads, 'conv_id': conv_id}

    async def attach_pm_thread(self, conv_id: str, payload: dict) -> dict:
        """将 PM Agent 绑定到对话线程。"""
        controller = self.context.controller
        state = controller.state
        db = getattr(controller, 'db', None)
        self.require_secretary()
        ch = getattr(controller, 'chat_handler', None)
        if not ch:
            raise ServiceError(status_code=503, detail='聊天处理器未初始化')
        pm_id = payload.get('pm_id', '')
        if not pm_id:
            raise ServiceError(status_code=400, detail='pm_id 不能为空')
        result = await run_in_threadpool(ch.attach_pm_thread, conv_id, pm_id, task_name=payload.get('task_name', ''), agent_name=payload.get('agent_name', ''))
        if result.get('ok'):
            await self.context.broadcast(state, 'pm_thread_attached', {'conv_id': conv_id, 'pm_id': pm_id})
        return result

    async def detach_pm_thread(self, conv_id: str, pm_id: str) -> dict:
        """从对话中移除 PM 线程。"""
        controller = self.context.controller
        state = controller.state
        db = getattr(controller, 'db', None)
        self.require_secretary()
        ch = getattr(controller, 'chat_handler', None)
        if not ch:
            return {'ok': False}
        ok = await run_in_threadpool(ch.detach_pm_thread, conv_id, pm_id)
        return {'ok': ok}

    async def get_pm_thread_messages(self, pm_id: str, limit: int=100) -> dict:
        """获取 PM 线程的历史消息。"""
        controller = self.context.controller
        state = controller.state
        db = getattr(controller, 'db', None)
        self.require_secretary()
        ch = getattr(controller, 'chat_handler', None)
        if not ch:
            return {'messages': []}
        messages = await run_in_threadpool(ch.get_pm_thread_messages, pm_id, limit)
        return {'messages': messages, 'pm_id': pm_id}

    async def send_pm_thread_message(self, pm_id: str, payload: dict) -> dict:
        """L2 路由: 在 PM 线程内直接发送消息给 PM Agent。

        跳过秘书 LLM, 直接注入 PM 的 receive_input。
        """
        controller = self.context.controller
        state = controller.state
        db = getattr(controller, 'db', None)
        self.require_secretary()
        ch = getattr(controller, 'chat_handler', None)
        if not ch:
            raise ServiceError(status_code=503, detail='聊天处理器未初始化')
        message = payload.get('message', '')
        if not message:
            raise ServiceError(status_code=400, detail='消息不能为空')
        conv_id = payload.get('conv_id', '') or await run_in_threadpool(ch.find_conv_by_pm, pm_id)
        if not conv_id:
            raise ServiceError(status_code=404, detail='PM 未绑定到任何对话')
        result = await run_in_threadpool(ch.send_to_pm_thread, conv_id, pm_id, message)
        await self.context.broadcast(state, 'pm_thread_message', {'pm_id': pm_id, 'conv_id': conv_id, 'reply': result.get('reply', '')})
        return result

    async def bot_message_ingress(self, payload: dict) -> dict:
        """优化15: Bot 消息统一入口 (Webhook 模式)。

        用于 Telegram Webhook 或其他 Bot 平台回调:
        - 接收外部 Bot 平台推送的用户消息
        - 转发给 ChatHandler 处理 (与 Web 聊天相同逻辑)
        - 返回回复内容, 由调用方推送给用户

        Payload:
            {"message": "...", "chat_id": "...", "platform": "telegram"}
        """
        controller = self.context.controller
        state = controller.state
        db = getattr(controller, 'db', None)
        self.require_secretary()
        chat_handler = getattr(controller, 'chat_handler', None)
        if not chat_handler:
            raise ServiceError(status_code=503, detail='秘书未激活')
        message = payload.get('message', '').strip()
        if not message:
            raise ServiceError(status_code=400, detail='消息不能为空')
        chat_id = payload.get('chat_id', '')
        platform = payload.get('platform', 'unknown')
        logger.info('Bot 消息入口 (%s): %s (from %s)', platform, message[:50], chat_id)
        result = await run_in_threadpool(respond_to_chat, controller, ChatRequest(message))
        await self.context.broadcast(state, 'bot_chat', {'platform': platform, 'chat_id': chat_id, 'message': message, 'reply': result.get('reply', '')})
        return {'ok': True, 'reply': result.get('reply', ''), 'action_taken': result.get('action_taken', '')}
