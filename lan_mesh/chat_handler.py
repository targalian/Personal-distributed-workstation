"""
秘书聊天处理器 — Web 端对话接口

职责:
1. 接收用户 (Boss) 通过 Web 聊天窗口发送的消息
2. 构建工作站状态摘要作为 system prompt 上下文
3. 调用 LLM 生成回复
4. 解析回复中的操作意图并执行
5. 返回回复 + 操作结果

聊天历史持久化到 SQLite DB (chat_history 表), 重启不丢失。
"""
import re
import time
import json
from typing import Optional

from .logger import get_logger
from .role_cards import render_secretary_prompt

logger = get_logger("chat_handler")


# ── 操作意图关键词映射 ──────────────────────────────────────────

_ACTION_KEYWORDS = {
    "优化工作站": "workstation_optimization",
    "工作站优化": "workstation_optimization",
    "遇到瓶颈": "workstation_optimization",
    "工作站限制": "workstation_optimization",
    "添加优化建议": "workstation_optimization",
    "确认优化": "workstation_optimization",
    "拒绝优化": "workstation_optimization",
    # 自然语言 DAG 编辑 (F4.3, iter-51): Boss 口述修改图结构 — 优先级最高
    "编辑图": "edit_task_graph",
    "修改图": "edit_task_graph",
    "调整图": "edit_task_graph",
    "加一步": "edit_task_graph",
    "加个步骤": "edit_task_graph",
    "新增步骤": "edit_task_graph",
    "删一步": "edit_task_graph",
    "删除步骤": "edit_task_graph",
    "跳过步骤": "edit_task_graph",
    "加依赖": "edit_task_graph",
    "删依赖": "edit_task_graph",
    "提交任务": "submit_task",
    "提交一个": "submit_task",
    "创建任务": "submit_task",
    "新任务": "submit_task",
    "下发任务": "submit_task",
    "激活下发": "submit_task",
    "立即下发": "submit_task",
    "分配任务": "submit_task",
    # 自然语言表达 (用户日常说法)
    "做一个": "submit_task",
    "写一个": "submit_task",
    "开发一个": "submit_task",
    "创建一个": "submit_task",
    "搭建一个": "submit_task",
    "设计一个": "submit_task",
    "生成一个": "submit_task",
    "帮我做": "submit_task",
    "帮我写": "submit_task",
    "帮我开发": "submit_task",
    "帮我创建": "submit_task",
    "实现一个": "submit_task",
    "制作一个": "submit_task",
    "搞一个": "submit_task",
    "弄一个": "submit_task",
    "做个": "submit_task",
    "写个": "submit_task",
    "整一个": "submit_task",
    "启动秘书": "activate_secretary",
    "激活秘书": "activate_secretary",
    "停止秘书": "deactivate_secretary",
    "停用秘书": "deactivate_secretary",
    "状态": "query_status",
    "进度": "query_progress",
    "查看主机": "query_hosts",
    "主机列表": "query_hosts",
    "查看任务": "query_tasks",
    "任务列表": "query_tasks",
    # 优化8: 取消/暂停任务
    "取消任务": "cancel_task",
    "终止任务": "cancel_task",
    "停止任务": "cancel_task",
    "暂停任务": "pause_task",
    # 优化7: 回复 PM 决策请求
    "回复PM": "respond_to_pm",
    "告诉PM": "respond_to_pm",
    "告知PM": "respond_to_pm",
    # 优化9: 验收/退回交付物
    "验收": "accept_delivery",
    "验收通过": "accept_delivery",
    "确认通过": "accept_delivery",
    "退回": "reject_delivery",
    "重新做": "reject_delivery",
    # 项目管理
    "创建项目": "create_project",
    "新建项目": "create_project",
    "建立项目": "create_project",
}

_ACTION_DESCRIPTIONS = {
    "submit_task": "提交/创建/下发新任务, 或让秘书做/写/开发/搭建/设计某物",
    "create_project": "创建/新建/建立项目",
    "edit_task_graph": "编辑任务 DAG 图、增删步骤或调整依赖",
    "workstation_optimization": "优化工作站、处理瓶颈或答复优化决策",
    "activate_secretary": "启动/激活秘书",
    "deactivate_secretary": "停止/停用秘书",
    "query_status": "查询工作站整体状态",
    "query_progress": "查询任务或项目进度",
    "query_hosts": "查询主机列表",
    "query_tasks": "查询任务列表",
    "cancel_task": "取消/终止任务",
    "pause_task": "暂停任务",
    "respond_to_pm": "回复/告知 PM 决策或意见",
    "accept_delivery": "验收通过交付物",
    "reject_delivery": "退回/打回交付物要求重做",
}

# LLM 意图分类兜底的触发信号 (iter-79): 命中任一才值得花一次分类
# 调用。纯闲聊/长描述维持零额外成本; 「做一个/写一个」等高频表达已由
# 关键词快路径覆盖, 这里只需兜住「帮我建个项目」这类口语化变体。
_CLASSIFIER_VERB_SIGNALS = (
    "建", "弄", "搞", "查", "删", "停", "关闭", "执行", "生成",
    "启动", "激活", "取消", "暂停", "提交", "下发", "分配", "验收",
    "退回", "打回", "回复", "告知", "告诉", "优化",
    "create", "submit", "cancel", "pause", "optimize",
)
_CLASSIFIER_NOUN_SIGNALS = (
    "项目", "任务", "图", "步骤", "依赖", "主机", "状态", "进度",
    "秘书", "PM", "交付", "瓶颈", "project", "task", "status", "host",
)
_CLASSIFIER_MAX_MESSAGE_LEN = 200

_CLASSIFIER_SYSTEM_PROMPT = (
    "你是 LAN Mesh 秘书的意图分类器。只输出一个 JSON 对象, 格式 "
    '{"action": "<动作名>", "reason": "<12字内理由>"}, 不要输出任何'
    "其他文字。action 必须严格取自候选列表, 无法确定时用 none。"
    "用户只是在提问、闲聊、讨论或求解释时必须输出 none; "
    "只有明确要求执行某项操作时才输出对应动作。"
)


# ── 需求收集状态机 ──────────────────────────────────────────────

_REQ_PHASE_INTAKE = "intake"
_REQ_PHASE_SYNTHESIZE = "synthesize"
_REQ_PHASE_GAP_FILL = "gap_fill"
_REQ_PHASE_CONFIRM = "confirm"
_REQ_PHASE_DISPATCHED = "dispatched"

_REQ_INTAKE_KEYWORDS = (
    "我想做一个", "我想开发", "我需要开发", "帮我规划一个项目",
    "新项目想法", "项目需求", "需求收集", "开始一个新项目",
    "规划项目", "帮我理清需求",
)

_REQ_FAST_EXIT_KEYWORDS = (
    "够了", "开始吧", "直接做", "开始执行", "就这样",
    "不用补充了", "可以了", "就按这个", "提交吧", "按最终提示词执行",
    "按这个执行",
)

_REQ_CANCEL_KEYWORDS = (
    "取消需求收集", "取消收集", "不收集了", "退出需求收集",
)

_REQ_FINAL_PROMPT_PREFIX = "提交任务:"

REQ_CHECKLIST_TEMPLATES: dict[str, list[tuple]] = {
    "development": [
        ("background", "项目背景", True, "这个项目的背景是什么？要解决什么问题？"),
        ("goal", "目标功能", True, "具体要实现哪些功能？核心目标是什么？"),
        ("scope", "范围边界", True, "涉及哪些文件/模块？有哪些明确不做的？"),
        ("tech_stack", "技术栈", False, "有无技术约束（语言/框架/风格）？"),
        ("acceptance", "验收标准", True, "怎样算完成？验收标准是什么？"),
        ("priority", "优先级", False, "这个任务的优先级如何（紧急/高/普通/低）？"),
    ],
    "code_review": [
        ("background", "审查背景", True, "为什么要做这次审查？"),
        ("scope", "审查范围", True, "审查哪些文件/目录/模块？"),
        ("focus", "关注维度", True, "重点关注安全/性能/规范/架构中的哪些方面？"),
        ("severity", "严重级别门槛", False, "什么级别的问题需要阻断？"),
        ("priority", "优先级", False, "这个任务的优先级如何？"),
    ],
    "research": [
        ("background", "调研背景", True, "为什么要做这个调研？"),
        ("topic", "调研主题", True, "具体要调研什么？"),
        ("output_format", "期望产出", False, "期望什么格式的产出（报告/对比表/清单）？"),
        ("depth", "深度要求", False, "需要多深入的调研？"),
        ("priority", "优先级", False, "这个任务的优先级如何？"),
    ],
    "documentation": [
        ("background", "文档背景", True, "为什么要写这份文档？"),
        ("doc_type", "文档类型", True, "是什么类型的文档（API/README/设计文档/用户手册）？"),
        ("audience", "目标读者", True, "文档的目标读者是谁？"),
        ("scope", "覆盖范围", True, "文档需要覆盖哪些内容？"),
        ("priority", "优先级", False, "这个任务的优先级如何？"),
    ],
    "refactoring": [
        ("background", "重构背景", True, "为什么要重构？当前有什么问题？"),
        ("goal", "重构目标", True, "重构的目标是什么（可读性/性能/架构）？"),
        ("scope", "重构范围", True, "涉及哪些文件/模块？"),
        ("red_line", "红线约束", False, "有什么不能改的？"),
        ("priority", "优先级", False, "这个任务的优先级如何？"),
    ],
    "general": [
        ("background", "背景", True, "这个任务的背景是什么？"),
        ("goal", "目标", True, "具体要达成什么目标？"),
        ("scope", "范围", True, "涉及哪些文件/模块？"),
        ("constraints", "约束", False, "有无技术约束或特殊要求？"),
        ("priority", "优先级", False, "这个任务的优先级如何？"),
    ],
}

_REQ_DEFAULT_CHECKLIST = REQ_CHECKLIST_TEMPLATES["general"]


def _find_node_by_name(dag, name: str) -> str:
    """按节点名匹配子任务 ID (精确优先, 模糊包含兜底)。"""
    if not name:
        return ""
    for sid, st in dag.subtasks.items():
        if st.name == name:
            return sid
    for sid, st in dag.subtasks.items():
        if name in (st.name or ""):
            return sid
    return ""


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
        """从对话索引恢复未完成的需求收集草稿。"""
        for meta in self._conv_index:
            draft = meta.get("requirement_draft")
            if draft and draft.get("phase") != _REQ_PHASE_DISPATCHED:
                self._req_drafts[meta["id"]] = draft

    def _save_req_draft(self, conv_id: str) -> None:
        """把需求收集草稿写回对话索引。"""
        draft = self._req_drafts.get(conv_id)
        if not draft:
            return
        meta = self._get_conv_meta(conv_id)
        if meta:
            meta["requirement_draft"] = draft
            self._save_conv_index()

    def _get_or_init_req_draft(self, conv_id: str,
                               task_type: str = "general") -> dict:
        """获取或初始化需求收集草稿。"""
        if conv_id not in self._req_drafts:
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
            self._req_drafts[conv_id] = {
                "phase": _REQ_PHASE_INTAKE,
                "task_type": task_type,
                "checklist": checklist,
                "raw_messages": [],
                "brief": "",
                "rounds": 0,
                "created_at": time.time(),
            }
            self._save_req_draft(conv_id)
        return self._req_drafts[conv_id]

    def _clear_req_draft(self, conv_id: str) -> None:
        """清除需求收集草稿。"""
        self._req_drafts.pop(conv_id, None)
        meta = self._get_conv_meta(conv_id)
        if meta:
            meta.pop("requirement_draft", None)
            self._save_conv_index()

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
                    "question": self._get_item_question(
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
            resp = self.runtime._call_llm_with_routing(
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
        missing = self._detect_missing_items(draft)
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
        self._generate_brief(draft)
        self._save_req_draft(conv_id)

    def _try_requirement_flow(self, message: str, conv_id: str,
                              chat_history: list) -> str:
        """尝试进入需求收集流程；空串表示不匹配。"""
        draft = self._req_drafts.get(conv_id)
        if draft and draft.get("phase") != _REQ_PHASE_DISPATCHED:
            if self._is_cancel(message):
                self._clear_req_draft(conv_id)
                return "已取消本次需求收集。你可以直接说「提交任务: ...」或重新开始。"
            if self._is_final_prompt(message):
                self._apply_final_prompt(draft, conv_id, message)
                return self._dispatch_from_draft(draft, conv_id)
            is_fast_exit = any(
                keyword in message for keyword in _REQ_FAST_EXIT_KEYWORDS)
            return self._handle_active_draft(
                draft, conv_id, message, chat_history, is_fast_exit)
        if not draft and any(
                keyword in message for keyword in _REQ_INTAKE_KEYWORDS):
            task_type = self._infer_task_type_from_message(message)
            draft = self._get_or_init_req_draft(conv_id, task_type)
            draft["raw_messages"].append(message)
            self._fill_checklist_from_message(draft, message)
            draft["rounds"] = 1
            self._save_req_draft(conv_id)
            return self._build_intake_reply(draft)
        return ""

    def _handle_active_draft(self, draft: dict, conv_id: str,
                             message: str, chat_history: list,
                             is_fast_exit: bool) -> str:
        """按草稿阶段路由消息。"""
        if is_fast_exit:
            return self._handle_fast_exit(draft, conv_id, message)
        phase = draft.get("phase", _REQ_PHASE_INTAKE)
        if phase == _REQ_PHASE_GAP_FILL:
            return self._handle_gap_fill(draft, conv_id, message)
        if phase == _REQ_PHASE_CONFIRM:
            return self._handle_confirm(draft, conv_id, message)
        return self._handle_intake(draft, conv_id, message)

    def _handle_intake(self, draft: dict, conv_id: str,
                       message: str) -> str:
        """处理收集阶段：累积消息并填充 checklist。"""
        draft["raw_messages"].append(message)
        self._fill_checklist_from_message(draft, message)
        draft["rounds"] += 1
        self._save_req_draft(conv_id)
        missing = self._detect_missing_items(draft)
        if not missing or draft["rounds"] >= 3:
            draft["phase"] = _REQ_PHASE_SYNTHESIZE
            self._generate_brief(draft)
            remaining = self._detect_missing_items(draft)
            if remaining:
                draft["phase"] = _REQ_PHASE_GAP_FILL
                self._save_req_draft(conv_id)
                return self._build_gap_fill_reply(draft, remaining)
            draft["phase"] = _REQ_PHASE_CONFIRM
            self._save_req_draft(conv_id)
            return self._build_confirm_reply(draft)
        return self._build_intake_reply(draft)

    def _handle_gap_fill(self, draft: dict, conv_id: str,
                         message: str) -> str:
        """处理补缺阶段。"""
        draft["raw_messages"].append(message)
        self._fill_checklist_from_message(draft, message)
        draft["rounds"] += 1
        self._save_req_draft(conv_id)
        missing = self._detect_missing_items(draft)
        if not missing or draft["rounds"] >= 5:
            draft["phase"] = _REQ_PHASE_CONFIRM
            self._generate_brief(draft)
            self._save_req_draft(conv_id)
            return self._build_confirm_reply(draft)
        return self._build_gap_fill_reply(draft, missing)

    def _handle_confirm(self, draft: dict, conv_id: str,
                        message: str) -> str:
        """处理确认阶段：确认则派发，否则回到收集。"""
        if self._is_confirmation(message):
            return self._dispatch_from_draft(draft, conv_id)
        draft["phase"] = _REQ_PHASE_INTAKE
        return self._handle_intake(draft, conv_id, message)

    def _handle_fast_exit(self, draft: dict, conv_id: str,
                          message: str) -> str:
        """处理快速退出：直接生成 Brief 并派发。"""
        self._fill_checklist_from_message(draft, message)
        self._generate_brief(draft)
        self._save_req_draft(conv_id)
        return self._dispatch_from_draft(draft, conv_id)

    def _dispatch_from_draft(self, draft: dict, conv_id: str) -> str:
        """从需求收集草稿创建任务并交给 PM Agent。"""
        brief = self._generate_brief(draft)
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
        elif any(word in priority_value for word in ("优先", "重要", "high")):
            priority = "high"
        elif any(word in priority_value for word in ("不急", "低", "low")):
            priority = "low"
        try:
            result = self.controller.submit_task_from_chat(
                name=goal[:50], description=brief,
                created_by="secretary", priority=priority,
                input_data={"requirement": brief})
        except Exception as exc:
            print(f"[Chat] 需求收集派发失败: {exc}", flush=True)
            return f"任务创建失败: {exc}"
        if result.get("status") == "failed":
            error = result.get("output_data", {}).get("error", "未知错误")
            return f"任务创建失败: {error}"
        task_id = result.get("task_id", "")
        pm_id = str(result.get("pm_agent_id", ""))[:8]
        self._clear_req_draft(conv_id)
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

    def _build_intake_reply(self, draft: dict) -> str:
        """构建收集阶段回复。"""
        checklist = draft.get("checklist", {})
        filled = sum(1 for item in checklist.values() if item.get("filled"))
        total = len(checklist)
        missing = self._detect_missing_items(draft)
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
        final_prompt = self._build_final_prompt(draft)
        return (
            "📋 项目 Brief 已生成，请确认：\n\n"
            f"{brief}\n\n"
            "---\n"
            "**最终执行提示词**（可直接复制，或回复「按最终提示词执行」）：\n\n"
            f"```\n{final_prompt}\n```\n\n"
            "确认无误请回复「开始」或「确认」，需要修改请直接说明。"
        )

    # ── 公开接口 ──────────────────────────────────────────────────

    def _handle_requirement_flow(
            self, message: str, cid: str, chat_history: list,
            discuss_context: Optional[dict],
            pm_thread_id: str) -> Optional[dict]:
        """尝试用需求收集状态机处理消息；未命中返回 None。"""
        if discuss_context or pm_thread_id:
            return None
        if not hasattr(self, "_req_drafts"):
            self._req_drafts = {}
        req_reply = self._try_requirement_flow(message, cid, chat_history)
        if not req_reply:
            return None
        now = self._save_chat_turn(
            cid, message, req_reply, "requirement_flow")
        return {
            "reply": req_reply,
            "action_taken": "requirement_flow",
            "timestamp": now,
            "conv_id": cid,
        }

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

    def get_history(self, limit: int = 50, conv_id: str = "") -> list[dict]:
        """返回指定对话的最近消息。"""
        cid = conv_id or self._active_conv_id
        msgs = self._conversations.get(cid, [])
        return msgs[-limit:]

    def _build_discussion_context(self, discuss_context: dict) -> str:
        """构建优化讨论上下文, 供纯对话模式避免误执行命令。"""
        if not isinstance(discuss_context, dict):
            return ""
        optimizer = getattr(self.controller, "workstation_optimizer", None)
        if optimizer is None:
            return "【优化讨论上下文】优化模块暂不可用, 请基于 Boss 问题继续讨论。"
        topic = str(discuss_context.get("topic", "")).strip()
        try:
            if topic and topic != "__all__":
                item = optimizer.get_item(topic)
                if item:
                    return self._format_discussion_item(item)
                overview = self._format_discussion_overview(optimizer)
                return f"{overview}\n- 指定优化项 {topic} 不存在, 以上为总览。"
            return self._format_discussion_overview(optimizer)
        except Exception as exc:
            print(f"[Chat] 优化讨论上下文获取失败: {exc}", flush=True)
            return "【优化讨论上下文】优化信息暂不可用, 请基于已知内容讨论。"

    def _format_discussion_item(self, item: dict) -> str:
        """格式化单个优化讨论话题。"""
        return (
            "【优化讨论上下文】\n"
            f"- 标题: {item.get('title', '')}\n"
            f"- ID: {item.get('id', '')}\n"
            f"- 来源: {item.get('source', '')}\n"
            f"- 优先级: {item.get('priority', '')}\n"
            f"- 状态: {item.get('status', '')}\n"
            f"- 说明: {item.get('description', '')}\n"
            f"- Boss 补充: {item.get('decision_reply', '')}"
        )

    def _format_discussion_overview(self, optimizer) -> str:
        """格式化优化队列总览讨论话题。"""
        summary = optimizer.summary()
        items = optimizer.list_items(limit=100)
        interesting = [item for item in items if item.get("status") in (
            "waiting_boss", "candidate", "queued", "running")]
        lines = [
            "【优化讨论上下文】",
            f"- 守护状态: {'运行中' if summary.get('guardian_running') else '未运行'}",
            f"- 队列数量: {summary.get('queue_count', 0)}",
            f"- 待 Boss 决策: {summary.get('waiting_boss_count', 0)}",
        ]
        for item in interesting[:10]:
            lines.append(
                f"- {item.get('id', '')} [{item.get('status', '')}] "
                f"{item.get('title', '')}")
        return "\n".join(lines)

    def clear_history(self, conv_id: str = ""):
        """清空指定对话历史。"""
        cid = conv_id or self._active_conv_id
        self._conversations[cid] = []
        # 清空文件
        self._clear_conv_file(cid)

    # ── 多对话管理 API ─────────────────────────────────────────

    def list_conversations(self) -> list[dict]:
        """返回对话列表 (按 updated_at 降序)。"""
        return sorted(self._conv_index, key=lambda c: c.get("updated_at", 0), reverse=True)

    def create_conversation(self, title: str, project_id: str = "") -> dict:
        """新建对话, 返回对话元数据。"""
        cid = self._create_conversation(title, project_id)
        return self._get_conv_meta(cid)

    def delete_conversation(self, conv_id: str) -> bool:
        """删除对话。"""
        self._conv_index = [c for c in self._conv_index if c["id"] != conv_id]
        self._conversations.pop(conv_id, None)
        self._save_conv_index()
        self._delete_conv_file(conv_id)
        # 若删除的是活跃对话, 切换到第一个
        if self._active_conv_id == conv_id:
            self._active_conv_id = self._conv_index[0]["id"] if self._conv_index else ""
        return True

    def rename_conversation(self, conv_id: str, title: str) -> bool:
        """重命名对话。"""
        for c in self._conv_index:
            if c["id"] == conv_id:
                c["title"] = title
                self._save_conv_index()
                return True
        return False

    def get_messages(self, conv_id: str, limit: int = 100) -> list[dict]:
        """获取指定对话的消息 (懒加载)。"""
        if conv_id not in self._conversations:
            self._conversations[conv_id] = self._load_conversation_from_file(conv_id)
        return self._conversations.get(conv_id, [])[-limit:]

    def switch_conversation(self, conv_id: str) -> bool:
        """切换活跃对话。"""
        if any(c["id"] == conv_id for c in self._conv_index):
            self._active_conv_id = conv_id
            # 懒加载消息
            if conv_id not in self._conversations:
                self._conversations[conv_id] = self._load_conversation_from_file(conv_id)
            return True
        return False

    # ── 方案C: PM 线程管理 (L2 层) ───────────────────────

    def attach_pm_thread(self, conv_id: str, pm_id: str, task_name: str = "",
                         agent_name: str = "") -> dict:
        """将 PM Agent 绑定到对话的线程列表。

        当任务创建并分配 PM 后调用, 在对应项目对话中注册线程卡片。
        """
        meta = self._get_conv_meta(conv_id)
        if not meta:
            return {"ok": False, "message": "对话不存在"}

        threads = meta.setdefault("pm_threads", [])
        # 避免重复绑定
        if any(t["pm_id"] == pm_id for t in threads):
            return {"ok": True, "message": "PM 线程已存在", "pm_id": pm_id}

        thread_info = {
            "pm_id": pm_id,
            "task_name": task_name,
            "agent_name": agent_name or f"PM-{pm_id[:8]}",
            "status": "starting",
            "created_at": time.time(),
            "updated_at": time.time(),
        }
        threads.append(thread_info)
        self._pm_thread_messages.setdefault(pm_id, [])
        self._save_conv_index()
        self._save_thread_index(conv_id)
        logger.info("[方案C] PM %s 已绑定到对话 %s", pm_id[:8], conv_id)
        return {"ok": True, "pm_id": pm_id, "thread": thread_info}

    def detach_pm_thread(self, conv_id: str, pm_id: str) -> bool:
        """从对话中移除 PM 线程。"""
        meta = self._get_conv_meta(conv_id)
        if not meta:
            return False
        threads = meta.get("pm_threads", [])
        meta["pm_threads"] = [t for t in threads if t["pm_id"] != pm_id]
        self._save_conv_index()
        self._save_thread_index(conv_id)
        return True

    def update_pm_thread_status(self, pm_id: str, status: str, **kwargs):
        """更新 PM 线程状态 (在所有关联对话中查找)。"""
        for meta in self._conv_index:
            for t in meta.get("pm_threads", []):
                if t["pm_id"] == pm_id:
                    t["status"] = status
                    t["updated_at"] = time.time()
                    if kwargs.get("task_name"):
                        t["task_name"] = kwargs["task_name"]
                    self._save_conv_index()
                    self._save_thread_index(meta["id"])
                    return

    def get_pm_threads(self, conv_id: str) -> list[dict]:
        """获取对话关联的所有 PM 线程。"""
        meta = self._get_conv_meta(conv_id)
        if not meta:
            return []
        return meta.get("pm_threads", [])

    def find_conv_by_pm(self, pm_id: str) -> str:
        """根据 pm_id 反查所属对话 ID。"""
        for meta in self._conv_index:
            if any(t["pm_id"] == pm_id for t in meta.get("pm_threads", [])):
                return meta["id"]
        return ""

    def delete_pm_thread(self, pm_id: str):
        """彻底删除 PM 线程 (内存消息 + JSONL 文件), 任务删除时调用。"""
        self._pm_thread_messages.pop(pm_id, None)
        import os
        safe_id = pm_id.replace("/", "_").replace("\\", "_")
        filepath = os.path.join(self._get_thread_dir(), f"{safe_id}.jsonl")
        try:
            if os.path.isfile(filepath):
                os.remove(filepath)
        except Exception:
            pass

    # ── 方案C: PM 线程消息 (L2 层存储) ─────────────────────

    def send_to_pm_thread(self, conv_id: str, pm_id: str, message: str) -> dict:
        """L2 路由: 用户在 PM 线程内直接发消息给 PM Agent。

        跳过秘书 LLM, 直接注入到 PM 的 receive_input。
        """
        now = time.time()
        # 记录用户消息到线程
        user_msg = {"role": "user", "content": message, "timestamp": now, "layer": "L2"}
        self._pm_thread_messages.setdefault(pm_id, []).append(user_msg)
        self._save_thread_message_to_file(pm_id, user_msg)
        self._trim_pm_thread(pm_id)

        # 注入到 PM Agent
        result = self.controller.inject_input_to_pm(pm_id, {
            "response": message,
            "choice": message,
            "source": "pm_thread",  # 标记来源为线程直发
        })

        if result.get("ok"):
            # 记录系统确认消息
            ack_msg = {
                "role": "system",
                "content": f"✅ 已发送给 PM Agent",
                "timestamp": time.time(),
                "layer": "L2",
            }
            self._pm_thread_messages[pm_id].append(ack_msg)
            self._save_thread_message_to_file(pm_id, ack_msg)
            self._trim_pm_thread(pm_id)
            self.update_pm_thread_status(pm_id, "executing")
            return {"ok": True, "reply": ack_msg["content"], "timestamp": now, "pm_id": pm_id}

        return {"ok": False, "reply": f"❌ 发送失败: {result.get('message', '未知错误')}",
                "timestamp": now, "pm_id": pm_id}

    def get_pm_thread_messages(self, pm_id: str, limit: int = 100) -> list[dict]:
        """获取 PM 线程的历史消息 (懒加载)。"""
        if pm_id not in self._pm_thread_messages:
            self._pm_thread_messages[pm_id] = self._load_thread_messages_from_file(pm_id)
            self._trim_pm_thread(pm_id)
        return self._pm_thread_messages.get(pm_id, [])[-limit:]

    def append_pm_thread_message(self, pm_id: str, role: str, content: str, **kwargs):
        """向 PM 线程追加消息 (PM 回复/状态变更等)。"""
        msg = {
            "role": role,
            "content": content,
            "timestamp": time.time(),
            "layer": "L2",
            **kwargs,
        }
        self._pm_thread_messages.setdefault(pm_id, []).append(msg)
        self._save_thread_message_to_file(pm_id, msg)
        self._trim_pm_thread(pm_id)

    def _trim_pm_thread(self, pm_id: str):
        """裁剪 PM 线程消息 (内存 + 文件原子重写), 防止无限增长。"""
        msgs = self._pm_thread_messages.get(pm_id, [])
        if len(msgs) <= self._max_history * 2:
            return
        trimmed = msgs[-(self._max_history * 2):]
        self._pm_thread_messages[pm_id] = trimmed
        # 同步重写 JSONL 文件, 避免文件无限增长 (原子写防损坏)
        import json, os
        safe_id = pm_id.replace("/", "_").replace("\\", "_")
        filepath = os.path.join(self._get_thread_dir(), f"{safe_id}.jsonl")
        try:
            tmp = filepath + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                for m in trimmed:
                    f.write(json.dumps(m, ensure_ascii=False) + "\n")
            os.replace(tmp, filepath)
        except Exception:
            pass

    def notify_pm_clarification(self, pm_id: str, question: str, options: list = None):
        """方案C 双写: PM 请求决策时同时写入 L1 通知 + L2 线程消息。

        由 station_api 的 /api/pm/{pm_id}/status 端点在检测到 awaiting_input 时调用。
        """
        now = time.time()
        options = options or []

        # ── L2: 写入 PM 线程 ──
        clarification_msg = {
            "role": "pm",
            "content": question,
            "timestamp": now,
            "layer": "L2",
            "type": "clarification",
            "options": options,
        }
        self._pm_thread_messages.setdefault(pm_id, []).append(clarification_msg)
        self._save_thread_message_to_file(pm_id, clarification_msg)
        self._trim_pm_thread(pm_id)

        # ── L1: 写入主对话流 (作为系统通知卡片) ──
        conv_id = self.find_conv_by_pm(pm_id)
        if conv_id:
            # 获取 PM 名称
            agent_name = f"PM-{pm_id[:8]}"
            meta = self._get_conv_meta(conv_id)
            if meta:
                for t in meta.get("pm_threads", []):
                    if t["pm_id"] == pm_id:
                        agent_name = t.get("agent_name", agent_name)
                        break

            options_hint = ""
            if options:
                options_hint = "\n可选项: " + " | ".join(options)

            notify_content = (
                f"⚠️ **{agent_name} 等待您的决策**\n"
                f"问题: {question}{options_hint}\n"
                f"→ 展开 PM 线程直接回复, 或在此输入「告诉PM {pm_id[:12]} <您的决策>」"
            )
            notify_msg = {
                "role": "system",
                "content": notify_content,
                "timestamp": now,
                "layer": "L1",
                "type": "pm_clarification",
                "pm_id": pm_id,
                "options": options,
            }
            self._conversations.setdefault(conv_id, []).append(notify_msg)
            self._save_message_to_file(conv_id, notify_msg)
            self._touch_conv(conv_id)

        # 更新线程状态
        self.update_pm_thread_status(pm_id, "awaiting_input")
        logger.info("[方案C] PM %s 决策请求已双写 L1+L2", pm_id[:8])

    # ── 对话持久化 (共享文件夹) ─────────────────────────────────

    def _get_conv_dir(self):
        """获取对话存储目录。"""
        import os
        if self._shared_folder:
            base = self._shared_folder.path
        else:
            from pathlib import Path
            base = Path.home() / ".lan_mesh"
        conv_dir = os.path.join(str(base), "conversations")
        os.makedirs(conv_dir, exist_ok=True)
        return conv_dir

    def _load_conv_index(self):
        """从共享文件夹加载对话索引。"""
        import json, os
        idx_path = os.path.join(self._get_conv_dir(), "index.json")
        if os.path.isfile(idx_path):
            try:
                with open(idx_path, "r", encoding="utf-8") as f:
                    self._conv_index = json.load(f)
            except Exception:
                self._conv_index = []
        # 预加载所有对话消息
        for c in self._conv_index:
            cid = c["id"]
            if cid not in self._conversations:
                self._conversations[cid] = self._load_conversation_from_file(cid)

    def _save_conv_index(self):
        """原子写入对话索引到共享文件夹。"""
        import json, os
        idx_path = os.path.join(self._get_conv_dir(), "index.json")
        tmp_path = idx_path + ".tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(self._conv_index, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, idx_path)  # 原子替换
        except Exception:
            pass

    def _create_conversation(self, title: str, project_id: str = "") -> str:
        """内部创建对话, 返回 conv_id。"""
        import uuid
        cid = f"conv-{uuid.uuid4().hex[:10]}"
        now = time.time()
        meta = {
            "id": cid,
            "title": title,
            "project_id": project_id,
            "pm_threads": [],  # 方案C: 关联的 PM 线程列表
            "created_at": now,
            "updated_at": now,
        }
        self._conv_index.append(meta)
        self._conversations[cid] = []
        self._active_conv_id = cid
        self._save_conv_index()
        return cid

    def _get_conv_meta(self, conv_id: str) -> dict:
        for c in self._conv_index:
            if c["id"] == conv_id:
                return c
        return {}

    def _touch_conv(self, conv_id: str):
        """更新对话的 updated_at。"""
        for c in self._conv_index:
            if c["id"] == conv_id:
                c["updated_at"] = time.time()
                break
        self._save_conv_index()

    def _save_message_to_file(self, conv_id: str, msg: dict):
        """追加写入消息到 JSONL 文件。"""
        import json, os
        filepath = os.path.join(self._get_conv_dir(), f"{conv_id}.jsonl")
        try:
            with open(filepath, "a", encoding="utf-8") as f:
                f.write(json.dumps(msg, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def _load_conversation_from_file(self, conv_id: str) -> list[dict]:
        """从 JSONL 文件加载对话消息。"""
        import json, os
        filepath = os.path.join(self._get_conv_dir(), f"{conv_id}.jsonl")
        messages = []
        if os.path.isfile(filepath):
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            messages.append(json.loads(line))
            except Exception:
                pass
        return messages

    def _clear_conv_file(self, conv_id: str):
        """清空对话文件。"""
        import os
        filepath = os.path.join(self._get_conv_dir(), f"{conv_id}.jsonl")
        try:
            if os.path.isfile(filepath):
                open(filepath, "w").close()
        except Exception:
            pass

    def _delete_conv_file(self, conv_id: str):
        """删除对话文件。"""
        import os
        filepath = os.path.join(self._get_conv_dir(), f"{conv_id}.jsonl")
        try:
            if os.path.isfile(filepath):
                os.remove(filepath)
        except Exception:
            pass

    # ── 方案C: PM 线程持久化 ───────────────────────────────

    def _get_thread_dir(self):
        """获取 PM 线程存储目录。"""
        import os
        thread_dir = os.path.join(self._get_conv_dir(), "pm_threads")
        os.makedirs(thread_dir, exist_ok=True)
        return thread_dir

    def _save_thread_index(self, conv_id: str):
        """保存对话的 PM 线程索引到独立文件 (pm_threads/{conv_id}.json)。"""
        import json, os
        meta = self._get_conv_meta(conv_id)
        if not meta:
            return
        filepath = os.path.join(self._get_thread_dir(), f"{conv_id}.json")
        try:
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(meta.get("pm_threads", []), f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _save_thread_message_to_file(self, pm_id: str, msg: dict):
        """追加写入 PM 线程消息到 JSONL 文件。"""
        import json, os
        safe_id = pm_id.replace("/", "_").replace("\\", "_")
        filepath = os.path.join(self._get_thread_dir(), f"{safe_id}.jsonl")
        try:
            with open(filepath, "a", encoding="utf-8") as f:
                f.write(json.dumps(msg, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def _load_thread_messages_from_file(self, pm_id: str) -> list[dict]:
        """从 JSONL 文件加载 PM 线程消息。"""
        import json, os
        safe_id = pm_id.replace("/", "_").replace("\\", "_")
        filepath = os.path.join(self._get_thread_dir(), f"{safe_id}.jsonl")
        messages = []
        if os.path.isfile(filepath):
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            messages.append(json.loads(line))
            except Exception:
                pass
        return messages

    def _auto_title(self, conv_id: str, first_message: str):
        """O1: 根据首条消息自动生成对话标题 (5字内)。"""
        try:
            prompt = f"请用不超过8个字概括以下对话主题,直接输出标题,不要任何解释:\n{first_message[:100]}"
            resp = self.runtime._call_llm_with_routing(
                prompt,
                {"_model_preference": getattr(self.controller, '_default_model', ''),
                 "_fallback_models": [], "_system_prompt": "你是标题生成器,只输出标题文本。"}
            )
            title = resp.get("content", "").strip().strip('"').strip("'")[:20]
            if title:
                self.rename_conversation(conv_id, title)
        except Exception:
            pass  # 标题生成失败不影响主流程

    # ── 持久化辅助方法 ────────────────────────────────────────────

    def _load_history_from_db(self) -> list[dict]:
        """从 DB 加载聊天历史到内存。"""
        if not self._db:
            return []
        try:
            rows = self._db.get_chat_history(limit=self._max_history * 2)
            history = []
            for r in rows:
                entry = {
                    "role": r.get("role", "user"),
                    "content": r.get("content", ""),
                    "timestamp": r.get("timestamp", 0),
                }
                if r.get("action_taken"):
                    entry["action_taken"] = r["action_taken"]
                history.append(entry)
            logger.info("从 DB 加载 %d 条历史对话", len(history))
            return history
        except Exception as e:
            logger.error("加载 DB 聊天历史异常: %s", e)
            return []

    def _save_to_db(self, role: str, content: str, action_taken: str = "", timestamp: float = 0):
        """将单条聊天记录写入 DB。"""
        if not self._db:
            return
        try:
            self._db.save_chat_message(role, content, action_taken, timestamp)
        except Exception as e:
            logger.error("保存聊天记录异常: %s", e)

    # ── 内部方法 ──────────────────────────────────────────────────

    def _build_status_context(self) -> str:
        """从 controller.db 获取工作站状态, 拼接为文本摘要。"""
        lines = []

        try:
            # 主机状态
            hosts = self.controller.db.list_hosts()
            online_hosts = [h for h in hosts if h.online]
            lines.append(f"## 工作站状态")
            lines.append(f"- 在线主机: {len(online_hosts)}/{len(hosts)} 台")
            if online_hosts:
                host_names = [h.device_name or h.hostname or h.device_id[:8] for h in online_hosts[:5]]
                lines.append(f"- 在线主机名: {', '.join(host_names)}")
            # 主机评级分布 (S/A/B/C/D)
            if hosts:
                tiers = {}
                for h in hosts:
                    tier = getattr(h, 'rating_tier', '') or '?'
                    tiers[tier] = tiers.get(tier, 0) + 1
                tier_summary = ", ".join(f"{t}级:{c}台" for t, c in sorted(tiers.items()))
                if tier_summary:
                    lines.append(f"- 主机评级分布: {tier_summary}")

            # Secretary 状态
            secretary_status = "已激活" if self.controller.secretary_active else "未激活"
            lines.append(f"- Secretary: {secretary_status}")

            # 工作站常驻优化状态 (iter-72)
            try:
                optimizer = getattr(self.controller, "workstation_optimizer", None)
                if optimizer:
                    opt_summary = optimizer.summary()
                    lines.append(
                        f"- 工作站优化: 队列 {opt_summary['queue_count']} 个, "
                        f"待决策 {opt_summary['waiting_boss_count']} 个")
            except Exception:
                pass

            # PM Agent 状态
            try:
                pm_agents = self.controller.db.list_pm_agents()
                active_pms = [p for p in pm_agents if p.status in ("planning", "executing", "monitoring")]
                lines.append(f"- 活跃 PM Agent: {len(active_pms)} 个")
                if active_pms:
                    for pm in active_pms[:3]:
                        lines.append(f"  - {pm.agent_name} [{pm.status}] 任务: {pm.task_id}")
                # 优化7: 高亮等待决策的 PM
                awaiting_pms = [p for p in pm_agents if p.status == "awaiting_input"]
                if awaiting_pms:
                    # 记录最近的 awaiting PM (用于后续回复匹配)
                    self._last_awaiting_pm = awaiting_pms[-1].pm_id
                    lines.append(f"- ⚠️ 等待决策 PM: {len(awaiting_pms)} 个")
                    for pm in awaiting_pms[:2]:
                        lines.append(f"  - {pm.agent_name} 正在等待您的决策! (任务: {pm.task_id})")
                        lines.append(f"    请回复「告诉PM {pm.agent_name} <您的决策>」来回复")
                # 优化10: 高亮升级的 PM
                escalated_pms = [p for p in pm_agents if p.status == "escalated"]
                if escalated_pms:
                    self._last_awaiting_pm = escalated_pms[-1].pm_id
                    lines.append(f"- 🚨 升级告警 PM: {len(escalated_pms)} 个")
                    for pm in escalated_pms[:2]:
                        lines.append(f"  - {pm.agent_name} 遇到严重失败, 需要您的决策! (任务: {pm.task_id})")
                        lines.append(f"    可选: 忽略继续 / 简化重试 / 指定主机 / 放弃任务")
                        lines.append(f"    请回复「告诉PM {pm.agent_name} <您的选择>」")
            except Exception:
                lines.append("- PM Agent: 数据不可用")

            # 任务状态
            try:
                tasks = self.controller.db.list_tasks(limit=10)
                running_tasks = [t for t in tasks if t.status in ("running", "pending")]
                lines.append(f"- 进行中任务: {len(running_tasks)} 个")
                if running_tasks:
                    for t in running_tasks[:3]:
                        lines.append(f"  - {t.name} [{t.status}]")
            except Exception:
                lines.append("- 任务: 数据不可用")

            # 团队状态
            try:
                teams = self.controller.db.list_teams()
                active_teams = [t for t in teams if t.status in ("active", "pending")]
                lines.append(f"- Agent 团队: {len(active_teams)} 个")
            except Exception:
                pass

            # 任务记忆统计 (历史经验参考)
            try:
                stats = self.controller.db.get_task_memory_stats()
                if stats.get("total", 0) > 0:
                    rate = round(stats["success_rate"] * 100)
                    lines.append(f"- 历史任务记忆: {stats['total']} 条, 成功率 {rate}%")
            except Exception:
                pass

            # 技能库状态
            try:
                skills = self.controller.db.list_skills()
                if skills:
                    lines.append(f"- 技能库: {len(skills)} 个技能")
            except Exception:
                pass

        except Exception as e:
            lines.append(f"[状态获取异常: {e}]")

        return "\n".join(lines)

    def _build_system_prompt(self, status_context: str) -> str:
        """构建 LLM system prompt (M6: 统一从 role_cards 角色卡拼装)。

        包含秘书身份、能力边界、行为约束和实时工作站状态。
        此 prompt 通过 input_data['_system_prompt'] 注入 LLM 调用。
        """
        return render_secretary_prompt(status_context)

    def _build_prompt(self, message: str, history: list) -> str:
        """拼接对话历史 + 用户消息为 prompt。"""
        if not history:
            return message

        # 将历史对话格式化为文本
        lines = []
        for msg in history[-20:]:  # 最近 20 条
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if role == "user":
                lines.append(f"Boss: {content}")
            else:
                lines.append(f"秘书: {content}")

        lines.append(f"Boss: {message}")
        return "\n".join(lines)

    def _detect_action(self, message: str) -> str:
        """检测用户消息中的操作意图。

        Returns:
            操作类型字符串, 无意图则返回空字符串
        """
        msg_lower = message.lower()
        for keyword, action in _ACTION_KEYWORDS.items():
            if keyword in message or keyword in msg_lower:
                return action
        return ""

    def _detect_action_with_context(self, message: str,
                                    history: Optional[list] = None) -> str:
        """BUG-031: 关键词未命中时, 尝试从上一轮秘书建议继承意图。

        场景: Boss 多轮澄清需求后只回「是/可以/够了」, 字面无关键词,
        旧逻辑直接判定无意图 → 后台静默不执行。此处仅在同时满足
        「本轮是纯确认」且「上一轮秘书明确给出过带触发词的指令样例」
        时才继承, 避免把秘书解释性提到的词误当指令。
        """
        direct = self._detect_action(message)
        if direct:
            return direct
        if not self._is_confirmation(message):
            return ""
        suggested = self._last_suggested_action(history)
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
        msgs = history if history is not None else self._conversations.get(
            self._active_conv_id, [])
        for msg in reversed(msgs[-4:]):
            if msg.get("role") != "assistant":
                continue
            content = msg.get("content", "") or ""
            for seg in self._extract_quoted(content):
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
        if not self._looks_like_command(message):
            return ""
        prompt = self._build_classifier_prompt(message, history)
        model_pref, fallback_models = self._resolve_chat_model_pref(message)
        try:
            resp = self.runtime._call_llm_with_routing(prompt, {
                "_model_preference": model_pref,
                "_fallback_models": fallback_models,
                "_system_prompt": _CLASSIFIER_SYSTEM_PROMPT,
            })
        except Exception as exc:
            print(f"[Chat] LLM 意图分类调用失败, 回退无动作: {exc}",
                  flush=True)
            return ""
        action = self._parse_classifier_json(resp.get("content", ""))
        if action:
            print(f"[Chat] LLM 意图分类命中: {message[:20]} -> {action}",
                  flush=True)
        return action

    def _execute_action(self, action: str, message: str) -> str:
        """执行检测到的操作。

        Args:
            action: 操作类型
            message: 原始用户消息

        Returns:
            操作结果描述 (字符串)
        """
        try:
            if action == "query_status":
                return self._action_query_status()
            elif action == "query_hosts":
                return self._action_query_hosts()
            elif action == "query_tasks":
                return self._action_query_tasks()
            elif action == "query_progress":
                return self._action_query_progress()
            elif action == "activate_secretary":
                return self._action_activate_secretary()
            elif action == "deactivate_secretary":
                return self._action_deactivate_secretary()
            elif action == "submit_task":
                return self._action_submit_task(message)
            elif action == "cancel_task":
                return self._action_cancel_task(message)
            elif action == "pause_task":
                return self._action_pause_task(message)
            elif action == "respond_to_pm":
                return self._action_respond_to_pm(message)
            elif action == "accept_delivery":
                return self._action_accept_delivery(message)
            elif action == "reject_delivery":
                return self._action_reject_delivery(message)
            elif action == "create_project":
                return self._action_create_project(message)
            elif action == "workstation_optimization":
                return self._action_workstation_optimization(message)
            elif action == "edit_task_graph":
                return self._action_edit_task_graph(message)
            return ""
        except Exception as e:
            return f"操作执行失败: {e}"

    # ── 操作实现 ──────────────────────────────────────────────────

    def _parse_task_from_message(self, message: str) -> dict:
        """从用户消息中提取任务名称和描述 (LLM 解析)。"""
        extract_prompt = (
            "从以下用户消息中提取任务信息。返回严格 JSON 格式: "
            "{\"name\": \"简短任务名称(10字以内)\", \"description\": \"详细任务描述\"}。\n"
            "如果描述太模糊无法提取有效信息, 返回 {\"name\": \"\", \"description\": \"\"}。\n\n"
            f"消息: {message}"
        )
        resp = self.runtime._call_llm_with_routing(
            extract_prompt,
            {"_model_preference": "", "_fallback_models": []},
        )
        content = resp.get("content", "")
        try:
            # 提取 JSON 部分
            import json
            start = content.find("{")
            end = content.rfind("}") + 1
            if start >= 0 and end > start:
                return json.loads(content[start:end])
        except Exception:
            pass
        return {"name": message[:20], "description": message}

    def _action_submit_task(self, message: str) -> str:
        """从对话直接提交任务并分配 PM Agent (优化13: 支持优先级)。"""
        task_info = self._parse_task_from_message(message)
        name = task_info.get("name", "")
        description = task_info.get("description", "")

        if not name or len(description) < 5:
            return (
                "请提供更详细的任务信息, 例如:\n"
                "「提交一个代码审查任务: 检查 xxx 项目的安全漏洞」\n"
                "可选: 加「优先」或「紧急」提高优先级"
            )

        # 优化13: 从消息中推断优先级
        priority = "normal"
        msg_lower = message.lower()
        if any(kw in msg_lower for kw in ["紧急", "urgent", "立刻", "马上"]):
            priority = "urgent"
        elif any(kw in msg_lower for kw in ["优先", "重要", "high", "尽快"]):
            priority = "high"
        elif any(kw in msg_lower for kw in ["不急", "低优先", "low", "有空"]):
            priority = "low"

        result = self.controller.submit_task_from_chat(
            name=name,
            description=description,
            created_by="secretary",
            priority=priority,
        )

        # 优化14: 查询任务记忆, 提供历史参考
        memory_hint = self._get_task_memory_hint(name, description)

        status = result.get("status", "unknown")
        if status == "failed":
            error = result.get("output_data", {}).get("error", "未知错误")
            return f"任务创建失败: {error}"
        elif status == "running":
            pm_id = result.get("pm_agent_id", "")[:8]
            priority_label = {"low": "低", "normal": "普通", "high": "高", "urgent": "紧急"}.get(priority, priority)
            msg = (
                f"✅ 任务已创建并分配 PM Agent!\n"
                f"- 任务ID: {result.get('task_id', '')}\n"
                f"- 名称: {name}\n"
                f"- 优先级: {priority_label}\n"
                f"- PM Agent: {pm_id}\n"
                f"- 状态: 运行中"
            )
            if memory_hint:
                msg += f"\n\n💡 历史参考: {memory_hint}"
            return msg
        return f"任务已创建: {name} (状态: {status})"

    def _action_workstation_optimization(self, message: str) -> str:
        """Handle workstation optimization queries, submissions, and decisions."""
        manager = getattr(self.controller, "workstation_optimizer", None)
        if manager is None:
            return "工作站优化功能未初始化"
        try:
            decision = self._parse_optimization_decision(message)
            if decision:
                item = manager.decide(
                    decision["item_id"], decision["decision"], decision["reply"])
                return self._format_optimization_item(item, "决策已更新")
            if self._is_optimization_query(message):
                summary = manager.summary()
                return (
                    f"工作站优化状态:\n"
                    f"- 守护: {'运行中' if summary['guardian_running'] else '未运行'}\n"
                    f"- 队列: {summary['queue_count']}\n"
                    f"- 待决策: {summary['waiting_boss_count']}"
                )
            source = self._parse_optimization_source(message)
            title, description = self._parse_optimization_text(message)
            priority = self._parse_optimization_priority(message)
            item = manager.submit(source, title, description, priority)
            return self._format_optimization_item(item, "优化项已创建")
        except Exception as exc:
            return f"工作站优化操作失败: {exc}"

    def _parse_optimization_source(self, message: str) -> str:
        """Infer optimization source from natural language."""
        if "瓶颈" in message or "限制" in message:
            return "bottleneck"
        if "agent建议" in message or "任务建议" in message or "自己思考" in message:
            return "agent"
        return "boss"

    def _parse_optimization_text(self, message: str) -> tuple[str, str]:
        """Extract a compact title and full description."""
        text = message.strip()
        for prefix in ("优化工作站:", "优化工作站：", "工作站优化:", "工作站优化：",
                       "遇到瓶颈:", "遇到瓶颈：", "添加优化建议:", "添加优化建议："):
            if text.startswith(prefix):
                text = text[len(prefix):].strip()
                break
        if ":" in text or "：" in text:
            parts = re.split(r"[:：]", text, maxsplit=1)
            text = parts[1].strip() if len(parts) == 2 and parts[1] else text
        title = text[:40] if len(text) <= 40 else text[:37] + "…"
        return title, text

    def _parse_optimization_priority(self, message: str) -> str:
        """Infer optimization priority from natural language."""
        lowered = message.lower()
        if "紧急" in message or "urgent" in lowered:
            return "urgent"
        if "优先" in message or "重要" in message or "high" in lowered:
            return "high"
        if "不急" in message or "低优先" in message or "low" in lowered:
            return "low"
        return "normal"

    def _parse_optimization_decision(self, message: str) -> dict:
        """Parse approve/reject/clarify commands with an optimization item id."""
        match = re.search(r"(opt-[A-Za-z0-9]+)", message)
        if not match:
            return {}
        if "确认优化" in message or "批准优化" in message:
            return {"item_id": match.group(1), "decision": "approve", "reply": ""}
        if "拒绝优化" in message:
            return {"item_id": match.group(1), "decision": "reject", "reply": ""}
        if "补充说明优化" in message:
            reply = re.sub(r"^.*?(?:补充说明优化[:：])?", "", message).strip()
            return {"item_id": match.group(1), "decision": "clarify", "reply": reply}
        return {}

    def _is_optimization_query(self, message: str) -> bool:
        """Detect read-only workstation optimization status requests."""
        if any(marker in message for marker in ("创建", "添加", "建议:", "建议：")):
            return False
        return any(marker in message for marker in ("状态", "队列", "列表", "多少"))

    def _format_optimization_item(self, item: dict, headline: str) -> str:
        """Format one optimization item for the secretary chat reply."""
        status_label = {
            "candidate": "待 Boss 评估", "waiting_boss": "待 Boss 决策",
            "queued": "已排队", "running": "执行中", "completed": "已完成",
            "rejected": "已拒绝", "failed": "失败",
        }.get(item.get("status", ""), item.get("status", ""))
        return (
            f"🛠️ {headline}\n"
            f"- ID: {item.get('id', '')}\n"
            f"- 标题: {item.get('title', '')}\n"
            f"- 来源: {item.get('source', '')}\n"
            f"- 优先级: {item.get('priority', '')}\n"
            f"- 状态: {status_label}"
        )

    def _action_create_project(self, message: str) -> str:
        """从对话创建项目 (BUG-026: 秘书幻觉修复配套)。"""
        pm = getattr(self.controller, "project_manager", None)
        if pm is None:
            return "项目创建失败: Secretary 未激活或项目管理器不可用"

        # 复用 LLM 解析提取项目名称和描述
        info = self._parse_task_from_message(message)
        name = info.get("name", "")
        description = info.get("description", "")

        if not name or len(description) < 5:
            return (
                "请提供更详细的项目信息, 例如:\n"
                "「创建一个项目: 股票自动交易系统, 预算10美元」"
            )

        try:
            project = pm.create_project(
                name=name,
                description=description,
                budget_limit_usd=10.0,
                allowed_models=[],
                routing_strategy="balanced",
                workspace_base="",
            )
            return (
                f"✅ 项目已创建!\n"
                f"- 项目ID: {project.project_id}\n"
                f"- 名称: {name}\n"
                f"- 描述: {description[:80]}"
            )
        except Exception as e:
            return f"项目创建失败: {e}"

    def _action_edit_task_graph(self, message: str) -> str:
        """自然语言 DAG 编辑 (F4.3, iter-51): Boss 口述修改任务图结构。

        支持指令: 加一步/新增步骤 X / 删除(跳过)步骤 X / 加依赖 X→Y /
        删依赖 X→Y; 任务用 task-xxx ID 或名称定位。
        流程: 定位任务 → 读图 → LLM 解析编辑指令 → 应用 (环检测回滚) → 落盘。
        防幻觉: 真实执行编辑并返回落盘结果, 失败时明确报错不虚报。
        """
        import json as _json
        import re as _re
        import uuid as _uuid
        from .protocol import SubTask
        from .task import TaskDAG

        # 1. 定位任务 (task-xxx ID 优先, 其次任务名匹配)
        task = None
        m = _re.search(r"task-[0-9a-fA-F]{6,}", message)
        if m:
            task = self.controller.db.get_task(m.group(0))
        if not task:
            for t in self.controller.db.list_tasks(limit=50):
                if t.name and t.name in message:
                    task = t
                    break
        if not task:
            return "未找到目标任务, 请在指令中带上任务 ID (task-xxx) 或任务名称"

        # 2. 读取当前图
        graph = self.controller.get_task_graph_data(task.task_id)
        if not graph:
            return f"任务 '{task.name}' 暂无 DAG 图数据, 无法编辑"

        # 3. LLM 解析编辑指令
        edit = self._parse_graph_edit(message, graph)
        if not edit or not edit.get("op"):
            return ("无法解析图编辑指令, 请使用:\n"
                    "- 加一步: 步骤名\n- 删除步骤 步骤名\n"
                    "- 加依赖: 步骤A → 步骤B")

        # 4. 应用编辑 (TaskDAG 自带环检测回滚)
        try:
            dag = TaskDAG.from_graph_json(graph)
            op = edit.get("op", "")
            if op == "add_node":
                name = (edit.get("node_name") or "").strip()
                if not name:
                    return "新增步骤需要名称, 例如: 加一步: 发布验收"
                st = SubTask(
                    subtask_id=f"st-{_uuid.uuid4().hex[:10]}",
                    parent_task_id=task.task_id,
                    name=name,
                    description=edit.get("description") or name,
                )
                if not dag.add_node(st):
                    return f"新增步骤 '{name}' 失败 (节点冲突或产生环)"
            elif op == "remove_node":
                sid = _find_node_by_name(dag, edit.get("node_name", ""))
                if not sid:
                    return f"未找到步骤 '{edit.get('node_name', '')}'"
                dag.remove_node(sid)
            elif op == "add_edge":
                src = _find_node_by_name(dag, edit.get("source", ""))
                tgt = _find_node_by_name(dag, edit.get("target", ""))
                if not src or not tgt:
                    return "加依赖需要存在的前置/后置步骤名, 例如: 加依赖: A → B"
                if not dag.add_edge(src, tgt):
                    return f"加依赖 '{src} → {tgt}' 失败 (重复边或产生环)"
            elif op == "remove_edge":
                src = _find_node_by_name(dag, edit.get("source", ""))
                tgt = _find_node_by_name(dag, edit.get("target", ""))
                if not src or not tgt:
                    return "删依赖需要存在的前置/后置步骤名"
                if not dag.remove_edge(src, tgt):
                    return f"未找到依赖边 '{src} → {tgt}'"
            else:
                return f"不支持的编辑操作: {op}"
        except Exception as e:
            return f"图编辑应用失败: {e}"

        # 5. 落盘并返回真实结果
        result = self.controller.update_task_graph(
            task.task_id, dag.to_graph_json())
        if result.get("ok"):
            return (f"✅ 图编辑完成: {result.get('message')}\n"
                    f"任务 '{task.name}' ({task.task_id}) 已更新")
        return f"图编辑保存失败: {result.get('message', '未知错误')}"

    def _parse_graph_edit(self, message: str, graph: dict) -> dict:
        """LLM 解析自然语言图编辑指令为结构化操作 (失败返回空 dict)。"""
        import json as _json
        node_names = [n.get("name", "") for n in graph.get("nodes", [])]
        prompt = (
            "解析用户的 DAG 图编辑指令, 返回严格 JSON: "
            "{\"op\": \"add_node|remove_node|add_edge|remove_edge\", "
            "\"node_name\": \"步骤名\", \"source\": \"前置步骤名\", "
            "\"target\": \"后置步骤名\", \"description\": \"步骤描述\"}。\n"
            "op 判定: 加/新增/增加步骤 → add_node; 删除/跳过/去掉步骤 → remove_node; "
            "加依赖/先做A再做B → add_edge; 删依赖/解除依赖 → remove_edge。\n"
            f"现有步骤: {node_names}\n"
            "无法解析返回 {\"op\": \"\"}。\n\n"
            f"用户指令: {message}"
        )
        resp = self.runtime._call_llm_with_routing(
            prompt,
            {"_model_preference": "", "_fallback_models": []},
        )
        content = resp.get("content", "")
        try:
            start = content.find("{")
            end = content.rfind("}") + 1
            if start >= 0 and end > start:
                return _json.loads(content[start:end])
        except Exception:
            pass
        return {}

    def _action_query_status(self) -> str:
        """查询综合状态。"""
        context = self._build_status_context()
        return context

    def _action_query_hosts(self) -> str:
        """查询主机列表。"""
        hosts = self.controller.db.list_hosts()
        if not hosts:
            return "暂无注册主机"
        lines = []
        for h in hosts:
            status = "在线" if h.online else "离线"
            name = h.device_name or h.hostname or h.device_id[:8]
            lines.append(f"  {status} | {name} | {h.ip or '-'} | 评级: {h.rating or '-'}")
        return f"主机列表 ({len(hosts)} 台):\n" + "\n".join(lines)

    def _action_query_tasks(self) -> str:
        """查询任务列表。"""
        if not self.controller.secretary_active:
            return "Secretary 未激活, 无法查询任务"
        tasks = self.controller.db.list_tasks(limit=10)
        if not tasks:
            return "暂无任务"
        lines = []
        for t in tasks:
            pm_info = f" PM: {t.pm_agent_id[:8]}" if t.pm_agent_id else ""
            lines.append(f"  [{t.status}] {t.name}{pm_info}")
        return f"最近任务 ({len(tasks)} 个):\n" + "\n".join(lines)

    def _action_query_progress(self) -> str:
        """查询 PM Agent 进度 (优化11: LLM 智能摘要)。"""
        if not self.controller.secretary_active:
            return "Secretary 未激活"
        try:
            pms = self.controller.db.list_pm_agents()
            if not pms:
                return "暂无 PM Agent"

            # 收集原始数据
            raw_lines = []
            for pm in pms:
                reports = self.controller.db.get_progress_reports(pm.pm_id, limit=3)
                latest_progress = reports[0]["progress"] if reports else 0.0
                latest_msg = reports[0]["message"] if reports else ""
                raw_lines.append(
                    f"- {pm.agent_name} | 状态: {pm.status} | "
                    f"进度: {latest_progress*100:.0f}% | "
                    f"模式: {pm.collaboration_mode or '-'} | "
                    f"最新消息: {latest_msg[:100]}"
                )

            raw_text = "\n".join(raw_lines)

            # 优化11: 用 LLM 生成自然语言摘要
            summary_prompt = (
                "你是 LAN Mesh 工作站的秘书。请用简洁的中文 (2-3句话) 向 Boss 汇报以下 PM Agent 进度。\n"
                "要求: 突出整体进展、异常情况和预计剩余时间。不要逐条列出, 用自然段落。\n\n"
                f"PM Agent 数据:\n{raw_text}"
            )
            try:
                resp = self.runtime._call_llm_with_routing(
                    summary_prompt,
                    {"_model_preference": "", "_fallback_models": []},
                )
                summary = resp.get("content", "").strip()
                if summary and len(summary) > 10:
                    return f"📊 进度汇报:\n{summary}\n\n--- 原始数据 ---\n{raw_text}"
            except Exception:
                pass

            # LLM 失败时回退到原始格式
            return f"PM Agent 进度 ({len(pms)} 个):\n{raw_text}"
        except Exception as e:
            return f"进度查询失败: {e}"

    def _action_activate_secretary(self) -> str:
        """激活 Secretary。"""
        if self.controller.secretary_active:
            return "Secretary 已处于激活状态"
        result = self.controller.activate_secretary()
        return result.get("message", "Secretary 激活结果未知")

    def _action_deactivate_secretary(self) -> str:
        """停用 Secretary。"""
        if not self.controller.secretary_active:
            return "Secretary 当前未激活"
        result = self.controller.deactivate_secretary()
        return result.get("message", "Secretary 停用结果未知")

    # ── 优化8: 取消/暂停任务操作 ──

    def _action_cancel_task(self, message: str) -> str:
        """取消指定任务。"""
        task_id = self._extract_task_id_from_message(message)
        if not task_id:
            # 尝试从消息中找任务名称
            tasks = self.controller.db.list_tasks(status="running", limit=10)
            if not tasks:
                return "当前没有运行中的任务可以取消"
            # 模糊匹配任务名
            for t in tasks:
                if t.name and t.name in message:
                    task_id = t.task_id
                    break
            if not task_id:
                task_list = "\n".join([f"  - [{t.status}] {t.name} (ID: {t.task_id[:12]})" for t in tasks[:5]])
                return f"请指定要取消的任务。当前运行中的任务:\n{task_list}\n\n示例: 取消任务 {tasks[0].name if tasks else 'XXX'}"

        result = self.controller.cancel_task(task_id)
        if result.get("ok"):
            return f"✅ {result.get('message', '任务已取消')}"
        return f"❌ 取消失败: {result.get('message', '未知错误')}"

    def _action_pause_task(self, message: str) -> str:
        """暂停指定任务。"""
        task_id = self._extract_task_id_from_message(message)
        if not task_id:
            tasks = self.controller.db.list_tasks(status="running", limit=10)
            if not tasks:
                return "当前没有运行中的任务可以暂停"
            for t in tasks:
                if t.name and t.name in message:
                    task_id = t.task_id
                    break
            if not task_id:
                task_list = "\n".join([f"  - [{t.status}] {t.name} (ID: {t.task_id[:12]})" for t in tasks[:5]])
                return f"请指定要暂停的任务。当前运行中的任务:\n{task_list}\n\n示例: 暂停任务 {tasks[0].name if tasks else 'XXX'}"

        result = self.controller.pause_task(task_id)
        if result.get("ok"):
            return f"⏸️ {result.get('message', '任务已暂停')}"
        return f"❌ 暂停失败: {result.get('message', '未知错误')}"

    def _extract_task_id_from_message(self, message: str) -> str:
        """从消息中提取 task_id (格式: task-xxxx)。"""
        import re
        match = re.search(r'task-[a-f0-9]{12}', message, re.IGNORECASE)
        return match.group(0) if match else ""

    # ── 优化7: 回复 PM 决策 ──

    def _action_respond_to_pm(self, message: str) -> str:
        """向等待决策的 PM Agent 回复 Boss 的决策。

        支持两种方式:
        1. 「告诉PM PM-abc 用方案A」→ 精确指定 PM
        2. 如果只有一个 PM 在等待决策, 可直接用「用方案A」
        """
        pm_id = self._extract_pm_id_from_message(message)

        # 如果没有明确指定 PM, 使用最近的 awaiting PM
        if not pm_id:
            # 刷新 _last_awaiting_pm
            try:
                pm_agents = self.controller.db.list_pm_agents()
                awaiting = [p for p in pm_agents if p.status == "awaiting_input"]
                if awaiting:
                    pm_id = awaiting[-1].pm_id
                    self._last_awaiting_pm = pm_id
            except Exception:
                pass

        if not pm_id:
            pm_id = self._last_awaiting_pm

        if not pm_id:
            return "当前没有 PM Agent 在等待您的决策。"

        # 提取回复内容 (去掉"告诉PM xxx"前缀)
        response_text = message
        for prefix in ["告诉PM", "告知PM", "回复PM"]:
            if prefix in message:
                # 找到前缀后的内容
                idx = message.index(prefix) + len(prefix)
                # 跳过 PM ID 部分
                after_prefix = message[idx:].strip()
                # 如果以 PM ID 开头, 去掉它
                if after_prefix.startswith("PM-") or after_prefix.startswith("pm-"):
                    parts = after_prefix.split(None, 1)
                    response_text = parts[1] if len(parts) > 1 else ""
                else:
                    response_text = after_prefix
                break

        if not response_text.strip():
            return "请提供您的决策内容。示例: 告诉PM PM-abc 用方案A"

        # 获取 PM 信息
        pm = self.controller.db.get_pm_agent(pm_id)
        if not pm:
            return f"PM Agent {pm_id[:12]} 不存在"

        result = self.controller.inject_input_to_pm(pm_id, {
            "response": response_text,
            "choice": response_text,
        })

        if result.get("ok"):
            return f"✅ 已将您的决策发送给 {pm.agent_name}: {response_text[:100]}"
        return f"❌ 发送失败: {result.get('message', '未知错误')}"

    def _extract_pm_id_from_message(self, message: str) -> str:
        """从消息中提取 pm_id (格式: pm-xxxx 或 PM-xxxx)。"""
        import re
        match = re.search(r'[Pp][Mm]-[a-f0-9]{12}', message)
        return match.group(0) if match else ""

    # ── 优化9: 验收/退回交付物 ──

    def _action_accept_delivery(self, message: str) -> str:
        """验收最近交付的任务。"""
        task = self._find_latest_delivered_task()
        if not task:
            return "当前没有待验收的任务交付物。"

        task_id = task.task_id
        delivery = task.output_data.get("_delivery", {})
        delivery["accepted"] = True
        delivery["accepted_at"] = time.time()
        task.output_data["_delivery"] = delivery
        task.status = "completed"
        self.controller.db.save_task(task)

        return (
            f"✅ 已验收任务「{task.name}」的交付物。\n"
            f"任务状态已更新为 completed。"
        )

    def _action_reject_delivery(self, message: str) -> str:
        """退回最近交付的任务, 附带修改意见。"""
        task = self._find_latest_delivered_task()
        if not task:
            return "当前没有待退回的任务交付物。"

        task_id = task.task_id
        delivery = task.output_data.get("_delivery", {})
        delivery["accepted"] = False
        delivery["rejected_at"] = time.time()

        # 提取退回原因
        reason = message
        for kw in ["退回", "重新做"]:
            if kw in message:
                idx = message.index(kw) + len(kw)
                reason = message[idx:].strip()
                break
        if not reason:
            reason = "Boss 未提供具体修改意见"

        delivery["reject_reason"] = reason
        task.output_data["_delivery"] = delivery
        task.status = "running"  # 重新运行
        self.controller.db.save_task(task)

        # 通知 PM Agent (如果还在运行)
        pm_id = task.pm_agent_id
        if pm_id:
            self.controller.inject_input_to_pm(pm_id, {
                "response": f"[退回] {reason}",
                "choice": "reject",
                "task_name": task.name,
            })

        return (
            f"↩️ 已退回任务「{task.name}」的交付物。\n"
            f"退回原因: {reason}\n"
            f"PM Agent 将收到修改意见并重新执行。"
        )

    def _find_latest_delivered_task(self):
        """查找最近的已交付但未验收的任务。"""
        try:
            tasks = self.controller.db.list_tasks(limit=20)
            for t in tasks:
                if t.output_data and "_delivery" in t.output_data:
                    delivery = t.output_data["_delivery"]
                    if delivery.get("accepted") is None:  # 未验收
                        return t
        except Exception:
            pass
        return None

    # ── 优化14: 任务记忆辅助 ──

    def _get_task_memory_hint(self, task_name: str, task_desc: str) -> str:
        """优化14: 查询任务记忆, 生成历史参考提示。

        当 Boss 提交新任务时, 查询同类型任务的历史记录:
        - 成功率和平均耗时
        - 推荐的协作模式
        - 常见错误预警
        """
        try:
            db = self.controller.db
            # 推断任务类型 (复用 PM Agent 的逻辑)
            from .pm_agent import ProjectManagerAgent
            task_type = ProjectManagerAgent._infer_task_type(task_name, task_desc)

            stats = db.get_task_memory_stats(task_type=task_type)
            if stats.get("total", 0) == 0:
                return ""

            hints = []
            hints.append(f"同类任务历史 {stats['total']} 次, 成功率 {stats['success_rate']*100:.0f}%")
            if stats.get("avg_duration", 0) > 0:
                mins = stats["avg_duration"] / 60
                hints.append(f"平均耗时 {mins:.1f} 分钟")
            if stats.get("recommended_mode"):
                hints.append(f"推荐模式: {stats['recommended_mode']}")
            if stats.get("common_errors"):
                top_error = stats["common_errors"][0][0]
                hints.append(f"注意: 历史常见错误「{top_error[:50]}」")

            return "; ".join(hints)
        except Exception:
            return ""
