"""BotIngressService: domain behavior separated from the public compatibility facade."""

from __future__ import annotations
from typing import Any
from . import bot_gateway as policy
from .component_service import ComponentService
from .application_lifecycle import wait_for_stop

class BotIngressService(ComponentService):
    """BotIngressService business operations with explicit state ownership."""
    OPERATIONS = ('_start_telegram_polling', '_telegram_poll_loop', '_handle_callback_query', '_answer_callback_query', '_handle_telegram_command', '_handle_natural_language')

    def _start_telegram_polling(self, channel: policy.BotChannel) -> Any:
        """启动 Telegram Bot 长轮询（接收用户命令）。"""
        if self.owner._tg_polling:
            return
        from .application_lifecycle import stop_signal
        stop_signal(self.owner).clear()
        self.owner._tg_polling = True
        self.owner._tg_poll_thread = policy.threading.Thread(target=self.owner._telegram_poll_loop, args=(channel,), daemon=True, name='bot-telegram-poll')
        self.owner._tg_poll_thread.start()
        policy.logger.info('Telegram 轮询已启动，等待手机端命令...')

    def _telegram_poll_loop(self, channel: policy.BotChannel) -> Any:
        """Telegram getUpdates 长轮询循环。"""
        while self.owner._tg_polling and channel.enabled:
            try:
                url = f'https://api.telegram.org/bot{channel.bot_token}/getUpdates'
                params = {'offset': self.owner._tg_last_update_id + 1, 'timeout': 30}
                resp = policy.requests.get(url, params=params, timeout=35)
                if resp.status_code != 200:
                    wait_for_stop(self.owner, 5)
                    continue
                result = resp.json()
                if not result.get('ok'):
                    wait_for_stop(self.owner, 5)
                    continue
                for update in result.get('result', []):
                    self.owner._tg_last_update_id = update.get('update_id', self.owner._tg_last_update_id)
                    callback_query = update.get('callback_query')
                    if callback_query:
                        self.owner._handle_callback_query(channel, callback_query)
                        continue
                    message = update.get('message')
                    if not message:
                        continue
                    text = message.get('text', '').strip()
                    chat_id = str(message.get('chat', {}).get('id', ''))
                    if text:
                        self.owner._handle_telegram_command(channel, text, chat_id)
            except policy.requests.exceptions.Timeout:
                continue
            except Exception as e:
                policy.logger.error('Telegram 轮询异常: %s', e)
                wait_for_stop(self.owner, 5)

    def _handle_callback_query(self, channel: policy.BotChannel, callback_query: dict) -> Any:
        """优化2: 处理 Inline Keyboard 按钮点击。

        callback_data 格式: "decide:{pm_id}:{choice}"
        """
        data = callback_query.get('data', '')
        chat_id = str(callback_query.get('message', {}).get('chat', {}).get('id', ''))
        query_id = callback_query.get('id', '')
        self.owner._answer_callback_query(channel, query_id)
        if not data.startswith('decide:'):
            return
        parts = data.split(':', 2)
        if len(parts) < 3:
            return
        _, pm_id, choice = parts
        policy.logger.info('收到 Inline 决策: PM=%s, choice=%s (from %s)', pm_id[:12], choice, chat_id)
        if self.owner._chat_handler and hasattr(self.owner._chat_handler, 'controller'):
            try:
                controller = self.owner._chat_handler.controller
                result = controller.inject_input_to_pm(pm_id, {'response': choice, 'choice': choice, 'source': 'telegram_inline'})
                if result.get('ok'):
                    ack = f'✅ 已将决策「{choice}」发送给 PM Agent'
                else:
                    ack = f"❌ 发送失败: {result.get('message', '未知错误')}"
            except Exception as e:
                ack = f'⚠️ 处理异常: {e}'
        elif self.owner._command_handler:
            try:
                ack = self.owner._command_handler('decide', f'{pm_id} {choice}', chat_id)
            except Exception as e:
                ack = f'⚠️ 命令执行出错: {e}'
        else:
            ack = '⚠️ 无法处理决策: 秘书未激活'
        try:
            url = f'https://api.telegram.org/bot{channel.bot_token}/sendMessage'
            policy.requests.post(url, json={'chat_id': chat_id, 'text': ack}, timeout=10)
        except Exception as e:
            policy.logger.error('回复 Inline 决策确认失败: %s', e)

    def _answer_callback_query(self, channel: policy.BotChannel, callback_query_id: str) -> Any:
        """应答 Telegram callback_query (消除客户端 loading)。"""
        if not callback_query_id or not channel.bot_token:
            return
        url = f'https://api.telegram.org/bot{channel.bot_token}/answerCallbackQuery'
        try:
            policy.requests.post(url, json={'callback_query_id': callback_query_id}, timeout=5)
        except Exception:
            pass

    def _handle_telegram_command(self, channel: policy.BotChannel, text: str, chat_id: str) -> Any:
        """处理来自 Telegram 的命令 (优化15: 统一入口)。"""
        policy.logger.info('收到 Telegram 消息: %s (from %s)', text[:50], chat_id)
        if text == '/start' or text == '/help':
            reply = '🤖 LAN Mesh 工作站 Bot\n\n可用命令:\n/status — 工作站状态概览\n/hosts — 在线主机列表\n/tasks — 最近任务\n/help — 显示帮助\n\n或直接发送自然语言消息, 秘书将为您处理。\n示例: 「提交一个代码审查任务: 检查 xxx 项目」'
        elif text == '/ping':
            reply = 'pong 🏓'
        elif text.startswith('/'):
            if self.owner._command_handler:
                try:
                    parts = text.split(maxsplit=1)
                    cmd = parts[0].lstrip('/')
                    args = parts[1] if len(parts) > 1 else ''
                    reply = self.owner._command_handler(cmd, args, chat_id)
                except Exception as e:
                    reply = f'⚠️ 命令执行出错: {e}'
            else:
                reply = f'未知命令: {text}\n发送 /help 查看可用命令'
        else:
            self.owner._send_telegram_typing(channel, chat_id)
            reply = self.owner._handle_natural_language(text, chat_id)
        url = f'https://api.telegram.org/bot{channel.bot_token}/sendMessage'
        try:
            chunks = policy._split_message(reply, max_len=4000)
            for chunk in chunks:
                policy.requests.post(url, json={'chat_id': chat_id, 'text': chunk, 'parse_mode': 'HTML'}, timeout=10)
        except Exception as e:
            policy.logger.error('回复 Telegram 失败: %s', e)

    def _handle_natural_language(self, text: str, chat_id: str) -> str:
        """优化15: 统一处理自然语言消息。

        优先使用 ChatHandler (与 Web 端相同的 LLM + 操作意图),
        回退到 command_handler, 最后回退到提示信息。
        """
        if self.owner._chat_handler:
            try:
                from .conversation_service import ChatRequest, respond_to_chat
                controller = getattr(self.owner._chat_handler, 'controller', None)
                if controller is None:
                    result = self.owner._chat_handler.chat(text)
                else:
                    result = respond_to_chat(controller, ChatRequest(text), self.owner._chat_handler)
                reply = result.get('reply', '')
                if reply:
                    return reply
            except Exception as e:
                policy.logger.error('ChatHandler 处理异常: %s', e)
                try:
                    from .error_tracker import error_tracker
                    error_tracker.capture('bot', e, context={'point': 'chat_handler'})
                except Exception:
                    pass
                return f'⚠️ 秘书处理异常: {e}'
        if self.owner._command_handler:
            try:
                return self.owner._command_handler('chat', text, chat_id)
            except Exception as e:
                return f'⚠️ 命令执行出错: {e}'
        return f'收到: {text}\n秘书尚未激活, 无法处理自然语言消息。\n请先在 Web UI 激活 Secretary, 或发送 /help 查看可用命令。'
