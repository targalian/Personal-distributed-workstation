"""ChatIntentService: extracted domain behavior with explicit component ownership."""

import re
import json
from typing import Optional
from .chat_policy import (_ACTION_KEYWORDS, _READONLY_ACTIONS, _looks_like_non_directive, _ACTION_DESCRIPTIONS, _CLASSIFIER_VERB_SIGNALS, _CLASSIFIER_NOUN_SIGNALS, _CLASSIFIER_MAX_MESSAGE_LEN, _CLASSIFIER_SYSTEM_PROMPT)

from .component_service import ComponentService

class ChatIntentService(ComponentService):
    """Single domain implementation behind the existing facade."""

    OPERATIONS = ('_detect_action', '_detect_action_with_context', '_last_suggested_action', '_build_classifier_prompt', '_classify_action_llm')

    def _detect_action(self, message: str) -> str:
        """检测用户消息中的操作意图。

        Returns:
            操作类型字符串, 无意图则返回空字符串
        """
        msg_lower = message.lower()
        matched = ""
        for keyword, action in _ACTION_KEYWORDS.items():
            if keyword in message or keyword in msg_lower:
                matched = action
                break
        if not matched:
            return ""
        # BUG-035/036: 只读查询无副作用, 直接放行; 变更类动作需过语气护栏,
        # 避免「谈论/否定/复述某动作」被当成「执行该动作」。
        if matched in _READONLY_ACTIONS:
            return matched
        if _looks_like_non_directive(message):
            return ""
        return matched


    def _detect_action_with_context(self, message: str,
                                    history: Optional[list] = None) -> str:
        """BUG-031: 关键词未命中时, 尝试从上一轮秘书建议继承意图。

        场景: Boss 多轮澄清需求后只回「是/可以/够了」, 字面无关键词,
        旧逻辑直接判定无意图 → 后台静默不执行。此处仅在同时满足
        「本轮是纯确认」且「上一轮秘书明确给出过带触发词的指令样例」
        时才继承, 避免把秘书解释性提到的词误当指令。
        """
        direct = self.owner._detect_action(message)
        if direct:
            return direct
        if not self.owner._is_confirmation(message):
            return ""
        suggested = self.owner._last_suggested_action(history)
        if suggested:
            print(f"[Chat] 确认类回复继承上轮建议意图: {suggested}", flush=True)
        return suggested


    @staticmethod
    def _is_confirmation(message: str) -> bool:
        """判断是否为纯确认 (短句且以肯定词起头/构成)。"""
        text = (message or "").strip().strip("。.!！,，~ ")
        if not text or len(text) > 30:
            return False
        words = ("是", "对", "好", "可以", "行", "嗯", "OK", "ok", "Ok",
                 "确认", "没问题", "足够", "够了", "同意", "继续", "开始",
                 "麻烦你", "就这样", "如上", "以上")
        return any(w in text for w in words)


    def _last_suggested_action(self, history: Optional[list]) -> str:
        """取上一轮秘书回复中「指令样例」所指向的动作。

        只认样例块内的触发词 (「」引号或 > 引用内), 不认散文里的提及,
        以免秘书解释能力范围时被误判为指令。
        """
        msgs = history if history is not None else self.owner._conversations.get(
            self.owner._active_conv_id, [])
        for msg in reversed(msgs[-4:]):
            if msg.get("role") != "assistant":
                continue
            content = msg.get("content", "") or ""
            for seg in self.owner._extract_quoted(content):
                for keyword, action in _ACTION_KEYWORDS.items():
                    if keyword in seg:
                        return action
            return ""
        return ""


    @staticmethod
    def _extract_quoted(content: str) -> list:
        """抽出回复中的指令样例片段 (「」/『』包裹或 > 引用行)。"""
        segs = []
        for left, right in (("「", "」"), ("『", "』")):
            start = 0
            while True:
                i = content.find(left, start)
                if i < 0:
                    break
                j = content.find(right, i + 1)
                if j < 0:
                    break
                segs.append(content[i + 1:j])
                start = j + 1
        for line in content.split("\n"):
            stripped = line.strip()
            if stripped.startswith(">"):
                segs.append(stripped.lstrip("> ").strip())
        return segs


    @staticmethod
    def _looks_like_command(message: str) -> bool:
        """廉价预筛: 只让疑似指令的消息进入 LLM 意图分类。

        中文字词信号按子串匹配; ASCII 信号按整词匹配, 避免
        「example」里的 pm 之类误触发。未过闸门的消息不产生任何
        额外模型调用, 这是延迟与成本的主要护栏。
        """
        text = (message or "").strip()
        if not text or len(text) > _CLASSIFIER_MAX_MESSAGE_LEN:
            return False
        # BUG-036: 非指令语气直接拒闸。否则关键词路径被护栏拦下后, 消息会
        # 继续走 LLM 分类兜底, 由模型把「先不要创建项目」重新判成
        # create_project —— 绕过整条防线。在闸门处拦截同时省掉一次 LLM 调用。
        if _looks_like_non_directive(text):
            return False
        lowered = text.lower()
        for signal in _CLASSIFIER_VERB_SIGNALS + _CLASSIFIER_NOUN_SIGNALS:
            if signal.isascii():
                if re.search(rf"\b{re.escape(signal)}\b", lowered):
                    return True
            elif signal in text:
                return True
        return False


    def _build_classifier_prompt(self, message: str,
                                  history: Optional[list]) -> str:
        """构建意图分类 prompt: 动作白名单 + 少量近况上下文。"""
        lines = ["候选动作:"]
        for name, desc in _ACTION_DESCRIPTIONS.items():
            lines.append(f"- {name}: {desc}")
        lines.append("- none: 非以上任何操作 (提问/闲聊/讨论)")
        lines.append("")
        lines.append("近期对话 (仅用于消解指代, 最后一行是本轮消息):")
        msgs = history if history is not None else []
        for msg in msgs[-4:]:
            role = "Boss" if msg.get("role") == "user" else "秘书"
            content = (msg.get("content", "") or "")[:80]
            lines.append(f"{role}: {content}")
        lines.append(f"Boss: {message}")
        lines.append("")
        lines.append('只输出 JSON, 例如 {"action": "create_project", '
                     '"reason": "要求建项目"}')
        return "\n".join(lines)


    @staticmethod
    def _parse_classifier_json(content: str) -> str:
        """从分类回复中提取白名单动作; 解析失败或越权一律返回空。"""
        text = (content or "").strip()
        start = text.find("{")
        end = text.rfind("}") + 1
        if start < 0 or end <= start:
            return ""
        try:
            data = json.loads(text[start:end])
        except Exception:
            return ""
        if not isinstance(data, dict):
            return ""
        action = data.get("action", "")
        if isinstance(action, str) and action in _ACTION_DESCRIPTIONS:
            return action
        return ""


    def _classify_action_llm(self, message: str,
                             history: Optional[list] = None) -> str:
        """LLM 意图分类兜底 (iter-79)。

        关键词与上下文继承都未命中时, 对「帮我建个项目」这类口语化
        指令做一次意图分类。结果必须落在动作白名单内才生效; 分类
        失败或输出越权动作一律视为无意图, 维持 BUG-031 的「宁可
        明说不执行, 不可虚报已执行」底线。
        """
        if not self.owner._looks_like_command(message):
            return ""
        prompt = self.owner._build_classifier_prompt(message, history)
        model_pref, fallback_models = self.owner._resolve_chat_model_pref(message)
        try:
            resp = self.owner.runtime._call_llm_with_routing(prompt, {
                "_model_preference": model_pref,
                "_fallback_models": fallback_models,
                "_system_prompt": _CLASSIFIER_SYSTEM_PROMPT,
            })
        except Exception as exc:
            print(f"[Chat] LLM 意图分类调用失败, 回退无动作: {exc}",
                  flush=True)
            return ""
        action = self.owner._parse_classifier_json(resp.get("content", ""))
        if action:
            print(f"[Chat] LLM 意图分类命中: {message[:20]} -> {action}",
                  flush=True)
        return action

