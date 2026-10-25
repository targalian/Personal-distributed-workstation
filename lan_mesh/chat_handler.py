"""Compatibility facade and composition root for independently routed domains."""

import re
import time
import json
from threading import RLock
from functools import wraps
from contextlib import nullcontext
from typing import Optional, Any
from .logger import get_logger
from .role_cards import render_secretary_prompt
from .chat_policy import (logger, _ACTION_KEYWORDS, _QUESTION_MARKERS, _NEGATION_MARKERS, _DELIBERATION_MARKERS, _RETROSPECT_MARKERS, _IMPERATIVE_MARKERS, _READONLY_ACTIONS, _looks_like_question, _looks_like_non_directive, _ACTION_DESCRIPTIONS, _CLASSIFIER_VERB_SIGNALS, _CLASSIFIER_NOUN_SIGNALS, _CLASSIFIER_MAX_MESSAGE_LEN, _CLASSIFIER_SYSTEM_PROMPT, _REQ_PHASE_INTAKE, _REQ_PHASE_SYNTHESIZE, _REQ_PHASE_GAP_FILL, _REQ_PHASE_CONFIRM, _REQ_PHASE_DISPATCHED, _REQ_INTAKE_KEYWORDS, _REQ_FAST_EXIT_KEYWORDS, _REQ_CANCEL_KEYWORDS, _REQ_FINAL_PROMPT_PREFIX, REQ_CHECKLIST_TEMPLATES, _REQ_DEFAULT_CHECKLIST, _find_node_by_name, _PATH_BODY, _PATH_LABELED, _PATH_BARE, _extract_local_path)

from .component_service import call_component
from .chat_requirements_service import ChatRequirementsService
from .chat_history_service import ChatHistoryService
from .chat_context_service import ChatContextService
from .chat_intent_service import ChatIntentService
from .chat_command_service import ChatCommandService

def _serialized_chat(handler):
    """Keep the active conversation and draft stable across one complete reply."""
    @wraps(handler)
    def invoke(self, *args, **kwargs):
        with vars(self).get("_component_lock", nullcontext()):
            return handler(self, *args, **kwargs)
    return invoke


class ChatHandler:
    """秘书聊天处理器 — 支持多项目对话隔离。

    在 Secretary 激活后由 StationController 创建,
    通过 station_api 的 /api/conversations 端点对外服务。

    对话持久化到共享文件夹 (conversations/), 跨主机可访问。
    """

    def __init__(self, runtime, controller, db=None, shared_folder=None):
        """初始化聊天处理器。

        Args:
            runtime: AgentRuntime 实例, 用于 LLM 调用
            controller: StationController 实例
            db: Database 实例 (本地缓存)
            shared_folder: SharedFolderManager 实例 (权威源)
        """
        self.runtime = runtime
        self._component_lock = RLock()
        self.controller = controller
        self._db = db
        self._shared_folder = shared_folder
        self._max_history = 50

        # ── 多对话状态 ──
        self._conv_index: list[dict] = []       # [{id, title, project_id, pm_threads, created_at, updated_at}]
        self._conversations: dict[str, list] = {}  # conv_id → messages
        self._active_conv_id: str = ""

        # ── 方案C: PM 线程 (L2 层) ──
        # pm_id → [{role, content, timestamp, ...}]  线程消息独立存储
        self._pm_thread_messages: dict[str, list] = {}

        # 加载对话索引
        self._load_conv_index()

        # 向后兼容: 若无任何对话, 创建默认对话
        if not self._conv_index:
            self._create_conversation("默认对话")

        # 确保活跃对话有效
        if not self._active_conv_id and self._conv_index:
            self._active_conv_id = self._conv_index[0]["id"]

        # 向后兼容: 为旧对话补充 pm_threads 字段
        for c in self._conv_index:
            if "pm_threads" not in c:
                c["pm_threads"] = []

        # ── 优化7: 反向沟通跟踪 ──
        self._last_awaiting_pm: str = ""

        # ── 需求收集状态机 ──
        self._req_drafts: dict[str, dict] = {}
        self._load_req_drafts()

    # ── 需求收集状态机 ──────────────────────────────────────────────

    def _load_req_drafts(self) -> None:
        return call_component(self, ChatRequirementsService, "_load_req_drafts")

    def _save_req_draft(self, conv_id: str) -> None:
        return call_component(self, ChatRequirementsService, "_save_req_draft", conv_id=conv_id)

    def _get_or_init_req_draft(self, conv_id: str, task_type: str='general') -> dict:
        return call_component(self, ChatRequirementsService, "_get_or_init_req_draft", conv_id=conv_id, task_type=task_type)

    def _clear_req_draft(self, conv_id: str) -> None:
        return call_component(self, ChatRequirementsService, "_clear_req_draft", conv_id=conv_id)

    def _infer_task_type_from_message(self, message: str) -> str:
        return call_component(self, ChatRequirementsService, "_infer_task_type_from_message", message=message)

    def _detect_missing_items(self, draft: dict) -> list[dict]:
        return call_component(self, ChatRequirementsService, "_detect_missing_items", draft=draft)

    _get_item_question = staticmethod(ChatRequirementsService._get_item_question)

    def _fill_checklist_from_message(self, draft: dict, message: str) -> None:
        return call_component(self, ChatRequirementsService, "_fill_checklist_from_message", draft=draft, message=message)

    def _generate_brief(self, draft: dict) -> str:
        return call_component(self, ChatRequirementsService, "_generate_brief", draft=draft)

    def _build_final_prompt(self, draft: dict) -> str:
        return call_component(self, ChatRequirementsService, "_build_final_prompt", draft=draft)

    _is_final_prompt = staticmethod(ChatRequirementsService._is_final_prompt)

    _is_cancel = staticmethod(ChatRequirementsService._is_cancel)

    def _apply_final_prompt(self, draft: dict, conv_id: str, message: str) -> None:
        return call_component(self, ChatRequirementsService, "_apply_final_prompt", draft=draft, conv_id=conv_id, message=message)

    def _try_requirement_flow(self, message: str, conv_id: str, chat_history: list) -> str:
        return call_component(self, ChatRequirementsService, "_try_requirement_flow", message=message, conv_id=conv_id, chat_history=chat_history)

    def _handle_active_draft(self, draft: dict, conv_id: str, message: str, chat_history: list, is_fast_exit: bool) -> str:
        return call_component(self, ChatRequirementsService, "_handle_active_draft", draft=draft, conv_id=conv_id, message=message, chat_history=chat_history, is_fast_exit=is_fast_exit)

    def _handle_intake(self, draft: dict, conv_id: str, message: str) -> str:
        return call_component(self, ChatRequirementsService, "_handle_intake", draft=draft, conv_id=conv_id, message=message)

    def _handle_gap_fill(self, draft: dict, conv_id: str, message: str) -> str:
        return call_component(self, ChatRequirementsService, "_handle_gap_fill", draft=draft, conv_id=conv_id, message=message)

    def _handle_confirm(self, draft: dict, conv_id: str, message: str) -> str:
        return call_component(self, ChatRequirementsService, "_handle_confirm", draft=draft, conv_id=conv_id, message=message)

    def _handle_fast_exit(self, draft: dict, conv_id: str, message: str) -> str:
        return call_component(self, ChatRequirementsService, "_handle_fast_exit", draft=draft, conv_id=conv_id, message=message)

    def _dispatch_from_draft(self, draft: dict, conv_id: str) -> str:
        return call_component(self, ChatRequirementsService, "_dispatch_from_draft", draft=draft, conv_id=conv_id)

    _extract_project_context = staticmethod(ChatRequirementsService._extract_project_context)

    def _build_intake_reply(self, draft: dict) -> str:
        return call_component(self, ChatRequirementsService, "_build_intake_reply", draft=draft)

    _build_gap_fill_reply = staticmethod(ChatRequirementsService._build_gap_fill_reply)

    def _build_confirm_reply(self, draft: dict) -> str:
        return call_component(self, ChatRequirementsService, "_build_confirm_reply", draft=draft)

    # ── 公开接口 ──────────────────────────────────────────────────

    def _handle_requirement_flow(self, message: str, cid: str, chat_history: list, discuss_context: Optional[dict], pm_thread_id: str) -> Optional[dict]:
        return call_component(self, ChatRequirementsService, "_handle_requirement_flow", message=message, cid=cid, chat_history=chat_history, discuss_context=discuss_context, pm_thread_id=pm_thread_id)

    @_serialized_chat
    def chat(self, message: str, conv_id: str = "", history: Optional[list] = None,
             pm_thread_id: str = "", discuss_context: Optional[dict] = None) -> dict:
        """处理用户消息, 返回回复。

        Args:
            message: 用户输入文本
            conv_id: 对话 ID (留空则用当前活跃对话)
            history: 可选外部历史 (向后兼容)
            pm_thread_id: 方案C - 若指定, 消息直接路由到 PM 线程 (L2), 跳过秘书 LLM
            discuss_context: 优化讨论上下文; 非空时跳过命令意图, 仅纯对话

        Returns:
            {"reply": str, "action_taken": str, "timestamp": float, "conv_id": str}
        """
        # 确定对话
        cid = conv_id or self._active_conv_id
        if not cid or cid not in self._conversations:
            cid = self._conv_index[0]["id"] if self._conv_index else self._create_conversation("默认对话")
        self._active_conv_id = cid

        # ── 方案C: L2 路由 — PM 线程内直接对话 ──
        if pm_thread_id:
            result = self.send_to_pm_thread(cid, pm_thread_id, message)
            return {
                "reply": result.get("reply", ""),
                "action_taken": "pm_thread_direct",
                "timestamp": result.get("timestamp", time.time()),
                "conv_id": cid,
                "pm_id": pm_thread_id,
                "layer": "L2",
            }

        # 获取对话历史
        chat_history = history if history is not None else self._conversations.get(cid, [])

        req_result = self._handle_requirement_flow(
            message, cid, chat_history, discuss_context, pm_thread_id)
        if req_result is not None:
            return req_result

        # 1. 构建状态上下文
        status_context = self._build_status_context()

        # 2. 构建 system prompt
        system_prompt = self._build_system_prompt(status_context)
        discussion_context = ""
        if discuss_context:
            discussion_context = self._build_discussion_context(discuss_context)
            if discussion_context:
                system_prompt = f"{system_prompt}\n\n{discussion_context}"

        # 2.5 BUG-031: 先判定意图再生成回复 — 让 LLM 知道后台到底会不会
        # 执行动作, 避免「动作未触发却回复正在处理」的静默失败 (见
        # _build_action_guard 说明)。讨论模式不参与命令检测。
        pending_action = ("" if discuss_context else
                          self._detect_action_with_context(
                              message, chat_history))
        # iter-79: 关键词与上下文继承都未命中时, 疑似指令交给 LLM 分类
        if not pending_action and not discuss_context:
            pending_action = self._classify_action_llm(
                message, chat_history)
        guard = self._build_action_guard(pending_action, discuss_context)
        if guard:
            system_prompt = f"{system_prompt}\n\n{guard}"

        # 3. 拼接对话历史 + 用户消息
        prompt = self._build_prompt(message, chat_history)

        reply_text = self._call_chat_llm(message, prompt, system_prompt)
        reply_text, action_taken = self._apply_chat_action(
            message, reply_text, discuss_context, pending_action)
        now = self._save_chat_turn(cid, message, reply_text, action_taken)
        return {"reply": reply_text, "action_taken": action_taken,
                "timestamp": now, "conv_id": cid}

    def _call_chat_llm(self, message: str, prompt: str,
                       system_prompt: str) -> str:
        """调用秘书 LLM, 优先默认模型并保留降级链。"""
        model_pref, fallback_models = self._resolve_chat_model_pref(message)
        resp = self.runtime._call_llm_with_routing(prompt, {
            "_model_preference": model_pref,
            "_fallback_models": fallback_models,
            "_system_prompt": system_prompt,
            "description": message,
        })
        return resp.get("content", "[LLM 调用失败]")

    def _resolve_chat_model_pref(self, message: str) -> tuple[str, list]:
        """解析对话链路的首选模型与降级链 (回复与意图分类共用)。"""
        model_pref = ""
        fallback_models = []
        default_model = getattr(self.controller, "_default_model", "")
        if default_model:
            model_pref = default_model
            if self.controller.model_router:
                entry = self.controller.model_router._entries.get(default_model)
                if entry and hasattr(entry, "fallback"):
                    fallback_models = entry.fallback or []
        elif self.controller.model_router:
            try:
                routing = self.controller.model_router.route(
                    message, skill="document_summary")
                model_pref = routing.selected_model
                fallback_models = routing.fallback_chain
            except Exception:
                pass
        return model_pref, fallback_models

    def _build_action_guard(self, pending_action: str,
                            discuss_context: Optional[dict]) -> str:
        """BUG-031: 按「后台是否真会执行动作」注入约束, 防静默失败。

        起因: 角色卡要求 Boss 下达操作指令时回复「系统正在处理您的指令」,
        但该规则未区分动作是否真被识别。关键词未命中时后台什么都不做,
        LLM 却照常承诺处理中, Boss 便在等一个从未开始的操作 (实测: 股票
        自动交易系统项目对话, action_taken 全程为空, projects 表无记录)。
        故把判定结果显式告知 LLM: 命中则可确认, 未命中必须引导补关键词。
        """
        if discuss_context:
            return ""
        if pending_action:
            return (
                "# 本轮后台动作判定\n"
                f"系统已识别到操作意图 `{pending_action}`, 后台将在你回复后"
                "真实执行, 结果以「📋 操作结果」追加。你可以回复确认收到, "
                "但仍不得声称自己完成了操作。"
            )
        return (
            "# 本轮后台动作判定 (重要)\n"
            "系统**未**识别到任何操作意图, 本轮后台不会执行任何动作。\n"
            "因此绝对禁止回复「系统正在处理」「正在创建」「已提交」这类"
            "暗示后台有动作的措辞, 也不要提及「📋 操作结果」。\n"
            "若 Boss 显然想执行操作 (如创建项目/提交任务), 你必须明确告知"
            "本轮未触发, 并给出可直接复制的指令原文, 例如:\n"
            "「创建项目: <项目名>, 本地地址 <路径>」\n"
            "「提交任务: <任务描述>」\n"
            "说明必须包含触发关键词 (如「创建项目」「提交任务」) 才会执行。"
        )

    def _apply_chat_action(self, message: str, reply_text: str,
                           discuss_context: Optional[dict],
                           pending_action: str = "") -> tuple[str, str]:
        """执行普通消息命令; 讨论模式仅标记 opt_discuss。

        pending_action 由 chat() 预先判定并已注入 prompt; 省略时回退到
        即时检测, 保持旧调用方 (含测试) 的向后兼容。
        """
        if discuss_context:
            print("[Chat] 优化讨论消息已跳过命令检测", flush=True)
            return reply_text, "opt_discuss"
        action = pending_action or self._detect_action_with_context(
            message)
        if not action:
            return self._append_no_action_notice(message, reply_text), ""
        action_result = self._execute_action(action, message)
        if action_result:
            return f"{reply_text}\n\n📋 **操作结果**: {action_result}", action
        return reply_text, ""

    def _append_no_action_notice(self, message: str,
                                 reply_text: str) -> str:
        """BUG-031 兜底: LLM 仍谎称后台在处理时, 追加显式澄清。

        prompt 约束不是硬保证 (模型可能不听), 故在无动作这条路径上做
        文本兜底: 命中承诺类措辞就追加提示, 让失败可见而非静默。
        """
        if not reply_text:
            return reply_text
        claims = ("正在处理", "正在创建", "正在提交", "已提交", "已创建",
                  "已下发", "已经在处理", "系统处理中", "操作结果")
        if not any(c in reply_text for c in claims):
            return reply_text
        hint = self._guess_intent_hint(message)
        print("[Chat] 检测到无动作却承诺执行, 已追加澄清提示", flush=True)
        return (
            f"{reply_text}\n\n"
            "⚠️ **系统提示**: 本轮未识别到可执行指令, 后台**没有**执行任何"
            f"操作。上面关于处理中的说法不成立。{hint}"
        )

    @staticmethod
    def _guess_intent_hint(message: str) -> str:
        """按用户措辞猜可能想做什么, 给出带触发词的指令样例。"""
        if any(k in message for k in ("项目", "仓库", "分支", "repo")):
            return ("如需创建项目, 请发送含关键词的指令, 例如:\n"
                    "「创建项目: <项目名>, 本地地址 <路径>」")
        if any(k in message for k in ("任务", "开发", "实现", "做")):
            return ("如需提交任务, 请发送含关键词的指令, 例如:\n"
                    "「提交任务: <任务描述>」")
        return ("如需执行操作, 请在指令中包含触发关键词, "
                "例如「创建项目」「提交任务」「查看任务」。")

    def _save_chat_turn(self, cid: str, message: str, reply_text: str,
                        action_taken: str) -> float:
        """保存一轮对话到内存、共享文件夹和数据库。"""
        now = time.time()
        user_msg = {"role": "user", "content": message, "timestamp": now}
        asst_msg = {"role": "assistant", "content": reply_text,
                    "timestamp": now, "action_taken": action_taken}
        self._conversations.setdefault(cid, [])
        self._conversations[cid].extend((user_msg, asst_msg))
        if len(self._conversations[cid]) > self._max_history * 2:
            self._conversations[cid] = self._conversations[cid][-(self._max_history * 2):]
        self._save_message_to_file(cid, user_msg)
        self._save_message_to_file(cid, asst_msg)
        self._save_to_db("user", message, timestamp=now)
        self._save_to_db("assistant", reply_text,
                         action_taken=action_taken, timestamp=now)
        self._touch_conv(cid)
        if len(self._conversations.get(cid, [])) == 2:
            meta = self._get_conv_meta(cid)
            if meta and meta.get("title", "") in ("新对话", "默认对话", ""):
                self._auto_title(cid, message)
        return now

    def get_history(self, limit: int=50, conv_id: str='') -> list[dict]:
        return call_component(self, ChatHistoryService, "get_history", limit=limit, conv_id=conv_id)

    def _build_discussion_context(self, discuss_context: dict) -> str:
        return call_component(self, ChatContextService, "_build_discussion_context", discuss_context=discuss_context)

    def _format_discussion_item(self, item: dict) -> str:
        return call_component(self, ChatContextService, "_format_discussion_item", item=item)

    def _format_discussion_overview(self, optimizer) -> str:
        return call_component(self, ChatContextService, "_format_discussion_overview", optimizer=optimizer)

    def clear_history(self, conv_id: str='') -> Any:
        return call_component(self, ChatHistoryService, "clear_history", conv_id=conv_id)

    # ── 多对话管理 API ─────────────────────────────────────────

    def list_conversations(self) -> list[dict]:
        return call_component(self, ChatHistoryService, "list_conversations")

    def create_conversation(self, title: str, project_id: str='') -> dict:
        return call_component(self, ChatHistoryService, "create_conversation", title=title, project_id=project_id)

    def delete_conversation(self, conv_id: str) -> bool:
        return call_component(self, ChatHistoryService, "delete_conversation", conv_id=conv_id)

    def rename_conversation(self, conv_id: str, title: str) -> bool:
        return call_component(self, ChatHistoryService, "rename_conversation", conv_id=conv_id, title=title)

    def get_messages(self, conv_id: str, limit: int=100) -> list[dict]:
        return call_component(self, ChatHistoryService, "get_messages", conv_id=conv_id, limit=limit)

    def switch_conversation(self, conv_id: str) -> bool:
        return call_component(self, ChatHistoryService, "switch_conversation", conv_id=conv_id)

    # ── 方案C: PM 线程管理 (L2 层) ───────────────────────

    def attach_pm_thread(self, conv_id: str, pm_id: str, task_name: str='', agent_name: str='') -> dict:
        return call_component(self, ChatHistoryService, "attach_pm_thread", conv_id=conv_id, pm_id=pm_id, task_name=task_name, agent_name=agent_name)

    def detach_pm_thread(self, conv_id: str, pm_id: str) -> bool:
        return call_component(self, ChatHistoryService, "detach_pm_thread", conv_id=conv_id, pm_id=pm_id)

    def update_pm_thread_status(self, pm_id: str, status: str, **kwargs) -> Any:
        return call_component(self, ChatHistoryService, "update_pm_thread_status", pm_id=pm_id, status=status, **kwargs)

    def get_pm_threads(self, conv_id: str) -> list[dict]:
        return call_component(self, ChatHistoryService, "get_pm_threads", conv_id=conv_id)

    def find_conv_by_pm(self, pm_id: str) -> str:
        return call_component(self, ChatHistoryService, "find_conv_by_pm", pm_id=pm_id)

    def delete_pm_thread(self, pm_id: str) -> Any:
        return call_component(self, ChatHistoryService, "delete_pm_thread", pm_id=pm_id)

    # ── 方案C: PM 线程消息 (L2 层存储) ─────────────────────

    def send_to_pm_thread(self, conv_id: str, pm_id: str, message: str) -> dict:
        return call_component(self, ChatHistoryService, "send_to_pm_thread", conv_id=conv_id, pm_id=pm_id, message=message)

    def get_pm_thread_messages(self, pm_id: str, limit: int=100) -> list[dict]:
        return call_component(self, ChatHistoryService, "get_pm_thread_messages", pm_id=pm_id, limit=limit)

    def append_pm_thread_message(self, pm_id: str, role: str, content: str, **kwargs) -> Any:
        return call_component(self, ChatHistoryService, "append_pm_thread_message", pm_id=pm_id, role=role, content=content, **kwargs)

    def _trim_pm_thread(self, pm_id: str) -> Any:
        return call_component(self, ChatHistoryService, "_trim_pm_thread", pm_id=pm_id)

    def notify_pm_clarification(self, pm_id: str, question: str, options: list=None) -> Any:
        return call_component(self, ChatHistoryService, "notify_pm_clarification", pm_id=pm_id, question=question, options=options)

    # ── 对话持久化 (共享文件夹) ─────────────────────────────────

    def _get_conv_dir(self) -> Any:
        return call_component(self, ChatHistoryService, "_get_conv_dir")

    def _load_conv_index(self) -> Any:
        return call_component(self, ChatHistoryService, "_load_conv_index")

    def _save_conv_index(self) -> Any:
        return call_component(self, ChatHistoryService, "_save_conv_index")

    def _create_conversation(self, title: str, project_id: str='') -> str:
        return call_component(self, ChatHistoryService, "_create_conversation", title=title, project_id=project_id)

    def _get_conv_meta(self, conv_id: str) -> dict:
        return call_component(self, ChatHistoryService, "_get_conv_meta", conv_id=conv_id)

    def _touch_conv(self, conv_id: str) -> Any:
        return call_component(self, ChatHistoryService, "_touch_conv", conv_id=conv_id)

    def _save_message_to_file(self, conv_id: str, msg: dict) -> Any:
        return call_component(self, ChatHistoryService, "_save_message_to_file", conv_id=conv_id, msg=msg)

    def _load_conversation_from_file(self, conv_id: str) -> list[dict]:
        return call_component(self, ChatHistoryService, "_load_conversation_from_file", conv_id=conv_id)

    def _clear_conv_file(self, conv_id: str) -> Any:
        return call_component(self, ChatHistoryService, "_clear_conv_file", conv_id=conv_id)

    def _delete_conv_file(self, conv_id: str) -> Any:
        return call_component(self, ChatHistoryService, "_delete_conv_file", conv_id=conv_id)

    # ── 方案C: PM 线程持久化 ───────────────────────────────

    def _get_thread_dir(self) -> Any:
        return call_component(self, ChatHistoryService, "_get_thread_dir")

    def _save_thread_index(self, conv_id: str) -> Any:
        return call_component(self, ChatHistoryService, "_save_thread_index", conv_id=conv_id)

    def _save_thread_message_to_file(self, pm_id: str, msg: dict) -> Any:
        return call_component(self, ChatHistoryService, "_save_thread_message_to_file", pm_id=pm_id, msg=msg)

    def _load_thread_messages_from_file(self, pm_id: str) -> list[dict]:
        return call_component(self, ChatHistoryService, "_load_thread_messages_from_file", pm_id=pm_id)

    def _auto_title(self, conv_id: str, first_message: str) -> Any:
        return call_component(self, ChatHistoryService, "_auto_title", conv_id=conv_id, first_message=first_message)

    # ── 持久化辅助方法 ────────────────────────────────────────────

    def _load_history_from_db(self) -> list[dict]:
        return call_component(self, ChatHistoryService, "_load_history_from_db")

    def _save_to_db(self, role: str, content: str, action_taken: str='', timestamp: float=0) -> Any:
        return call_component(self, ChatHistoryService, "_save_to_db", role=role, content=content, action_taken=action_taken, timestamp=timestamp)

    # ── 内部方法 ──────────────────────────────────────────────────

    def _build_status_context(self) -> str:
        return call_component(self, ChatContextService, "_build_status_context")

    def _build_system_prompt(self, status_context: str) -> str:
        return call_component(self, ChatContextService, "_build_system_prompt", status_context=status_context)

    def _build_prompt(self, message: str, history: list) -> str:
        return call_component(self, ChatContextService, "_build_prompt", message=message, history=history)

    def _detect_action(self, message: str) -> str:
        return call_component(self, ChatIntentService, "_detect_action", message=message)

    def _detect_action_with_context(self, message: str, history: Optional[list]=None) -> str:
        return call_component(self, ChatIntentService, "_detect_action_with_context", message=message, history=history)

    _is_confirmation = staticmethod(ChatIntentService._is_confirmation)

    def _last_suggested_action(self, history: Optional[list]) -> str:
        return call_component(self, ChatIntentService, "_last_suggested_action", history=history)

    _extract_quoted = staticmethod(ChatIntentService._extract_quoted)

    _looks_like_command = staticmethod(ChatIntentService._looks_like_command)

    def _build_classifier_prompt(self, message: str, history: Optional[list]) -> str:
        return call_component(self, ChatIntentService, "_build_classifier_prompt", message=message, history=history)

    _parse_classifier_json = staticmethod(ChatIntentService._parse_classifier_json)

    def _classify_action_llm(self, message: str, history: Optional[list]=None) -> str:
        return call_component(self, ChatIntentService, "_classify_action_llm", message=message, history=history)

    def _execute_action(self, action: str, message: str) -> str:
        return call_component(self, ChatCommandService, "_execute_action", action=action, message=message)

    # ── 操作实现 ──────────────────────────────────────────────────

    def _parse_task_from_message(self, message: str) -> dict:
        return call_component(self, ChatCommandService, "_parse_task_from_message", message=message)

    def _action_submit_task(self, message: str) -> str:
        return call_component(self, ChatCommandService, "_action_submit_task", message=message)

    def _action_workstation_optimization(self, message: str) -> str:
        return call_component(self, ChatCommandService, "_action_workstation_optimization", message=message)

    def _parse_optimization_source(self, message: str) -> str:
        return call_component(self, ChatCommandService, "_parse_optimization_source", message=message)

    def _parse_optimization_text(self, message: str) -> tuple[str, str]:
        return call_component(self, ChatCommandService, "_parse_optimization_text", message=message)

    def _parse_optimization_priority(self, message: str) -> str:
        return call_component(self, ChatCommandService, "_parse_optimization_priority", message=message)

    def _parse_optimization_decision(self, message: str) -> dict:
        return call_component(self, ChatCommandService, "_parse_optimization_decision", message=message)

    def _is_optimization_query(self, message: str) -> bool:
        return call_component(self, ChatCommandService, "_is_optimization_query", message=message)

    def _format_optimization_item(self, item: dict, headline: str) -> str:
        return call_component(self, ChatCommandService, "_format_optimization_item", item=item, headline=headline)

    def _resolve_project_from_message(self, message: str) -> str:
        return call_component(self, ChatCommandService, "_resolve_project_from_message", message=message)

    def _action_create_project(self, message: str) -> str:
        return call_component(self, ChatCommandService, "_action_create_project", message=message)

    def _action_edit_task_graph(self, message: str) -> str:
        return call_component(self, ChatCommandService, "_action_edit_task_graph", message=message)

    def _parse_graph_edit(self, message: str, graph: dict) -> dict:
        return call_component(self, ChatCommandService, "_parse_graph_edit", message=message, graph=graph)

    def _action_query_status(self) -> str:
        return call_component(self, ChatCommandService, "_action_query_status")

    def _action_query_hosts(self) -> str:
        return call_component(self, ChatCommandService, "_action_query_hosts")

    def _action_query_tasks(self) -> str:
        return call_component(self, ChatCommandService, "_action_query_tasks")

    def _action_query_progress(self) -> str:
        return call_component(self, ChatCommandService, "_action_query_progress")

    def _action_activate_secretary(self) -> str:
        return call_component(self, ChatCommandService, "_action_activate_secretary")

    def _action_deactivate_secretary(self) -> str:
        return call_component(self, ChatCommandService, "_action_deactivate_secretary")

    # ── 优化8: 取消/暂停任务操作 ──

    def _action_cancel_task(self, message: str) -> str:
        return call_component(self, ChatCommandService, "_action_cancel_task", message=message)

    def _action_pause_task(self, message: str) -> str:
        return call_component(self, ChatCommandService, "_action_pause_task", message=message)

    def _extract_task_id_from_message(self, message: str) -> str:
        return call_component(self, ChatCommandService, "_extract_task_id_from_message", message=message)

    # ── 优化7: 回复 PM 决策 ──

    def _action_respond_to_pm(self, message: str) -> str:
        return call_component(self, ChatCommandService, "_action_respond_to_pm", message=message)

    def _extract_pm_id_from_message(self, message: str) -> str:
        return call_component(self, ChatCommandService, "_extract_pm_id_from_message", message=message)

    # ── 优化9: 验收/退回交付物 ──

    def _action_accept_delivery(self, message: str) -> str:
        return call_component(self, ChatCommandService, "_action_accept_delivery", message=message)

    def _action_reject_delivery(self, message: str) -> str:
        return call_component(self, ChatCommandService, "_action_reject_delivery", message=message)

    def _find_latest_delivered_task(self) -> Any:
        return call_component(self, ChatCommandService, "_find_latest_delivered_task")

    # ── 优化14: 任务记忆辅助 ──

    def _get_task_memory_hint(self, task_name: str, task_desc: str) -> str:
        return call_component(self, ChatCommandService, "_get_task_memory_hint", task_name=task_name, task_desc=task_desc)
