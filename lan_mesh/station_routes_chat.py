"""
Station 交互路由 — 秘书聊天/多对话/PM 线程/Bot 消息入口 (P1 #2 拆分产物)

Secretary 激活后可用:
  - 秘书聊天 (向后兼容端点) 与历史管理
  - 多对话 CRUD (方案C L1 层)
  - PM 线程绑定/解绑/消息 (方案C L2 层, 跳过秘书 LLM 直达 PM)
  - Bot 统一消息入口 (优化15, Webhook 模式)
"""
from fastapi import APIRouter, HTTPException

from .logger import get_logger
from .station_routes_common import _broadcast, check_secretary

logger = get_logger("station_api")


def build_chat_routes(controller) -> APIRouter:
    """Secretary 交互域路由。"""
    router = APIRouter()

    # 便捷别名
    state = controller.state

    # ── 秘书聊天 (向后兼容) ──

    @router.post("/api/secretary/chat")
    async def secretary_chat(payload: dict):
        """与秘书对话 — 处理用户消息并返回回复 (向后兼容, 内部转发到多对话)。"""
        check_secretary(controller)
        chat_handler = getattr(controller, 'chat_handler', None)
        if not chat_handler:
            raise HTTPException(status_code=503, detail="聊天处理器未初始化")
        message = payload.get("message", "")
        if not message:
            raise HTTPException(status_code=400, detail="消息不能为空")
        conv_id = payload.get("conv_id", "")
        history = payload.get("history")
        result = chat_handler.chat(message, conv_id=conv_id, history=history)
        await _broadcast(state, "chat_reply", result)
        return result

    @router.get("/api/secretary/chat/history")
    async def secretary_chat_history(limit: int = 50):
        """返回当前活跃对话的聊天历史。"""
        check_secretary(controller)
        chat_handler = getattr(controller, 'chat_handler', None)
        if not chat_handler:
            return {"history": [], "total": 0}
        history = chat_handler.get_history(limit)
        return {"history": history, "total": len(history)}

    @router.delete("/api/secretary/chat/history")
    async def secretary_chat_history_clear():
        """清空当前对话历史。"""
        check_secretary(controller)
        chat_handler = getattr(controller, 'chat_handler', None)
        if not chat_handler:
            return {"ok": True, "message": "聊天处理器未初始化"}
        chat_handler.clear_history()
        return {"ok": True, "message": "聊天历史已清空"}

    # ── 多对话管理 API ──

    @router.get("/api/conversations")
    async def list_conversations():
        """对话列表。"""
        check_secretary(controller)
        ch = getattr(controller, 'chat_handler', None)
        if not ch:
            return {"conversations": []}
        return {"conversations": ch.list_conversations()}

    @router.post("/api/conversations")
    async def create_conversation(payload: dict):
        """新建对话。"""
        check_secretary(controller)
        ch = getattr(controller, 'chat_handler', None)
        if not ch:
            raise HTTPException(status_code=503, detail="聊天处理器未初始化")
        title = payload.get("title", "新对话")
        project_id = payload.get("project_id", "")
        conv = ch.create_conversation(title, project_id)
        await _broadcast(state, "conversation_created", conv)
        return conv

    @router.get("/api/conversations/{conv_id}/messages")
    async def get_conversation_messages(conv_id: str, limit: int = 100):
        """获取对话消息。"""
        check_secretary(controller)
        ch = getattr(controller, 'chat_handler', None)
        if not ch:
            return {"messages": []}
        messages = ch.get_messages(conv_id, limit)
        return {"messages": messages, "conv_id": conv_id}

    @router.post("/api/conversations/{conv_id}/messages")
    async def send_conversation_message(conv_id: str, payload: dict):
        """在指定对话中发送消息。

        方案C: 若 payload 含 pm_thread_id, 则消息直接路由到 PM 线程 (L2)。
        """
        check_secretary(controller)
        ch = getattr(controller, 'chat_handler', None)
        if not ch:
            raise HTTPException(status_code=503, detail="聊天处理器未初始化")
        message = payload.get("message", "")
        if not message:
            raise HTTPException(status_code=400, detail="消息不能为空")
        pm_thread_id = payload.get("pm_thread_id", "")
        result = ch.chat(message, conv_id=conv_id, pm_thread_id=pm_thread_id)
        await _broadcast(state, "chat_reply", result)
        return result

    @router.delete("/api/conversations/{conv_id}")
    async def delete_conversation(conv_id: str):
        """删除对话。"""
        check_secretary(controller)
        ch = getattr(controller, 'chat_handler', None)
        if not ch:
            return {"ok": False}
        ok = ch.delete_conversation(conv_id)
        return {"ok": ok}

    @router.put("/api/conversations/{conv_id}/title")
    async def rename_conversation(conv_id: str, payload: dict):
        """重命名对话。"""
        check_secretary(controller)
        ch = getattr(controller, 'chat_handler', None)
        if not ch:
            return {"ok": False}
        title = payload.get("title", "")
        if not title:
            raise HTTPException(status_code=400, detail="标题不能为空")
        ok = ch.rename_conversation(conv_id, title)
        return {"ok": ok}

    # ── 方案C: PM 线程 API (L2 层) ──

    @router.get("/api/conversations/{conv_id}/pm-threads")
    async def list_pm_threads(conv_id: str):
        """获取对话关联的 PM 线程列表。"""
        check_secretary(controller)
        ch = getattr(controller, 'chat_handler', None)
        if not ch:
            return {"threads": []}
        threads = ch.get_pm_threads(conv_id)
        return {"threads": threads, "conv_id": conv_id}

    @router.post("/api/conversations/{conv_id}/pm-threads")
    async def attach_pm_thread(conv_id: str, payload: dict):
        """将 PM Agent 绑定到对话线程。"""
        check_secretary(controller)
        ch = getattr(controller, 'chat_handler', None)
        if not ch:
            raise HTTPException(status_code=503, detail="聊天处理器未初始化")
        pm_id = payload.get("pm_id", "")
        if not pm_id:
            raise HTTPException(status_code=400, detail="pm_id 不能为空")
        result = ch.attach_pm_thread(
            conv_id, pm_id,
            task_name=payload.get("task_name", ""),
            agent_name=payload.get("agent_name", ""),
        )
        if result.get("ok"):
            await _broadcast(state, "pm_thread_attached", {"conv_id": conv_id, "pm_id": pm_id})
        return result

    @router.delete("/api/conversations/{conv_id}/pm-threads/{pm_id}")
    async def detach_pm_thread(conv_id: str, pm_id: str):
        """从对话中移除 PM 线程。"""
        check_secretary(controller)
        ch = getattr(controller, 'chat_handler', None)
        if not ch:
            return {"ok": False}
        ok = ch.detach_pm_thread(conv_id, pm_id)
        return {"ok": ok}

    @router.get("/api/pm-threads/{pm_id}/messages")
    async def get_pm_thread_messages(pm_id: str, limit: int = 100):
        """获取 PM 线程的历史消息。"""
        check_secretary(controller)
        ch = getattr(controller, 'chat_handler', None)
        if not ch:
            return {"messages": []}
        messages = ch.get_pm_thread_messages(pm_id, limit)
        return {"messages": messages, "pm_id": pm_id}

    @router.post("/api/pm-threads/{pm_id}/messages")
    async def send_pm_thread_message(pm_id: str, payload: dict):
        """L2 路由: 在 PM 线程内直接发送消息给 PM Agent。

        跳过秘书 LLM, 直接注入 PM 的 receive_input。
        """
        check_secretary(controller)
        ch = getattr(controller, 'chat_handler', None)
        if not ch:
            raise HTTPException(status_code=503, detail="聊天处理器未初始化")
        message = payload.get("message", "")
        if not message:
            raise HTTPException(status_code=400, detail="消息不能为空")
        conv_id = payload.get("conv_id", "") or ch.find_conv_by_pm(pm_id)
        if not conv_id:
            raise HTTPException(status_code=404, detail="PM 未绑定到任何对话")
        result = ch.send_to_pm_thread(conv_id, pm_id, message)
        await _broadcast(state, "pm_thread_message", {
            "pm_id": pm_id, "conv_id": conv_id,
            "reply": result.get("reply", ""),
        })
        return result

    # ── 优化15: Bot 统一消息入口 ──

    @router.post("/api/bot/message")
    async def bot_message_ingress(payload: dict):
        """优化15: Bot 消息统一入口 (Webhook 模式)。

        用于 Telegram Webhook 或其他 Bot 平台回调:
        - 接收外部 Bot 平台推送的用户消息
        - 转发给 ChatHandler 处理 (与 Web 聊天相同逻辑)
        - 返回回复内容, 由调用方推送给用户

        Payload:
            {"message": "...", "chat_id": "...", "platform": "telegram"}
        """
        check_secretary(controller)
        chat_handler = getattr(controller, 'chat_handler', None)
        if not chat_handler:
            raise HTTPException(status_code=503, detail="秘书未激活")

        message = payload.get("message", "").strip()
        if not message:
            raise HTTPException(status_code=400, detail="消息不能为空")

        chat_id = payload.get("chat_id", "")
        platform = payload.get("platform", "unknown")
        logger.info("Bot 消息入口 (%s): %s (from %s)", platform, message[:50], chat_id)

        # 统一转发给 ChatHandler
        result = chat_handler.chat(message)

        # 广播到 Web UI (显示 Bot 来源的对话)
        await _broadcast(state, "bot_chat", {
            "platform": platform,
            "chat_id": chat_id,
            "message": message,
            "reply": result.get("reply", ""),
        })

        return {
            "ok": True,
            "reply": result.get("reply", ""),
            "action_taken": result.get("action_taken", ""),
        }

    return router
