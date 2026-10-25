"""ChatRequirementsService: extracted domain behavior with explicit component ownership."""

import re
import time
import json
from typing import Optional
from .chat_policy import (_REQ_PHASE_INTAKE, _REQ_PHASE_SYNTHESIZE, _REQ_PHASE_GAP_FILL, _REQ_PHASE_CONFIRM, _REQ_PHASE_DISPATCHED, _REQ_INTAKE_KEYWORDS, _REQ_FAST_EXIT_KEYWORDS, _REQ_CANCEL_KEYWORDS, _REQ_FINAL_PROMPT_PREFIX, REQ_CHECKLIST_TEMPLATES, _REQ_DEFAULT_CHECKLIST, _extract_local_path)

from .component_service import ComponentService

class ChatRequirementsService(ComponentService):
    """Single domain implementation behind the existing facade."""

    OPERATIONS = ('_load_req_drafts', '_save_req_draft', '_get_or_init_req_draft', '_clear_req_draft', '_infer_task_type_from_message', '_detect_missing_items', '_fill_checklist_from_message', '_generate_brief', '_build_final_prompt', '_apply_final_prompt', '_try_requirement_flow', '_handle_active_draft', '_handle_intake', '_handle_gap_fill', '_handle_confirm', '_handle_fast_exit', '_dispatch_from_draft', '_build_intake_reply', '_build_confirm_reply', '_handle_requirement_flow')

    def _load_req_drafts(self) -> None:
        """从对话索引恢复未完成的需求收集草稿。"""
        for meta in self.owner._conv_index:
            draft = meta.get("requirement_draft")
            if draft and draft.get("phase") != _REQ_PHASE_DISPATCHED:
                self.owner._req_drafts[meta["id"]] = draft


    def _save_req_draft(self, conv_id: str) -> None:
        """把需求收集草稿写回对话索引。"""
        draft = self.owner._req_drafts.get(conv_id)
        if not draft:
            return
        meta = self.owner._get_conv_meta(conv_id)
        if meta:
            meta["requirement_draft"] = draft
            self.owner._save_conv_index()


    def _get_or_init_req_draft(self, conv_id: str,
                               task_type: str = "general") -> dict:
        """获取或初始化需求收集草稿。"""
        if conv_id not in self.owner._req_drafts:
            template = REQ_CHECKLIST_TEMPLATES.get(
                task_type, _REQ_DEFAULT_CHECKLIST)
            checklist = {
                key: {
                    "label": label,
                    "value": "",
                    "is_critical": critical,
                    "filled": False,
                }
                for key, label, critical, _ in template
            }
            self.owner._req_drafts[conv_id] = {
                "phase": _REQ_PHASE_INTAKE,
                "task_type": task_type,
                "checklist": checklist,
                "raw_messages": [],
                "brief": "",
                "rounds": 0,
                "created_at": time.time(),
            }
            self.owner._save_req_draft(conv_id)
        return self.owner._req_drafts[conv_id]


    def _clear_req_draft(self, conv_id: str) -> None:
        """清除需求收集草稿。"""
        self.owner._req_drafts.pop(conv_id, None)
        meta = self.owner._get_conv_meta(conv_id)
        if meta:
            meta.pop("requirement_draft", None)
            self.owner._save_conv_index()


    def _infer_task_type_from_message(self, message: str) -> str:
        """从消息推断任务类型。"""
        try:
            from .pm_planner import PMPlanner
            return PMPlanner.infer_task_type("", message)
        except Exception:
            return "general"


    def _detect_missing_items(self, draft: dict) -> list[dict]:
        """检测仍未填充的关键项。"""
        checklist = draft.get("checklist", {})
        missing = []
        for key, item in checklist.items():
            if item.get("is_critical") and not item.get("filled"):
                missing.append({
                    "key": key,
                    "label": item.get("label", key),
                    "question": self.owner._get_item_question(
                        draft.get("task_type", "general"), key),
                })
        return missing


    @staticmethod
    def _get_item_question(task_type: str, item_key: str) -> str:
        """从模板取某个字段的追问文案。"""
        template = REQ_CHECKLIST_TEMPLATES.get(
            task_type, _REQ_DEFAULT_CHECKLIST)
        for key, _, _, question in template:
            if key == item_key:
                return question
        return f"请补充 {item_key} 相关信息"


    def _fill_checklist_from_message(self, draft: dict, message: str) -> None:
        """用 LLM 从单条消息提取 checklist 字段。"""
        checklist = draft.get("checklist", {})
        if not checklist:
            return
        items_desc = "\n".join(
            f"- {key}: {item.get('label', key)}"
            for key, item in checklist.items()
            if not item.get("filled")
        )
        prompt = (
            "从以下用户消息中提取需求信息，填入结构化字段。"
            "返回严格 JSON，key 为字段名，value 为提取到的内容"
            "（空字符串表示未提及）。\n"
            f"需要提取的字段：\n{items_desc}\n\n"
            f"用户消息：{message}\n\n"
            '返回格式：{"field1": "value1", "field2": ""}'
        )
        try:
            resp = self.owner.runtime._call_llm_with_routing(
                prompt, {"_model_preference": "", "_fallback_models": []})
            content = resp.get("content", "")
            start = content.find("{")
            end = content.rfind("}") + 1
            extracted = (
                json.loads(content[start:end])
                if start >= 0 and end > start else {}
            )
        except Exception:
            extracted = {}
        for key, value in extracted.items():
            if (key in checklist and value
                    and not checklist[key].get("filled")):
                checklist[key]["value"] = str(value)
                checklist[key]["filled"] = True


    def _generate_brief(self, draft: dict) -> str:
        """把 checklist 整理成结构化项目 Brief。"""
        checklist = draft.get("checklist", {})
        task_type = draft.get("task_type", "general")
        type_label = {
            "development": "开发", "code_review": "代码审查",
            "research": "调研", "documentation": "文档",
            "refactoring": "重构", "general": "通用",
        }.get(task_type, task_type)
        lines = [f"## 项目 Brief（{type_label}类）", ""]
        for key, item in checklist.items():
            marker = "[x]" if item.get("filled") else "[ ]"
            value = item.get("value") or "未明确"
            critical = " *" if item.get("is_critical") else ""
            lines.append(
                f"- {marker} {item.get('label', key)}{critical}: {value}")
        missing = self.owner._detect_missing_items(draft)
        if missing:
            lines.extend(["", "### 待补充关键项"])
            for item in missing:
                lines.append(
                    f"- {item['label']}: {item['question']}")
        brief = "\n".join(lines)
        draft["brief"] = brief
        return brief


    def _build_final_prompt(self, draft: dict) -> str:
        """生成可直接复制执行的最终提示词。"""
        checklist = draft.get("checklist", {})
        goal = ""
        for key in ("goal", "topic", "doc_type"):
            item = checklist.get(key, {})
            if item.get("filled"):
                goal = item.get("value", "")
                break
        if not goal:
            goal = "新任务"
        lines = [f"{_REQ_FINAL_PROMPT_PREFIX} {goal}"]
        for key, item in checklist.items():
            if key in ("goal", "topic", "doc_type"):
                continue
            value = item.get("value") or "未明确"
            lines.append(f"【{item.get('label', key)}】{value}")
        return "\n".join(lines)


    @staticmethod
    def _is_final_prompt(message: str) -> bool:
        """判断消息是否是可执行的最终提示词。"""
        return message.strip().startswith(_REQ_FINAL_PROMPT_PREFIX)


    @staticmethod
    def _is_cancel(message: str) -> bool:
        """判断是否取消需求收集。"""
        text = message.strip()
        return any(keyword in text for keyword in _REQ_CANCEL_KEYWORDS)


    def _apply_final_prompt(self, draft: dict, conv_id: str,
                            message: str) -> None:
        """把 Boss 修改后的最终提示词回填进草稿。"""
        lines = [line.strip() for line in message.strip().split("\n")]
        checklist = draft.get("checklist", {})
        first = lines[0] if lines else ""
        if first.startswith(_REQ_FINAL_PROMPT_PREFIX):
            goal = first[len(_REQ_FINAL_PROMPT_PREFIX):].strip()
            if goal:
                for key in ("goal", "topic", "doc_type"):
                    if key in checklist:
                        checklist[key]["value"] = goal
                        checklist[key]["filled"] = True
                        break
        for line in lines[1:]:
            if not line.startswith("【"):
                continue
            end = line.find("】")
            if end <= 1:
                continue
            label = line[1:end]
            value = line[end + 1:].strip()
            for item in checklist.values():
                if item.get("label") == label:
                    if value and value != "未明确":
                        item["value"] = value
                        item["filled"] = True
                    break
        self.owner._generate_brief(draft)
        self.owner._save_req_draft(conv_id)


    def _try_requirement_flow(self, message: str, conv_id: str,
                              chat_history: list) -> str:
        """尝试进入需求收集流程；空串表示不匹配。"""
        draft = self.owner._req_drafts.get(conv_id)
        if draft and draft.get("phase") != _REQ_PHASE_DISPATCHED:
            if self.owner._is_cancel(message):
                self.owner._clear_req_draft(conv_id)
                return "已取消本次需求收集。你可以直接说「提交任务: ...」或重新开始。"
            if self.owner._is_final_prompt(message):
                self.owner._apply_final_prompt(draft, conv_id, message)
                return self.owner._dispatch_from_draft(draft, conv_id)
            is_fast_exit = any(
                keyword in message for keyword in _REQ_FAST_EXIT_KEYWORDS)
            return self.owner._handle_active_draft(
                draft, conv_id, message, chat_history, is_fast_exit)
        if not draft and any(
                keyword in message for keyword in _REQ_INTAKE_KEYWORDS):
            task_type = self.owner._infer_task_type_from_message(message)
            draft = self.owner._get_or_init_req_draft(conv_id, task_type)
            draft["raw_messages"].append(message)
            self.owner._fill_checklist_from_message(draft, message)
            draft["rounds"] = 1
            self.owner._save_req_draft(conv_id)
            return self.owner._build_intake_reply(draft)
        return ""


    def _handle_active_draft(self, draft: dict, conv_id: str,
                             message: str, chat_history: list,
                             is_fast_exit: bool) -> str:
        """按草稿阶段路由消息。"""
        if is_fast_exit:
            return self.owner._handle_fast_exit(draft, conv_id, message)
        phase = draft.get("phase", _REQ_PHASE_INTAKE)
        if phase == _REQ_PHASE_GAP_FILL:
            return self.owner._handle_gap_fill(draft, conv_id, message)
        if phase == _REQ_PHASE_CONFIRM:
            return self.owner._handle_confirm(draft, conv_id, message)
        return self.owner._handle_intake(draft, conv_id, message)


    def _handle_intake(self, draft: dict, conv_id: str,
                       message: str) -> str:
        """处理收集阶段：累积消息并填充 checklist。"""
        draft["raw_messages"].append(message)
        self.owner._fill_checklist_from_message(draft, message)
        draft["rounds"] += 1
        self.owner._save_req_draft(conv_id)
        missing = self.owner._detect_missing_items(draft)
        if not missing or draft["rounds"] >= 3:
            draft["phase"] = _REQ_PHASE_SYNTHESIZE
            self.owner._generate_brief(draft)
            remaining = self.owner._detect_missing_items(draft)
            if remaining:
                draft["phase"] = _REQ_PHASE_GAP_FILL
                self.owner._save_req_draft(conv_id)
                return self.owner._build_gap_fill_reply(draft, remaining)
            draft["phase"] = _REQ_PHASE_CONFIRM
            self.owner._save_req_draft(conv_id)
            return self.owner._build_confirm_reply(draft)
        return self.owner._build_intake_reply(draft)


    def _handle_gap_fill(self, draft: dict, conv_id: str,
                         message: str) -> str:
        """处理补缺阶段。"""
        draft["raw_messages"].append(message)
        self.owner._fill_checklist_from_message(draft, message)
        draft["rounds"] += 1
        self.owner._save_req_draft(conv_id)
        missing = self.owner._detect_missing_items(draft)
        if not missing or draft["rounds"] >= 5:
            draft["phase"] = _REQ_PHASE_CONFIRM
            self.owner._generate_brief(draft)
            self.owner._save_req_draft(conv_id)
            return self.owner._build_confirm_reply(draft)
        return self.owner._build_gap_fill_reply(draft, missing)


    def _handle_confirm(self, draft: dict, conv_id: str,
                        message: str) -> str:
        """处理确认阶段：确认则派发，否则回到收集。"""
        if self.owner._is_confirmation(message):
            return self.owner._dispatch_from_draft(draft, conv_id)
        draft["phase"] = _REQ_PHASE_INTAKE
        return self.owner._handle_intake(draft, conv_id, message)


    def _handle_fast_exit(self, draft: dict, conv_id: str,
                          message: str) -> str:
        """处理快速退出：直接生成 Brief 并派发。"""
        self.owner._fill_checklist_from_message(draft, message)
        self.owner._generate_brief(draft)
        self.owner._save_req_draft(conv_id)
        return self.owner._dispatch_from_draft(draft, conv_id)


    def _dispatch_from_draft(self, draft: dict, conv_id: str) -> str:
        """从需求收集草稿创建任务并交给 PM Agent。"""
        brief = self.owner._generate_brief(draft)
        checklist = draft.get("checklist", {})
        goal = ""
        for key in ("goal", "topic", "doc_type"):
            item = checklist.get(key, {})
            if item.get("filled"):
                goal = item.get("value", "")
                break
        if not goal:
            goal = "新任务"
        priority_value = checklist.get("priority", {}).get("value", "")
        priority = "normal"
        if any(word in priority_value for word in ("紧急", "urgent", "立刻")):
            priority = "urgent"
        elif any(word in priority_value for word in ("优先", "重要", "high", "高")):
            priority = "high"
        elif any(word in priority_value for word in ("不急", "低", "low")):
            priority = "low"
        input_data = {"requirement": brief}
        project_context = self.owner._extract_project_context(draft)
        input_data.update(project_context)
        try:
            result = self.owner.controller.submit_task_from_chat(
                name=goal[:50], description=brief,
                created_by="secretary", priority=priority,
                input_data=input_data)
        except Exception as exc:
            print(f"[Chat] 需求收集派发失败: {exc}", flush=True)
            return f"任务创建失败: {exc}"
        if result.get("status") == "failed":
            error = result.get("output_data", {}).get("error", "未知错误")
            return f"任务创建失败: {error}"
        task_id = result.get("task_id", "")
        pm_id = str(result.get("pm_agent_id", ""))[:8]
        self.owner._clear_req_draft(conv_id)
        priority_label = {
            "low": "低", "normal": "普通",
            "high": "高", "urgent": "紧急",
        }.get(priority, priority)
        return (
            "✅ 需求收集完成，任务已创建并分配 PM Agent!\n"
            f"- 任务ID: {task_id}\n"
            f"- 名称: {goal[:50]}\n"
            f"- 优先级: {priority_label}\n"
            f"- PM Agent: {pm_id}\n\n"
            f"--- 项目 Brief ---\n{brief}"
        )


    @staticmethod
    def _extract_project_context(draft: dict) -> dict:
        """从需求文本中提取项目路径、仓库与执行模式。"""
        texts = list(draft.get("raw_messages", []))
        for item in draft.get("checklist", {}).values():
            texts.append(str(item.get("value", "")))
        text = "\n".join(texts)
        result = {}
        path = _extract_local_path(text)
        if path:
            result["project_path"] = path
        repo_match = re.search(
            r"(?:远端仓库|仓库|repo(?:sitory)?)\s*(?:地址)?(?:为|是)?[:：]?\s*(https?://[^\s，。；;]+)",
            text,
        )
        if not repo_match:
            repo_match = re.search(r"(https?://[^\s，。；;]+)", text)
        if repo_match:
            result["repo_url"] = repo_match.group(1).rstrip("\\.,;，。")
        if any(marker in text for marker in ("只做规划", "规划与任务拆解", "不直接修改")):
            result["execution_mode"] = "planning_only"
        return result


    def _build_intake_reply(self, draft: dict) -> str:
        """构建收集阶段回复。"""
        checklist = draft.get("checklist", {})
        filled = sum(1 for item in checklist.values() if item.get("filled"))
        total = len(checklist)
        missing = self.owner._detect_missing_items(draft)
        lines = [
            f"📝 正在收集项目需求（第 {draft['rounds']} 轮，"
            f"已覆盖 {filled}/{total} 项）",
            "",
        ]
        if missing:
            next_item = missing[0]
            lines.append(f"接下来请补充：**{next_item['label']}**")
            lines.append(next_item["question"])
            lines.append("")
            lines.append("（随时可以说「够了」直接提交，或「取消需求收集」退出）")
        else:
            lines.append("关键信息已收集完整，正在生成项目 Brief...")
        return "\n".join(lines)


    @staticmethod
    def _build_gap_fill_reply(draft: dict, missing: list[dict]) -> str:
        """构建补缺阶段回复。"""
        lines = ["📋 项目 Brief 已初步生成，但以下关键项仍缺失：", ""]
        for item in missing:
            lines.append(f"- **{item['label']}**: {item['question']}")
        lines.extend(["", "请补充以上信息，或说「够了」直接开始执行。"])
        return "\n".join(lines)


    def _build_confirm_reply(self, draft: dict) -> str:
        """构建确认阶段回复，附最终可执行提示词。"""
        brief = draft.get("brief", "")
        final_prompt = self.owner._build_final_prompt(draft)
        return (
            "📋 项目 Brief 已生成，请确认：\n\n"
            f"{brief}\n\n"
            "---\n"
            "**最终执行提示词**（可直接复制，或回复「按最终提示词执行」）：\n\n"
            f"```\n{final_prompt}\n```\n\n"
            "确认无误请回复「开始」或「确认」，需要修改请直接说明。"
        )


    def _handle_requirement_flow(
            self, message: str, cid: str, chat_history: list,
            discuss_context: Optional[dict],
            pm_thread_id: str) -> Optional[dict]:
        """尝试用需求收集状态机处理消息；未命中返回 None。"""
        if discuss_context or pm_thread_id:
            return None
        if not hasattr(self.owner, "_req_drafts"):
            self.owner._req_drafts = {}
        req_reply = self.owner._try_requirement_flow(message, cid, chat_history)
        if not req_reply:
            return None
        now = self.owner._save_chat_turn(
            cid, message, req_reply, "requirement_flow")
        return {
            "reply": req_reply,
            "action_taken": "requirement_flow",
            "timestamp": now,
            "conv_id": cid,
        }

