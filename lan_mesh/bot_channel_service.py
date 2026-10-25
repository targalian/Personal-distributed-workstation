"""BotChannelService: domain behavior separated from the public compatibility facade."""

from __future__ import annotations
from typing import Any
from . import bot_gateway as policy
from .component_service import ComponentService

class BotChannelService(ComponentService):
    """BotChannelService business operations with explicit state ownership."""
    OPERATIONS = ('add_channel', 'remove_channel', 'list_channels', 'get_channel', 'set_command_handler', 'set_chat_handler', 'test_channel')

    def add_channel(self, channel: policy.BotChannel) -> Any:
        """添加或更新一个 Bot 通道。"""
        with self.owner._lock:
            self.owner._channels = [c for c in self.owner._channels if c.channel_type != channel.channel_type]
            self.owner._channels.append(channel)
        if channel.channel_type == 'telegram' and channel.enabled:
            self.owner._start_telegram_polling(channel)

    def remove_channel(self, channel_type: str) -> Any:
        """移除一个通道。"""
        with self.owner._lock:
            self.owner._channels = [c for c in self.owner._channels if c.channel_type != channel_type]

    def list_channels(self) -> list[dict]:
        """列出所有通道配置（脱敏）。"""
        with self.owner._lock:
            return [{'channel_type': c.channel_type, 'enabled': c.enabled, 'min_priority': c.min_priority, 'webhook_url': policy._mask(c.webhook_url) if c.webhook_url else '', 'bot_token': policy._mask(c.bot_token) if c.bot_token else '', 'chat_id': c.chat_id, 'webhook_url_base': c.webhook_url_base} for c in self.owner._channels]

    def get_channel(self, channel_type: str) -> policy.Optional[policy.BotChannel]:
        """获取指定类型的通道。"""
        with self.owner._lock:
            for c in self.owner._channels:
                if c.channel_type == channel_type:
                    return c
        return None

    def set_command_handler(self, handler: policy.Callable) -> Any:
        """设置命令处理回调。

        handler 签名: handler(command: str, args: str, chat_id: str) -> str
        返回值作为回复消息发送给用户。
        """
        self.owner._command_handler = handler

    def set_chat_handler(self, chat_handler) -> Any:
        """优化15: 设置 ChatHandler 实例, 统一处理自然语言消息。

        设置后, Bot 收到的非斜杠命令消息将转发给 ChatHandler 处理,
        与 Web 聊天窗口使用相同的 LLM + 操作意图检测逻辑。
        """
        self.owner._chat_handler = chat_handler

    def test_channel(self, channel_type: str) -> dict:
        """发送测试消息到指定通道。"""
        ch = self.owner.get_channel(channel_type)
        if not ch:
            return {'ok': False, 'error': f'通道 {channel_type} 不存在'}
        if not ch.enabled:
            return {'ok': False, 'error': f'通道 {channel_type} 未启用'}
        try:
            self.owner._do_send_to_channel(ch, policy.EVENT_TEMPLATES['bot_test'], 'bot_test')
            return {'ok': True, 'message': '测试消息已发送，请检查手机端'}
        except Exception as e:
            return {'ok': False, 'error': str(e)}
