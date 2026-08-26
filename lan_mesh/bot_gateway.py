"""
Bot 网关 — 手机消息通道

职责:
1. 将工作站事件推送到手机（企业微信群机器人 / Telegram Bot）
2. 接收手机端命令（Telegram Bot webhook）
3. 作为 Secretary 与手机用户的异步交互通道

架构:
  StationController
    └── BotGateway
          ├── 企业微信群机器人 Webhook (单向推送)
          └── Telegram Bot API (双向: 推送 + 命令)
                ├── sendMessage (推)
                └── getUpdates / webhook (收)

优化清单:
  - 消息聚合/防刷屏 (短时间窗口内事件合并为一条)
  - Telegram Inline Keyboard (PM 决策一键操作)
  - typing 状态指示 + 发送重试/离线队列
  - 免打扰时段 (Quiet Hours)

事件类型:
  - task_submitted / task_completed / task_failed
  - host_online / host_offline
  - secretary_activated / secretary_deactivated
  - skill_assigned / skill_revoked
  - budget_warning
"""
import json
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Callable

import requests

from .logger import get_logger

logger = get_logger("bot")


# ── 事件消息模板 ──────────────────────────────────────────────

EVENT_TEMPLATES = {
    "task_submitted": "📋 新任务: {name}\n📝 {description}\n🆔 {task_id}",
    "task_completed": "✅ 任务完成: {name}\n🆔 {task_id}",
    "task_failed": "❌ 任务失败: {name}\n原因: {reason}\n🆔 {task_id}",
    "task_cancelled": "🚫 任务已取消: {name}\n🆔 {task_id}",
    "task_paused": "⏸️ 任务已暂停: {name}\n🆔 {task_id}",
    "host_online": "🖥️ 主机上线: {device_name} ({ip})",
    "host_offline": "⚠️ 主机离线: {device_name}",
    "secretary_activated": "🔑 Secretary 已激活 (端口 {port})",
    "secretary_deactivated": "🔴 Secretary 已停用",
    "skill_assigned": "📚 技能分配: {skill_id} → {assignee_type}:{assignee_id}",
    "skill_revoked": "📚 技能撤销: {skill_id} ← {assignee_type}:{assignee_id}",
    "budget_warning": "💰 预算告警: 项目 {project_id} 已用 {used:.2f}/{limit:.2f} USD",
    "bot_test": "🤖 Bot 通道测试 — 如果你看到这条消息，说明配置正确！",
    "pm_awaiting_input": "❓ PM {pm_id} 请求决策: {question}",
    "task_delivered": "📦 任务交付: {name}\n🆔 {task_id}\n请验收或退回",
    "task_escalated": "🚨 任务升级: {task_name}\n子任务 {failed_subtask} 失败\n错误: {error}\n需要您的决策!",
    "periodic_report": "{summary}",
    # R7: 资源预警 (三档优先级, 供通道 min_priority 过滤/免打扰)
    "resource_alert_low": "🔔 资源提醒 [{resource_id}] {message}",
    "resource_alert": "⚠️ 资源预警 [{resource_id}] {message}",
    "resource_alert_high": "🚨 资源紧急 [{resource_id}] {message}",
    # iter-41: 任务停滞告警 (三档, 与资源预警对齐)
    "task_stall_alert_low": "🕐 任务停滞提醒 [{task_id}] {message}",
    "task_stall_alert": "⚠️ 任务停滞告警 [{task_id}] {message}",
    "task_stall_alert_high": "🚨 任务停滞紧急 [{task_id}] {message}",
    # iter-44: 错误突发告警 (窗口内错误数超阈值, 冷却去重防刷屏)
    "error_burst": "❗ 错误突发 [{module}] {window:.0f} 秒窗口内 {count} 条错误, 请检查日志",
}

# 事件严重级别 → 决定是否推送（避免低优先级事件打扰）
EVENT_PRIORITY = {
    "task_submitted": "normal",
    "task_completed": "normal",
    "task_failed": "high",
    "task_cancelled": "high",
    "task_paused": "normal",
    "host_online": "low",
    "host_offline": "normal",
    "secretary_activated": "high",
    "secretary_deactivated": "high",
    "skill_assigned": "low",
    "skill_revoked": "low",
    "budget_warning": "high",
    "bot_test": "high",
    "pm_awaiting_input": "high",
    "task_delivered": "high",
    "task_escalated": "high",
    "periodic_report": "low",
    "resource_alert_low": "low",
    "resource_alert": "normal",
    "resource_alert_high": "high",
    "task_stall_alert_low": "low",
    "task_stall_alert": "normal",
    "task_stall_alert_high": "high",
    "error_burst": "high",
}

# 事件图标 (聚合消息时使用)
EVENT_ICONS = {
    "task_submitted": "📋",
    "task_completed": "✅",
    "task_failed": "❌",
    "task_cancelled": "🚫",
    "task_paused": "⏸️",
    "host_online": "🖥️",
    "host_offline": "⚠️",
    "secretary_activated": "🔑",
    "secretary_deactivated": "🔴",
    "budget_warning": "💰",
    "pm_awaiting_input": "❓",
    "task_delivered": "📦",
    "task_escalated": "🚨",
    "periodic_report": "📊",
    "error_burst": "❗",
}


@dataclass
class BotChannel:
    """单个 Bot 通道配置。"""
    channel_type: str = ""        # wechat_webhook | telegram
    webhook_url: str = ""         # 企业微信群机器人 webhook URL
    bot_token: str = ""          # Telegram bot token
    chat_id: str = ""            # Telegram chat_id
    enabled: bool = False
    min_priority: str = "normal"  # low | normal | high — 推送此级别及以上
    # Telegram webhook 模式
    webhook_url_base: str = ""   # 公网回调地址 (如 https://example.com)


# ── 优化1: 消息聚合器 ──────────────────────────────────────────


class MessageAggregator:
    """消息聚合器 — 短时间窗口内的事件合并为一条摘要推送, 防止刷屏。

    工作原理:
    - 收到事件后放入缓冲区, 启动 window_secs 的定时器
    - 定时器到期后, 将缓冲区内所有事件合并为一条消息发送
    - 若窗口内只有 1 条事件, 直接原样发送 (不聚合)
    - high 优先级事件立即发送, 不参与聚合
    """

    def __init__(self, window_secs: int = 30, flush_callback: Optional[Callable] = None):
        self._window = window_secs
        self._flush_callback = flush_callback  # fn(channel, message, event_type)
        self._buffer: list[dict] = []
        self._lock = threading.Lock()
        self._timer: Optional[threading.Timer] = None

    @property
    def enabled(self) -> bool:
        return self._window > 0

    def push(self, channel: "BotChannel", message: str, event_type: str, priority: str):
        """将事件推入聚合缓冲区。

        high 优先级事件立即刷新, 不参与聚合。
        注意: 直接发送路径必须在后台线程执行, notify() 由 async 端点调用,
        同步网络请求+重试退避会阻塞 FastAPI 事件循环。
        """
        if not self.enabled or priority == "high":
            # 聚合禁用或 high 优先级: 直接发送 (后台线程, 不阻塞调用方)
            if self._flush_callback:
                threading.Thread(
                    target=self._flush_callback,
                    args=(channel, message, event_type),
                    daemon=True,
                ).start()
            return

        with self._lock:
            self._buffer.append({
                "channel": channel,
                "message": message,
                "event_type": event_type,
                "priority": priority,
                "ts": time.time(),
            })
            # 首条消息时启动定时器
            if self._timer is None:
                self._timer = threading.Timer(self._window, self._flush)
                self._timer.daemon = True
                self._timer.start()

    def _flush(self):
        """定时器到期, 刷新缓冲区。"""
        with self._lock:
            pending = list(self._buffer)
            self._buffer.clear()
            self._timer = None

        if not pending or not self._flush_callback:
            return

        # 按通道分组
        by_channel: dict[str, list] = {}
        for item in pending:
            key = item["channel"].channel_type
            by_channel.setdefault(key, []).append(item)

        for _ch_type, items in by_channel.items():
            channel = items[0]["channel"]
            if len(items) == 1:
                # 只有一条, 原样发送
                self._flush_callback(channel, items[0]["message"], items[0]["event_type"])
            else:
                # 多条合并
                combined = self._merge_messages(items)
                self._flush_callback(channel, combined, "aggregated")

    def _merge_messages(self, items: list[dict]) -> str:
        """将多条事件合并为一条摘要消息。"""
        lines = [f"📊 工作站通知 ({len(items)} 条)"]
        lines.append("─" * 20)
        for item in items:
            icon = EVENT_ICONS.get(item["event_type"], "📢")
            # 取消息第一行作为摘要
            first_line = item["message"].split("\n")[0]
            # 去掉原始 icon (避免重复)
            for existing_icon in EVENT_ICONS.values():
                if first_line.startswith(existing_icon):
                    first_line = first_line[len(existing_icon):].strip()
                    break
            lines.append(f"{icon} {first_line}")
        lines.append("─" * 20)
        lines.append(f"⏰ {datetime.now().strftime('%H:%M:%S')}")
        return "\n".join(lines)

    def cancel(self):
        """取消待刷新的定时器。"""
        with self._lock:
            if self._timer:
                self._timer.cancel()
                self._timer = None


# ── 优化4: 免打扰时段检查 ──────────────────────────────────────


class QuietHoursChecker:
    """免打扰时段检查器。"""

    def __init__(self, enabled: bool = False, start: str = "23:00",
                 end: str = "08:00", override_priority: str = "high"):
        self.enabled = enabled
        self._start = self._parse_time(start)
        self._end = self._parse_time(end)
        self._override_priority = override_priority

    @staticmethod
    def _parse_time(t: str) -> tuple[int, int]:
        try:
            parts = t.split(":")
            return int(parts[0]), int(parts[1])
        except (ValueError, IndexError):
            return 23, 0

    def is_quiet(self) -> bool:
        """当前是否处于免打扰时段。"""
        if not self.enabled:
            return False
        now = datetime.now()
        current = (now.hour, now.minute)
        if self._start <= self._end:
            # 同日区间 (如 08:00 ~ 12:00)
            return self._start <= current < self._end
        else:
            # 跨午夜区间 (如 23:00 ~ 08:00)
            return current >= self._start or current < self._end

    def should_block(self, event_priority: str) -> bool:
        """判断是否应阻止该优先级的消息。"""
        if not self.is_quiet():
            return False
        # 高于 override_priority 的消息可穿透
        return _PRIORITY_ORDER.get(event_priority, 1) < _PRIORITY_ORDER.get(self._override_priority, 2)


# ── 主类 ──────────────────────────────────────────────────────


class BotGateway:
    """Bot 网关 — 手机消息通道管理器。

    支持两种通道:
    1. 企业微信群机器人 Webhook — 单向推送，最简单
    2. Telegram Bot API — 双向，支持推送 + 命令交互

    优化:
    - 消息聚合防刷屏 (aggregate_window)
    - Inline Keyboard 交互按钮 (PM 决策一键操作)
    - typing 状态 + 发送重试/离线队列
    - 免打扰时段 (Quiet Hours)

    用法:
        gw = BotGateway()
        gw.add_channel(BotChannel(channel_type="wechat_webhook", webhook_url="...", enabled=True))
        gw.notify("task_completed", {"name": "xxx", "task_id": "abc"})
    """

    def __init__(self, aggregate_window: int = 30, max_retry: int = 3,
                 retry_backoff: float = 2.0, quiet_hours: dict = None):
        self._channels: list[BotChannel] = []
        self._lock = threading.Lock()
        self._command_handler: Optional[Callable] = None
        self._tg_poll_thread: Optional[threading.Thread] = None
        self._tg_last_update_id = 0
        self._tg_polling = False
        # ── 优化15: 统一消息入口 ──
        self._chat_handler = None  # ChatHandler 实例, 用于统一处理自然语言消息

        # ── 优化1: 消息聚合 ──
        self._aggregator = MessageAggregator(
            window_secs=aggregate_window,
            flush_callback=self._do_send_to_channel,
        )

        # ── 优化3: 重试 + 离线队列 ──
        self._max_retry = max_retry
        self._retry_backoff = retry_backoff
        self._pending_queue: deque = deque(maxlen=100)  # 发送失败的离线消息
        self._send_pool_lock = threading.Lock()

        # ── 优化4: 免打扰 ──
        qh = quiet_hours or {}
        self._quiet_checker = QuietHoursChecker(
            enabled=qh.get("enabled", False),
            start=qh.get("start", "23:00"),
            end=qh.get("end", "08:00"),
            override_priority=qh.get("override_priority", "high"),
        )

    # ── 通道管理 ──

    def add_channel(self, channel: BotChannel):
        """添加或更新一个 Bot 通道。"""
        with self._lock:
            # 替换同类型的已有通道
            self._channels = [c for c in self._channels if c.channel_type != channel.channel_type]
            self._channels.append(channel)
        # 如果是 Telegram 且启用，启动轮询
        if channel.channel_type == "telegram" and channel.enabled:
            self._start_telegram_polling(channel)

    def remove_channel(self, channel_type: str):
        """移除一个通道。"""
        with self._lock:
            self._channels = [c for c in self._channels if c.channel_type != channel_type]

    def list_channels(self) -> list[dict]:
        """列出所有通道配置（脱敏）。"""
        with self._lock:
            return [
                {
                    "channel_type": c.channel_type,
                    "enabled": c.enabled,
                    "min_priority": c.min_priority,
                    "webhook_url": _mask(c.webhook_url) if c.webhook_url else "",
                    "bot_token": _mask(c.bot_token) if c.bot_token else "",
                    "chat_id": c.chat_id,
                    "webhook_url_base": c.webhook_url_base,
                }
                for c in self._channels
            ]

    def get_channel(self, channel_type: str) -> Optional[BotChannel]:
        """获取指定类型的通道。"""
        with self._lock:
            for c in self._channels:
                if c.channel_type == channel_type:
                    return c
        return None

    def set_command_handler(self, handler: Callable):
        """设置命令处理回调。

        handler 签名: handler(command: str, args: str, chat_id: str) -> str
        返回值作为回复消息发送给用户。
        """
        self._command_handler = handler

    def set_chat_handler(self, chat_handler):
        """优化15: 设置 ChatHandler 实例, 统一处理自然语言消息。

        设置后, Bot 收到的非斜杠命令消息将转发给 ChatHandler 处理,
        与 Web 聊天窗口使用相同的 LLM + 操作意图检测逻辑。
        """
        self._chat_handler = chat_handler

    # ── 推送通知 ──

    def notify(self, event_type: str, data: dict = None):
        """推送事件通知到所有启用的通道。

        Args:
            event_type: 事件类型（见 EVENT_TEMPLATES）
            data: 模板变量字典
        """
        data = data or {}
        template = EVENT_TEMPLATES.get(event_type)
        if not template:
            message = f"📢 {event_type}\n{json.dumps(data, ensure_ascii=False, indent=2)}"
        else:
            try:
                message = template.format(**data)
            except (KeyError, IndexError):
                message = f"📢 {event_type}\n{json.dumps(data, ensure_ascii=False)}"

        priority = EVENT_PRIORITY.get(event_type, "normal")

        # ── 优化4: 免打扰检查 ──
        if self._quiet_checker.should_block(priority):
            logger.debug("免打扰时段, 跳过 %s 推送 (priority=%s)", event_type, priority)
            return

        with self._lock:
            channels = list(self._channels)

        for ch in channels:
            if not ch.enabled:
                continue
            if not _should_send(priority, ch.min_priority):
                continue

            # ── 优化2: PM 决策请求附带 Inline Keyboard ──
            if event_type == "pm_awaiting_input" and ch.channel_type == "telegram":
                options = data.get("options", [])
                pm_id = data.get("pm_id", "")
                self._send_telegram_with_keyboard(ch, message, pm_id, options)
                continue

            # ── 优化1: 通过聚合器发送 ──
            self._aggregator.push(ch, message, event_type, priority)

    def _do_send_to_channel(self, channel: BotChannel, message: str, event_type: str):
        """实际发送 (聚合器回调 + 直接发送共用)。含重试逻辑。"""
        # ── 优化3: 重试 + 离线队列 ──
        for attempt in range(self._max_retry):
            try:
                if channel.channel_type == "wechat_webhook":
                    self._send_wechat(channel, message)
                elif channel.channel_type == "telegram":
                    self._send_telegram(channel, message)
                return  # 成功
            except Exception as e:
                if attempt < self._max_retry - 1:
                    wait = self._retry_backoff ** attempt
                    logger.warning("发送到 %s 失败 (第%d次), %.1fs后重试: %s",
                                   channel.channel_type, attempt + 1, wait, e)
                    time.sleep(wait)
                else:
                    logger.error("发送到 %s 最终失败, 消息进入离线队列: %s", channel.channel_type, e)
                    self._pending_queue.append({
                        "channel_type": channel.channel_type,
                        "message": message,
                        "event_type": event_type,
                        "failed_at": time.time(),
                    })

    def flush_pending_queue(self):
        """手动触发离线队列补发 (可在通道恢复后调用)。"""
        flushed = 0
        while self._pending_queue:
            item = self._pending_queue[0]
            ch = self.get_channel(item["channel_type"])
            if not ch or not ch.enabled:
                break
            try:
                if ch.channel_type == "wechat_webhook":
                    self._send_wechat(ch, item["message"])
                elif ch.channel_type == "telegram":
                    self._send_telegram(ch, item["message"])
                self._pending_queue.popleft()
                flushed += 1
            except Exception:
                break
        if flushed:
            logger.info("离线队列补发 %d 条消息", flushed)
        return flushed

    # ── 企业微信群机器人 ──

    def _send_wechat(self, channel: BotChannel, message: str):
        """企业微信群机器人 Webhook 推送。"""
        if not channel.webhook_url:
            return
        payload = {
            "msgtype": "text",
            "text": {"content": message},
        }
        resp = requests.post(channel.webhook_url, json=payload, timeout=10)
        if resp.status_code != 200:
            raise RuntimeError(f"微信 webhook 返回 {resp.status_code}: {resp.text[:200]}")
        result = resp.json()
        if result.get("errcode", 0) != 0:
            raise RuntimeError(f"微信 webhook 错误: {result}")

    # ── Telegram Bot ──

    def _send_telegram(self, channel: BotChannel, message: str):
        """Telegram Bot API 推送消息。"""
        if not channel.bot_token or not channel.chat_id:
            return
        url = f"https://api.telegram.org/bot{channel.bot_token}/sendMessage"
        # 长消息分片 (Telegram 限制 4096 字符)
        chunks = _split_message(message, max_len=4000)
        for chunk in chunks:
            payload = {
                "chat_id": channel.chat_id,
                "text": chunk,
                "parse_mode": "HTML",
            }
            resp = requests.post(url, json=payload, timeout=10)
            if resp.status_code != 200:
                raise RuntimeError(f"Telegram 返回 {resp.status_code}: {resp.text[:200]}")

    def _send_telegram_with_keyboard(self, channel: BotChannel, message: str,
                                      pm_id: str, options: list):
        """优化2: 发送带 Inline Keyboard 的 Telegram 消息 (PM 决策按钮)。"""
        if not channel.bot_token or not channel.chat_id:
            return
        url = f"https://api.telegram.org/bot{channel.bot_token}/sendMessage"

        # 构建 inline keyboard
        keyboard_rows = []
        if options:
            for opt in options[:6]:  # 最多 6 个选项
                keyboard_rows.append([{
                    "text": opt,
                    "callback_data": f"decide:{pm_id}:{opt}"
                }])
        # 添加通用操作行
        keyboard_rows.append([
            {"text": "✅ 同意继续", "callback_data": f"decide:{pm_id}:approve"},
            {"text": "❌ 放弃任务", "callback_data": f"decide:{pm_id}:abort"},
        ])

        payload = {
            "chat_id": channel.chat_id,
            "text": message,
            "parse_mode": "HTML",
            "reply_markup": json.dumps({"inline_keyboard": keyboard_rows}),
        }
        try:
            resp = requests.post(url, json=payload, timeout=10)
            if resp.status_code != 200:
                logger.warning("Telegram keyboard 消息发送失败: %d", resp.status_code)
        except Exception as e:
            logger.error("Telegram keyboard 发送异常: %s", e)

    def _send_telegram_typing(self, channel: BotChannel, chat_id: str):
        """优化3: 发送 typing 状态指示 (让用户知道 Bot 正在处理)。"""
        if not channel.bot_token:
            return
        url = f"https://api.telegram.org/bot{channel.bot_token}/sendChatAction"
        try:
            requests.post(url, json={"chat_id": chat_id, "action": "typing"}, timeout=5)
        except Exception:
            pass  # typing 失败不影响主流程

    # ── Telegram 轮询 ──

    def _start_telegram_polling(self, channel: BotChannel):
        """启动 Telegram Bot 长轮询（接收用户命令）。"""
        if self._tg_polling:
            return
        self._tg_polling = True
        self._tg_poll_thread = threading.Thread(
            target=self._telegram_poll_loop,
            args=(channel,),
            daemon=True,
            name="bot-telegram-poll",
        )
        self._tg_poll_thread.start()
        logger.info("Telegram 轮询已启动，等待手机端命令...")

    def _telegram_poll_loop(self, channel: BotChannel):
        """Telegram getUpdates 长轮询循环。"""
        while self._tg_polling and channel.enabled:
            try:
                url = f"https://api.telegram.org/bot{channel.bot_token}/getUpdates"
                params = {
                    "offset": self._tg_last_update_id + 1,
                    "timeout": 30,  # 长轮询 30s
                }
                resp = requests.get(url, params=params, timeout=35)
                if resp.status_code != 200:
                    time.sleep(5)
                    continue

                result = resp.json()
                if not result.get("ok"):
                    time.sleep(5)
                    continue

                for update in result.get("result", []):
                    self._tg_last_update_id = update.get("update_id", self._tg_last_update_id)

                    # ── 优化2: 处理 Inline Keyboard 回调 ──
                    callback_query = update.get("callback_query")
                    if callback_query:
                        self._handle_callback_query(channel, callback_query)
                        continue

                    message = update.get("message")
                    if not message:
                        continue
                    text = message.get("text", "").strip()
                    chat_id = str(message.get("chat", {}).get("id", ""))
                    if text:
                        self._handle_telegram_command(channel, text, chat_id)

            except requests.exceptions.Timeout:
                continue  # 长轮询超时是正常的
            except Exception as e:
                logger.error("Telegram 轮询异常: %s", e)
                time.sleep(5)

    def _handle_callback_query(self, channel: BotChannel, callback_query: dict):
        """优化2: 处理 Inline Keyboard 按钮点击。

        callback_data 格式: "decide:{pm_id}:{choice}"
        """
        data = callback_query.get("data", "")
        chat_id = str(callback_query.get("message", {}).get("chat", {}).get("id", ""))
        query_id = callback_query.get("id", "")

        # 应答 callback (消除按钮 loading 状态)
        self._answer_callback_query(channel, query_id)

        if not data.startswith("decide:"):
            return

        parts = data.split(":", 2)
        if len(parts) < 3:
            return
        _, pm_id, choice = parts

        logger.info("收到 Inline 决策: PM=%s, choice=%s (from %s)", pm_id[:12], choice, chat_id)

        # 将决策注入 PM Agent
        if self._chat_handler and hasattr(self._chat_handler, 'controller'):
            try:
                controller = self._chat_handler.controller
                result = controller.inject_input_to_pm(pm_id, {
                    "response": choice,
                    "choice": choice,
                    "source": "telegram_inline",
                })
                if result.get("ok"):
                    ack = f"✅ 已将决策「{choice}」发送给 PM Agent"
                else:
                    ack = f"❌ 发送失败: {result.get('message', '未知错误')}"
            except Exception as e:
                ack = f"⚠️ 处理异常: {e}"
        elif self._command_handler:
            try:
                ack = self._command_handler("decide", f"{pm_id} {choice}", chat_id)
            except Exception as e:
                ack = f"⚠️ 命令执行出错: {e}"
        else:
            ack = "⚠️ 无法处理决策: 秘书未激活"

        # 回复确认
        try:
            url = f"https://api.telegram.org/bot{channel.bot_token}/sendMessage"
            requests.post(url, json={"chat_id": chat_id, "text": ack}, timeout=10)
        except Exception as e:
            logger.error("回复 Inline 决策确认失败: %s", e)

    def _answer_callback_query(self, channel: BotChannel, callback_query_id: str):
        """应答 Telegram callback_query (消除客户端 loading)。"""
        if not callback_query_id or not channel.bot_token:
            return
        url = f"https://api.telegram.org/bot{channel.bot_token}/answerCallbackQuery"
        try:
            requests.post(url, json={"callback_query_id": callback_query_id}, timeout=5)
        except Exception:
            pass

    def _handle_telegram_command(self, channel: BotChannel, text: str, chat_id: str):
        """处理来自 Telegram 的命令 (优化15: 统一入口)。"""
        logger.info("收到 Telegram 消息: %s (from %s)", text[:50], chat_id)

        # 内置命令
        if text == "/start" or text == "/help":
            reply = (
                "🤖 LAN Mesh 工作站 Bot\n\n"
                "可用命令:\n"
                "/status — 工作站状态概览\n"
                "/hosts — 在线主机列表\n"
                "/tasks — 最近任务\n"
                "/help — 显示帮助\n\n"
                "或直接发送自然语言消息, 秘书将为您处理。\n"
                "示例: 「提交一个代码审查任务: 检查 xxx 项目」"
            )
        elif text == "/ping":
            reply = "pong 🏓"
        elif text.startswith("/"):
            # 斜杠命令: 委托给外部命令处理器
            if self._command_handler:
                try:
                    parts = text.split(maxsplit=1)
                    cmd = parts[0].lstrip("/")
                    args = parts[1] if len(parts) > 1 else ""
                    reply = self._command_handler(cmd, args, chat_id)
                except Exception as e:
                    reply = f"⚠️ 命令执行出错: {e}"
            else:
                reply = f"未知命令: {text}\n发送 /help 查看可用命令"
        else:
            # 优化15: 自然语言消息 → 统一转发给 ChatHandler
            # ── 优化3: 先发送 typing 状态 ──
            self._send_telegram_typing(channel, chat_id)
            reply = self._handle_natural_language(text, chat_id)

        # 发送回复
        url = f"https://api.telegram.org/bot{channel.bot_token}/sendMessage"
        try:
            # 长消息分片
            chunks = _split_message(reply, max_len=4000)
            for chunk in chunks:
                requests.post(url, json={
                    "chat_id": chat_id,
                    "text": chunk,
                    "parse_mode": "HTML",
                }, timeout=10)
        except Exception as e:
            logger.error("回复 Telegram 失败: %s", e)

    def _handle_natural_language(self, text: str, chat_id: str) -> str:
        """优化15: 统一处理自然语言消息。

        优先使用 ChatHandler (与 Web 端相同的 LLM + 操作意图),
        回退到 command_handler, 最后回退到提示信息。
        """
        # 策略1: ChatHandler (完整的秘书对话能力)
        if self._chat_handler:
            try:
                result = self._chat_handler.chat(text)
                reply = result.get("reply", "")
                if reply:
                    return reply
            except Exception as e:
                logger.error("ChatHandler 处理异常: %s", e)
                return f"⚠️ 秘书处理异常: {e}"

        # 策略2: command_handler (简单命令模式)
        if self._command_handler:
            try:
                return self._command_handler("chat", text, chat_id)
            except Exception as e:
                return f"⚠️ 命令执行出错: {e}"

        # 策略3: 无处理器
        return (
            f"收到: {text}\n"
            "秘书尚未激活, 无法处理自然语言消息。\n"
            "请先在 Web UI 激活 Secretary, 或发送 /help 查看可用命令。"
        )

    def stop(self):
        """停止所有后台线程。"""
        self._tg_polling = False
        self._aggregator.cancel()

    # ── 测试 ──

    def test_channel(self, channel_type: str) -> dict:
        """发送测试消息到指定通道。"""
        ch = self.get_channel(channel_type)
        if not ch:
            return {"ok": False, "error": f"通道 {channel_type} 不存在"}
        if not ch.enabled:
            return {"ok": False, "error": f"通道 {channel_type} 未启用"}
        try:
            self._do_send_to_channel(ch, EVENT_TEMPLATES["bot_test"], "bot_test")
            return {"ok": True, "message": "测试消息已发送，请检查手机端"}
        except Exception as e:
            return {"ok": False, "error": str(e)}


# ── 工具函数 ──────────────────────────────────────────────────

_PRIORITY_ORDER = {"low": 0, "normal": 1, "high": 2}


def _should_send(event_priority: str, min_priority: str) -> bool:
    """判断事件优先级是否达到通道的最低推送门槛。"""
    return _PRIORITY_ORDER.get(event_priority, 1) >= _PRIORITY_ORDER.get(min_priority, 1)


def _mask(s: str) -> str:
    """脱敏处理 — 只显示前6位和后4位。"""
    if len(s) <= 12:
        return s[:3] + "***"
    return s[:6] + "..." + s[-4:]


# HTML 标签正则 + Telegram 支持的 void 标签 (M4: 分片标签安全)
_HTML_TAG_RE = re.compile(r"</?([a-zA-Z][a-zA-Z0-9]*)(\s[^>]*)?>|<!--.*?-->")
_VOID_TAGS = {"br", "img", "hr", "meta", "link", "input"}


def _split_message(text: str, max_len: int = 4000) -> list[str]:
    """将长消息按段落分片, 避免超过 Telegram 4096 字符限制。

    HTML 感知 (M4): 分片点不落在标签内部; 片段末尾未闭合的标签自动补闭合,
    并在下一片段开头恢复, 保证每段都是合法 HTML (parse_mode=HTML 不报错)。
    """
    if len(text) <= max_len:
        return [text]

    chunks = []
    remaining = text
    while remaining:
        if len(remaining) <= max_len:
            chunks.append(remaining)
            break
        # 候选切割点: 优先换行
        cut_pos = remaining.rfind("\n", 0, max_len)
        if cut_pos < max_len // 2:
            cut_pos = max_len
        # 若切割点落在标签内部 (< 后紧跟字母或 / 且无闭合 >), 回退到标签前
        lt = remaining.rfind("<", 0, cut_pos)
        gt = remaining.rfind(">", 0, cut_pos)
        if lt > gt and lt < cut_pos:
            after = remaining[lt + 1] if lt + 1 < len(remaining) else ""
            if after.isalpha() or after == "/":
                prev_nl = remaining.rfind("\n", 0, lt)
                cut_pos = prev_nl if prev_nl > 0 else lt
        chunk = remaining[:cut_pos]
        remaining = remaining[cut_pos:].lstrip("\n")
        # 平衡未闭合标签
        chunk, remaining = _balance_html_tags(chunk, remaining)
        chunks.append(chunk + ("\n…" if remaining else ""))
    return chunks


def _balance_html_tags(chunk: str, rest: str) -> tuple[str, str]:
    """chunk 末尾未闭合标签 → 补闭合标签; rest 开头恢复对应开标签。

    Returns:
        (补全后的 chunk, 恢复后的 rest)
    """
    stack: list[str] = []
    for m in _HTML_TAG_RE.finditer(chunk):
        raw = m.group(0)
        if raw.startswith("<!--") or m.group(1) is None:
            continue  # 注释或非标签
        if raw.startswith("</"):
            if stack and stack[-1] == m.group(1):
                stack.pop()
        elif not raw.endswith("/>") and m.group(1) not in _VOID_TAGS:
            stack.append(m.group(1))
    if not stack:
        return chunk, rest
    closers = "".join(f"</{t}>" for t in reversed(stack))
    openers = "".join(f"<{t}>" for t in stack)
    return chunk + closers, openers + rest
