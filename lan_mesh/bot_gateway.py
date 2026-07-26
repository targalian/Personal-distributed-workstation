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

事件类型:
  - task_submitted / task_completed / task_failed
  - host_online / host_offline
  - secretary_activated / secretary_deactivated
  - skill_assigned / skill_revoked
  - budget_warning
"""
import json
import threading
import time
from dataclasses import dataclass, field
from typing import Optional, Callable

import requests


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


class BotGateway:
    """Bot 网关 — 手机消息通道管理器。

    支持两种通道:
    1. 企业微信群机器人 Webhook — 单向推送，最简单
    2. Telegram Bot API — 双向，支持推送 + 命令交互

    用法:
        gw = BotGateway()
        gw.add_channel(BotChannel(channel_type="wechat_webhook", webhook_url="...", enabled=True))
        gw.notify("task_completed", {"name": "xxx", "task_id": "abc"})
    """

    def __init__(self):
        self._channels: list[BotChannel] = []
        self._lock = threading.Lock()
        self._command_handler: Optional[Callable] = None
        self._tg_poll_thread: Optional[threading.Thread] = None
        self._tg_last_update_id = 0
        self._tg_polling = False
        # ── 优化15: 统一消息入口 ──
        self._chat_handler = None  # ChatHandler 实例, 用于统一处理自然语言消息

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
            # 未知事件类型，直接用 JSON
            message = f"📢 {event_type}\n{json.dumps(data, ensure_ascii=False, indent=2)}"
        else:
            try:
                message = template.format(**data)
            except (KeyError, IndexError):
                message = f"📢 {event_type}\n{json.dumps(data, ensure_ascii=False)}"

        priority = EVENT_PRIORITY.get(event_type, "normal")

        with self._lock:
            channels = list(self._channels)

        for ch in channels:
            if not ch.enabled:
                continue
            if not _should_send(priority, ch.min_priority):
                continue
            # 异步发送，避免阻塞调用方
            threading.Thread(
                target=self._send_to_channel,
                args=(ch, message, event_type),
                daemon=True,
            ).start()

    def _send_to_channel(self, channel: BotChannel, message: str, event_type: str):
        """发送消息到单个通道。"""
        try:
            if channel.channel_type == "wechat_webhook":
                self._send_wechat(channel, message)
            elif channel.channel_type == "telegram":
                self._send_telegram(channel, message)
        except Exception as e:
            print(f"[BotGateway] 发送到 {channel.channel_type} 失败: {e}")

    # ── 企业微信群机器人 ──

    def _send_wechat(self, channel: BotChannel, message: str):
        """企业微信群机器人 Webhook 推送。

        文档: https://developer.work.weixin.qq.com/document/path/91770
        """
        if not channel.webhook_url:
            return
        payload = {
            "msgtype": "text",
            "text": {"content": message},
        }
        resp = requests.post(channel.webhook_url, json=payload, timeout=10)
        if resp.status_code != 200:
            print(f"[BotGateway] 微信 webhook 返回 {resp.status_code}: {resp.text}")
        elif resp.json().get("errcode", 0) != 0:
            print(f"[BotGateway] 微信 webhook 错误: {resp.json()}")

    # ── Telegram Bot ──

    def _send_telegram(self, channel: BotChannel, message: str):
        """Telegram Bot API 推送消息。"""
        if not channel.bot_token or not channel.chat_id:
            return
        url = f"https://api.telegram.org/bot{channel.bot_token}/sendMessage"
        payload = {
            "chat_id": channel.chat_id,
            "text": message,
            "parse_mode": "HTML",
        }
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code != 200:
            print(f"[BotGateway] Telegram 返回 {resp.status_code}: {resp.text}")

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
        print("[BotGateway] Telegram 轮询已启动，等待手机端命令...")

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
                    message = update.get("message") or update.get("callback_query", {}).get("message")
                    if not message:
                        continue
                    text = message.get("text", "").strip()
                    chat_id = str(message.get("chat", {}).get("id", ""))
                    if text:
                        self._handle_telegram_command(channel, text, chat_id)

            except requests.exceptions.Timeout:
                continue  # 长轮询超时是正常的
            except Exception as e:
                print(f"[BotGateway] Telegram 轮询异常: {e}")
                time.sleep(5)

    def _handle_telegram_command(self, channel: BotChannel, text: str, chat_id: str):
        """处理来自 Telegram 的命令 (优化15: 统一入口)。"""
        print(f"[BotGateway] 收到 Telegram 消息: {text} (from {chat_id})")

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
            reply = self._handle_natural_language(text, chat_id)

        # 发送回复
        url = f"https://api.telegram.org/bot{channel.bot_token}/sendMessage"
        try:
            requests.post(url, json={
                "chat_id": chat_id,
                "text": reply,
                "parse_mode": "HTML",
            }, timeout=10)
        except Exception as e:
            print(f"[BotGateway] 回复 Telegram 失败: {e}")

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
                    # Telegram 消息长度限制 4096 字符
                    if len(reply) > 4000:
                        reply = reply[:4000] + "\n...(内容过长已截断)"
                    return reply
            except Exception as e:
                print(f"[BotGateway] ChatHandler 处理异常: {e}")
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

    # ── 测试 ──

    def test_channel(self, channel_type: str) -> dict:
        """发送测试消息到指定通道。"""
        ch = self.get_channel(channel_type)
        if not ch:
            return {"ok": False, "error": f"通道 {channel_type} 不存在"}
        if not ch.enabled:
            return {"ok": False, "error": f"通道 {channel_type} 未启用"}
        try:
            self._send_to_channel(ch, EVENT_TEMPLATES["bot_test"], "bot_test")
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
