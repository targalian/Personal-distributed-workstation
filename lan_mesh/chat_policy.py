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

# BUG-035 / BUG-036: 非指令语气护栏 — 命中则不触发**变更类**动作。
#
# 关键词表是朴素子串匹配, 天然会把「谈论某动作」误判成「执行某动作」:
#   - 疑问 (BUG-035): 「验收标准有哪些」命中「验收」→ 真实执行了验收
#   - 否定 (BUG-036): 「先不要创建项目」命中「创建项目」→ 明确说不要却做了
#   - 陈述 (BUG-036): 「上次取消任务导致数据不一致」→ 复述历史被当成新指令
# 实测 21 句非指令样本中 15 句误触发, 否定句尤其危险 (语义完全相反)。
_QUESTION_MARKERS = (
    "是什么", "有哪些", "是啥", "哪些", "什么时候", "为什么", "怎么",
    "如何", "多少", "是否", "能否", "可否", "吗", "呢", "?", "？",
    "介绍", "说明一下", "讲讲", "解释", "看看", "了解",
    "标准", "定义", "口径", "区别", "有什么不同",
    # 反复问句与征询式 (「会不会丢数据」「是不是更稳妥」)
    "会不会", "是不是", "要不要", "能不能", "有没有", "行不行",
    # 疑问代词
    "谁", "哪个", "哪里", "何时",
)

# 否定 / 推迟表达: 语义与执行相反, 必须拦截
_NEGATION_MARKERS = (
    "不要", "不用", "不需要", "不必", "没必要", "不想", "别急", "别再",
    "先不", "暂时不", "还没", "尚未", "不是要", "不做", "无需",
    "别", "勿",
)

# 议论 / 提议 / 评价: 在讨论「该不该做」, 尚未下达执行指令
_DELIBERATION_MARKERS = (
    "我们要做个决定", "做个决定", "应该", "是不是该", "建议", "考虑",
    "打算", "计划中", "讨论", "商量", "评估一下", "更稳妥", "更好",
)

# 复述 / 陈述过去事件: 讨论已发生的事, 不是新指令
_RETROSPECT_MARKERS = (
    "刚才", "上次", "上轮", "之前", "已经", "曾经", "那次", "这次不",
    "导致", "结果是",
)

# 明确的执行祈使信号 — 优先级最高, 即使句中带疑问词也按指令处理
# (如「帮我验收一下任务A好吗」)
_IMPERATIVE_MARKERS = (
    "帮我", "请", "麻烦", "立即", "马上", "现在就", "去做", "执行",
)

# 只读查询动作: 无副作用, 不受非指令语气护栏约束。
# 「查看任务列表」「进度怎么样」这类即便是疑问句也应照常响应 ——
# 护栏的目的是防止**误改状态**, 不是让查询失灵。
_READONLY_ACTIONS = frozenset({
    "query_status", "query_progress", "query_hosts", "query_tasks",
})


def _looks_like_question(message: str) -> bool:
    """判断是否为询问句 (询问不应触发变更类动作)。

    祈使信号优先: 命中则视为指令 (「帮我验收一下」仍应执行)。
    """
    text = (message or "").strip()
    if not text:
        return False
    if any(m in text for m in _IMPERATIVE_MARKERS):
        return False
    return any(m in text for m in _QUESTION_MARKERS)


def _looks_like_non_directive(message: str) -> bool:
    """BUG-036: 判断是否为「谈论动作」而非「下达指令」。

    覆盖三类: 疑问 / 否定或推迟 / 复述过去。否定与复述**不受祈使信号豁免**——
    「请先不要创建项目」既有祈使也有否定, 语义是「不要做」, 必须拦住。
    """
    text = (message or "").strip()
    if not text:
        return False
    if any(m in text for m in _NEGATION_MARKERS):
        return True
    if any(m in text for m in _RETROSPECT_MARKERS):
        return True
    if any(m in text for m in _DELIBERATION_MARKERS):
        return True
    return _looks_like_question(text)


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


# BUG-037: Windows 本地路径提取 — 必须同时接受反斜杠与正斜杠。
# 原正则只写 [A-Za-z]:\\ , 而 Boss 蓝图/消息里惯用正斜杠
# (实测「本地 E:/ingobj/stock_player」两条正则全部 miss), 导致
# project_path 恒缺失, 子任务模板回退字面量 "." 在错误目录作业。
_PATH_BODY = r"[A-Za-z]:[\\/][^\s，。；;\"'）)]+"
_PATH_LABELED = re.compile(
    r"(?:本地地址|本地路径|项目路径|仓库路径|代码路径|工程路径)"
    r"\s*(?:为|是|在)?[:：]?\s*[\"']?(" + _PATH_BODY + r")")
_PATH_BARE = re.compile("(" + _PATH_BODY + ")")


def _extract_local_path(text: str) -> str:
    """从自由文本提取本机项目路径; 优先带标签的写法, 其次裸路径。"""
    if not text:
        return ""
    for pattern in (_PATH_LABELED, _PATH_BARE):
        match = pattern.search(text)
        if match:
            return match.group(1).rstrip("\\/.,;，。\"'")
    return ""


