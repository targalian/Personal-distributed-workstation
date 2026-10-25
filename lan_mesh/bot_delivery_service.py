"""BotDeliveryService: domain behavior separated from the public compatibility facade."""

from __future__ import annotations
from typing import Any
from . import bot_gateway as policy
from .component_service import ComponentService

class BotDeliveryService(ComponentService):
    """BotDeliveryService business operations with explicit state ownership."""
    OPERATIONS = ('notify', '_do_send_to_channel', 'flush_pending_queue', '_send_wechat', '_send_telegram', '_send_telegram_with_keyboard', '_send_telegram_typing')

    def notify(self, event_type: str, data: dict=None) -> Any:
        """推送事件通知到所有启用的通道。

        Args:
            event_type: 事件类型（见 EVENT_TEMPLATES）
            data: 模板变量字典
        """
        data = data or {}
        template = policy.EVENT_TEMPLATES.get(event_type)
        if not template:
            message = f'📢 {event_type}\n{policy.json.dumps(data, ensure_ascii=False, indent=2)}'
        else:
            try:
                message = template.format(**data)
            except (KeyError, IndexError):
                message = f'📢 {event_type}\n{policy.json.dumps(data, ensure_ascii=False)}'
        priority = policy.EVENT_PRIORITY.get(event_type, 'normal')
        if self.owner._quiet_checker.should_block(priority):
            policy.logger.debug('免打扰时段, 跳过 %s 推送 (priority=%s)', event_type, priority)
            return
        with self.owner._lock:
            channels = list(self.owner._channels)
        for ch in channels:
            if not ch.enabled:
                continue
            if not policy._should_send(priority, ch.min_priority):
                continue
            if event_type == 'pm_awaiting_input' and ch.channel_type == 'telegram':
                options = data.get('options', [])
                pm_id = data.get('pm_id', '')
                self.owner._send_telegram_with_keyboard(ch, message, pm_id, options)
                continue
            self.owner._aggregator.push(ch, message, event_type, priority)

    def _do_send_to_channel(self, channel: policy.BotChannel, message: str, event_type: str) -> Any:
        """实际发送 (聚合器回调 + 直接发送共用)。含重试逻辑。"""
        for attempt in range(self.owner._max_retry):
            try:
                if channel.channel_type == 'wechat_webhook':
                    self.owner._send_wechat(channel, message)
                elif channel.channel_type == 'telegram':
                    self.owner._send_telegram(channel, message)
                return
            except Exception as e:
                if attempt < self.owner._max_retry - 1:
                    wait = self.owner._retry_backoff ** attempt
                    policy.logger.warning('发送到 %s 失败 (第%d次), %.1fs后重试: %s', channel.channel_type, attempt + 1, wait, e)
                    policy.time.sleep(wait)
                else:
                    policy.logger.error('发送到 %s 最终失败, 消息进入离线队列: %s', channel.channel_type, e)
                    try:
                        from .error_tracker import error_tracker
                        error_tracker.capture('bot', e, context={'point': 'channel_send', 'channel': channel.channel_type, 'event_type': event_type})
                    except Exception:
                        pass
                    self.owner._pending_queue.append({'channel_type': channel.channel_type, 'message': message, 'event_type': event_type, 'failed_at': policy.time.time()})

    def flush_pending_queue(self) -> Any:
        """手动触发离线队列补发 (可在通道恢复后调用)。"""
        flushed = 0
        while self.owner._pending_queue:
            item = self.owner._pending_queue[0]
            ch = self.owner.get_channel(item['channel_type'])
            if not ch or not ch.enabled:
                break
            try:
                if ch.channel_type == 'wechat_webhook':
                    self.owner._send_wechat(ch, item['message'])
                elif ch.channel_type == 'telegram':
                    self.owner._send_telegram(ch, item['message'])
                self.owner._pending_queue.popleft()
                flushed += 1
            except Exception:
                break
        if flushed:
            policy.logger.info('离线队列补发 %d 条消息', flushed)
        return flushed

    def _send_wechat(self, channel: policy.BotChannel, message: str) -> Any:
        """企业微信群机器人 Webhook 推送。"""
        if not channel.webhook_url:
            return
        payload = {'msgtype': 'text', 'text': {'content': message}}
        resp = policy.requests.post(channel.webhook_url, json=payload, timeout=10)
        if resp.status_code != 200:
            raise RuntimeError(f'微信 webhook 返回 {resp.status_code}: {resp.text[:200]}')
        result = resp.json()
        if result.get('errcode', 0) != 0:
            raise RuntimeError(f'微信 webhook 错误: {result}')

    def _send_telegram(self, channel: policy.BotChannel, message: str) -> Any:
        """Telegram Bot API 推送消息。"""
        if not channel.bot_token or not channel.chat_id:
            return
        url = f'https://api.telegram.org/bot{channel.bot_token}/sendMessage'
        chunks = policy._split_message(message, max_len=4000)
        for chunk in chunks:
            payload = {'chat_id': channel.chat_id, 'text': chunk, 'parse_mode': 'HTML'}
            resp = policy.requests.post(url, json=payload, timeout=10)
            if resp.status_code != 200:
                raise RuntimeError(f'Telegram 返回 {resp.status_code}: {resp.text[:200]}')

    def _send_telegram_with_keyboard(self, channel: policy.BotChannel, message: str, pm_id: str, options: list) -> Any:
        """优化2: 发送带 Inline Keyboard 的 Telegram 消息 (PM 决策按钮)。"""
        if not channel.bot_token or not channel.chat_id:
            return
        url = f'https://api.telegram.org/bot{channel.bot_token}/sendMessage'
        keyboard_rows = []
        if options:
            for opt in options[:6]:
                keyboard_rows.append([{'text': opt, 'callback_data': f'decide:{pm_id}:{opt}'}])
        keyboard_rows.append([{'text': '✅ 同意继续', 'callback_data': f'decide:{pm_id}:approve'}, {'text': '❌ 放弃任务', 'callback_data': f'decide:{pm_id}:abort'}])
        payload = {'chat_id': channel.chat_id, 'text': message, 'parse_mode': 'HTML', 'reply_markup': policy.json.dumps({'inline_keyboard': keyboard_rows})}
        try:
            resp = policy.requests.post(url, json=payload, timeout=10)
            if resp.status_code != 200:
                policy.logger.warning('Telegram keyboard 消息发送失败: %d', resp.status_code)
        except Exception as e:
            policy.logger.error('Telegram keyboard 发送异常: %s', e)

    def _send_telegram_typing(self, channel: policy.BotChannel, chat_id: str) -> Any:
        """优化3: 发送 typing 状态指示 (让用户知道 Bot 正在处理)。"""
        if not channel.bot_token:
            return
        url = f'https://api.telegram.org/bot{channel.bot_token}/sendChatAction'
        try:
            policy.requests.post(url, json={'chat_id': chat_id, 'action': 'typing'}, timeout=5)
        except Exception:
            pass
