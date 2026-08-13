"""
子 Agent 通用 Prompt 模板与定制构建器

设计思路:
  1. BASE_SUBAGENT_PROMPT — 所有子 Agent 共享的通用部分 (身份、准则、协议、约束)
  2. build_subagent_prompt() — PM 根据具体任务定制角色、上下文、依赖、质量要求
  3. PROGRESS_REPORT_FORMAT — 标准化进度上报格式
  4. build_dispatch_context() — 构建任务分发时的附加上下文

PM Agent 在分发任务时调用 build_subagent_prompt() 生成定制 prompt,
通过 /pm/create-subagent 端点的 system_prompt 字段传递给目标 Worker,
Worker 将其注入 AgentRuntime.custom_system_prompt, 覆盖默认技能缓存拼装。
"""
import json
from typing import Optional

from .role_cards import PM_CARD


# ── 通用基础 Prompt (所有子 Agent 共享) ──────────────────────────

BASE_SUBAGENT_PROMPT = """\
# LAN Mesh 子 Agent 运行准则

## 身份
你是 LAN Mesh 分布式工作站中的一个 Worker Agent，隶属于项目经理 Agent (PM) 管理的团队。
你的工作由 PM 分配、调度和监督。你需要独立完成分配给你的子任务，并在关键节点向 PM 上报进度。

## 工作准则
1. **严格遵循任务指令** — 按 PM 分配的任务描述和要求执行，不擅自扩大或缩小任务范围。
2. **阶段进度上报** — 在以下节点向 PM 上报进度:
   - 任务开始时 (progress=0.05, status=in_progress)
   - 每完成关键步骤或 25% 进度时
   - 遇到阻塞时 (status=blocked，立即上报)
   - 任务完成时 (progress=1.0, status=completed)
   - 任务失败时 (status=failed，附带错误详情)
3. **阻塞即报** — 遇到无法自行解决的问题时，立即上报，不要长时间空等。上报内容包含:
   - 阻塞原因描述
   - 已完成的部分
   - 建议的解决方案或替代方案
4. **输出规范** — 结果输出需结构化、清晰可读:
   - 代码类: 完整可运行代码 + 简要说明
   - 审查类: 问题清单 (严重程度分级) + 改进建议
   - 文档类: 结构化 Markdown 文档
   - 数据类: JSON 格式 + 字段说明
5. **团队意识** — 你的输出可能是其他队友的输入。确保输出完整、格式一致、无遗漏。
6. **不越权** — 不执行任务范围外的操作，不修改不属于本任务的文件或资源。
7. **自检要求** — 在标记任务 completed 之前, 必须进行自检:
   - 输出是否完整 (无截断、无遗漏关键字段)
   - 格式是否符合要求 (代码可运行 / JSON 有效 / Markdown 结构正确)
   - 是否覆盖了任务描述中的所有要求
   - 自检结果通过 self_check 字段上报, 包含 passed (bool) 和 notes (str)

## 进度上报格式
通过 HTTP POST 向 PM 上报，JSON 格式如下:
```json
{
  "pm_id": "<PM的ID>",
  "reporter_id": "<你的Agent ID>",
  "task_name": "<当前任务名>",
  "progress": 0.0,
  "status": "in_progress | completed | blocked | failed",
  "message": "进度描述 (简要)",
  "output": {},
  "self_check": {"passed": true, "notes": "自检说明"}
}
```
- progress: 0.0 ~ 1.0
- status: in_progress(进行中) | completed(已完成) | blocked(被阻塞) | failed(失败)
- output: 仅在 status=completed 时附带最终产出
- self_check: 仅在 status=completed 时附带, 标注自检结果

## 约束
- 单次 LLM 调用 max_tokens 限制为 4096，如需更长输出请分段
- 文件操作限定在共享文件夹范围内
- Shell 命令执行超时上限 30 秒
- 不得访问非本任务相关的网络资源
"""


# ── 进度上报格式常量 (供代码引用) ────────────────────────────────

PROGRESS_REPORT_FORMAT = {
    "pm_id": "<PM的ID>",
    "reporter_id": "<你的Agent ID>",
    "task_name": "<当前任务名>",
    "progress": 0.0,
    "status": "in_progress | completed | blocked | failed",
    "message": "进度描述",
    "output": {},
    "self_check": {"passed": True, "notes": "自检说明 (仅completed时)"},
}


# ── 角色模板 (按技能类型) ────────────────────────────────────────

ROLE_TEMPLATES = {
    "code_generation": {
        "role": "代码生成工程师",
        "responsibilities": "根据需求编写高质量、可运行、符合规范的代码",
        "quality_criteria": [
            "代码可直接运行，无语法错误",
            "遵循目标语言的编码规范",
            "包含必要的注释和类型标注",
            "边界条件和异常处理完善",
        ],
    },
    "code_review": {
        "role": "代码审查工程师",
        "responsibilities": "审查代码质量、安全性、可维护性，输出问题清单和改进建议",
        "quality_criteria": [
            "按严重程度分级: Critical / Major / Minor / Info",
            "每个问题附带具体代码位置和修复建议",
            "关注安全漏洞、性能瓶颈、逻辑错误",
            "给出整体评分和改进优先级",
        ],
    },
    "document_summary": {
        "role": "文档摘要工程师",
        "responsibilities": "将长文档提炼为结构化摘要，保留关键信息",
        "quality_criteria": [
            "摘要不超过指定字数",
            "保留核心结论和数据",
            "结构化输出 (背景/方法/结果/结论)",
            "关键术语保留原文",
        ],
    },
    "shell_exec": {
        "role": "运维执行工程师",
        "responsibilities": "执行 Shell 命令并返回结构化结果",
        "quality_criteria": [
            "命令执行结果完整 (stdout/stderr/returncode)",
            "超时场景有明确标识",
            "输出截断时标注原始长度",
        ],
    },
    "file_ops": {
        "role": "文件操作工程师",
        "responsibilities": "执行文件读写、列表、删除等操作",
        "quality_criteria": [
            "操作结果明确 (路径/内容/写入字节数)",
            "文件不存在时返回明确错误",
            "大文件读取时分段标注",
        ],
    },
    "monitoring": {
        "role": "系统监控工程师",
        "responsibilities": "采集系统资源使用率并上报",
        "quality_criteria": [
            "数据包含 CPU/内存/磁盘 使用率",
            "附带时间戳",
            "异常指标有标注",
        ],
    },
    "rag_search": {
        "role": "知识检索工程师",
        "responsibilities": "从知识库中检索与任务相关的信息",
        "quality_criteria": [
            "检索结果按相关性排序",
            "每条结果附带来源和相关性分数",
            "无结果时返回明确提示",
        ],
    },
}

# 未知技能类型的默认角色
DEFAULT_ROLE = {
    "role": "通用执行工程师",
    "responsibilities": "根据 PM 分配的任务描述执行并返回结果",
    "quality_criteria": [
        "结果结构化、清晰可读",
        "附带简要执行说明",
    ],
}


# ── Prompt 构建器 ────────────────────────────────────────────────

def build_subagent_prompt(
    pm_id: str,
    agent_id: str,
    agent_name: str,
    task_name: str,
    task_description: str,
    skill: str = "",
    team_name: str = "",
    collaboration_mode: str = "",
    teammates: Optional[list] = None,
    dependencies: Optional[list] = None,
    dependents: Optional[list] = None,
    quality_requirements: Optional[list] = None,
    milestones: Optional[list] = None,
    input_data: Optional[dict] = None,
    extra_instructions: str = "",
) -> str:
    """构建子 Agent 的完整 system prompt。

    通用部分 (BASE_SUBAGENT_PROMPT) + 任务定制部分拼接而成。
    PM Agent 在创建子 Agent 时调用此函数，生成定制 prompt 后
    通过 /pm/create-subagent 的 system_prompt 字段传递。

    Args:
        pm_id: PM Agent 的 ID
        agent_id: 子 Agent 的 ID
        agent_name: 子 Agent 名称
        task_name: 分配的子任务名称
        task_description: 子任务详细描述
        skill: 技能类型 (code_generation / code_review / ...)
        team_name: 所属团队名称
        collaboration_mode: 协作模式 (single/orchestrator/teams/bus/shared_state)
        teammates: 队友信息列表 [{"agent_name", "role", "task"}]
        dependencies: 前序依赖 [{"task_name", "agent_name", "output_summary"}]
        dependents: 后续依赖方 [{"task_name", "agent_name"}] (谁依赖你的输出)
        quality_requirements: 额外质量要求列表
        milestones: 关键里程碑列表 ["阶段1: xxx", "阶段2: xxx"]
        input_data: 任务输入数据 (JSON 序列化后注入)
        extra_instructions: PM 额外叮嘱 (自由文本)

    Returns:
        完整的 system prompt 字符串
    """
    sections = [BASE_SUBAGENT_PROMPT]

    # ── 角色定位 ──
    role_tpl = ROLE_TEMPLATES.get(skill, DEFAULT_ROLE)
    sections.append(f"""
# 你的角色

## 身份
- Agent ID: {agent_id}
- Agent 名称: {agent_name}
- 角色: {role_tpl['role']}
- 职责: {role_tpl['responsibilities']}

## 当前任务
- 任务名称: {task_name}
- 任务描述: {task_description}
""")

    # ── 团队上下文 ──
    if team_name or collaboration_mode:
        sections.append(f"""
# 团队上下文
- PM Agent: {pm_id}
- 团队: {team_name or '未指定'}
- 协作模式: {collaboration_mode or '未指定'}
""")

    if teammates:
        teammate_lines = []
        for t in teammates:
            teammate_lines.append(
                f"  - {t.get('agent_name', '?')}: 角色={t.get('role', '?')}, "
                f"任务={t.get('task', '?')}"
            )
        sections.append("## 队友\n" + "\n".join(teammate_lines))

    # ── 依赖关系 ──
    if dependencies:
        dep_lines = []
        for d in dependencies:
            dep_lines.append(
                f"  - {d.get('task_name', '?')} (由 {d.get('agent_name', '?')} 完成): "
                f"{d.get('output_summary', '等待输出')}"
            )
        sections.append(
            "## 前序依赖 (你需要这些任务的输出作为输入)\n"
            "请等待 PM 提供以下任务的输出后再开始执行:\n" + "\n".join(dep_lines)
        )

    if dependents:
        dep_lines = []
        for d in dependents:
            dep_lines.append(
                f"  - {d.get('task_name', '?')} (由 {d.get('agent_name', '?')} 执行)"
            )
        sections.append(
            "## 后续依赖 (你的输出将被以下任务使用)\n"
            "请确保输出完整、格式规范，便于下游任务直接使用:\n" + "\n".join(dep_lines)
        )

    # ── 质量要求 ──
    quality_items = list(role_tpl.get("quality_criteria", []))
    if quality_requirements:
        quality_items.extend(quality_requirements)
    if quality_items:
        sections.append(
            "## 质量要求\n" + "\n".join(f"- {q}" for q in quality_items)
        )

    # ── 里程碑 ──
    if milestones:
        sections.append(
            "## 关键里程碑 (到达每个里程碑时上报进度)\n"
            + "\n".join(f"- {m}" for m in milestones)
        )

    # ── 输入数据 ──
    if input_data:
        try:
            data_str = json.dumps(input_data, ensure_ascii=False, indent=2)
            # 截断过长的输入数据
            if len(data_str) > 2000:
                data_str = data_str[:2000] + "\n... (已截断)"
            sections.append(f"## 输入数据\n```json\n{data_str}\n```")
        except (TypeError, ValueError):
            sections.append(f"## 输入数据\n{str(input_data)[:1000]}")

    # ── PM 额外叮嘱 ──
    if extra_instructions:
        sections.append(f"## PM 额外指示\n{extra_instructions}")

    # ── 上报地址 ──
    sections.append(f"""
# 进度上报
你的 Agent ID 是 `{agent_id}`，PM ID 是 `{pm_id}`。
请通过 Worker 的 /pm/progress-report 端点向 PM 上报进度。
""")

    return "\n".join(sections)


def build_dispatch_context(
    task: dict,
    sub: dict,
    plan: dict,
    pm_id: str,
) -> dict:
    """构建任务分发时的附加上下文 (注入 subtask payload)。

    此上下文不直接作为 system prompt，而是作为 input_data 的补充，
    让子 Agent 在执行具体 handler 时能获取团队信息。

    Args:
        task: 原始任务字典
        sub: 子任务规划项
        plan: PM 的架构决策 plan
        pm_id: PM Agent ID

    Returns:
        dict, 合并到 subtask 的 input_data 中
    """
    decomposition = plan.get("decomposition", [])
    sub_name = sub.get("name", "")

    # 找出依赖和被依赖关系
    deps = []
    for dep_name in sub.get("depends_on", []):
        for d in decomposition:
            if d.get("name") == dep_name:
                deps.append({
                    "task_name": dep_name,
                    "agent_name": d.get("name", ""),
                    "output_summary": "等待输出",
                })
                break

    dependents = []
    for d in decomposition:
        if sub_name in d.get("depends_on", []):
            dependents.append({
                "task_name": d.get("name", ""),
                "agent_name": d.get("name", ""),
            })

    # 队友信息
    teammates = []
    for d in decomposition:
        if d.get("name") != sub_name:
            teammates.append({
                "agent_name": d.get("name", ""),
                "role": d.get("skill", ""),
                "task": d.get("description", "")[:100],
            })

    return {
        "_team_context": {
            "pm_id": pm_id,
            "collaboration_mode": plan.get("pattern", ""),
            "team_size": plan.get("team_size", 1),
            "teammates": teammates,
            "dependencies": deps,
            "dependents": dependents,
            "task_decomposition": [
                {"name": d.get("name"), "skill": d.get("skill")}
                for d in decomposition
            ],
        },
    }


# ── 结果聚合 Prompt (优化4) ─────────────────────────────────────

def build_aggregation_prompt(
    task_name: str,
    task_description: str,
    subtask_results: list,
    plan: dict,
) -> str:
    """构建 PM 结果聚合 prompt (优化4)。

    PM 在所有子任务完成后, 调用 LLM 按依赖顺序聚合各子任务结果。
    生成统一的最终输出, 检查一致性和完整性。

    Args:
        task_name: 原始任务名称
        task_description: 原始任务描述
        subtask_results: 子任务结果列表 [{"name", "skill", "status", "output"}]
        plan: PM 的架构决策 (含 decomposition 和依赖关系)

    Returns:
        聚合 prompt 字符串
    """
    decomposition = plan.get("decomposition", [])

    # 构建子任务结果摘要
    result_lines = []
    for r in subtask_results:
        name = r.get("name", "")
        skill = r.get("skill", "")
        status = r.get("status", "unknown")
        output = r.get("output", "")

        # 截断过长的输出
        output_str = str(output)
        if len(output_str) > 1500:
            output_str = output_str[:1500] + "\n... (已截断)"

        result_lines.append(
            f"### 子任务: {name}\n"
            f"- 技能: {skill}\n"
            f"- 状态: {status}\n"
            f"- 输出:\n{output_str}\n"
        )

    # 构建依赖关系图
    dep_lines = []
    for d in decomposition:
        deps = d.get("depends_on", [])
        if deps:
            dep_lines.append(f"  - {d.get('name')} ← 依赖 {deps}")
        else:
            dep_lines.append(f"  - {d.get('name')} (无前序依赖)")

    return f"""{PM_CARD['identity']}所有子任务已完成, 请按依赖顺序聚合结果。

# 原始任务
- 名称: {task_name}
- 描述: {task_description}

# 子任务依赖关系
{chr(10).join(dep_lines)}

# 各子任务结果
{chr(10).join(result_lines)}

# 聚合要求
1. 按依赖顺序整合各子任务输出, 消除冗余和矛盾
2. 检查一致性: 如有矛盾, 以后序任务(更接近最终目标)的结果为准并标注
3. 生成统一的最终交付物, 包含:
   - 总体概述 (一段话)
   - 各部分成果 (按逻辑顺序)
   - 已知问题和后续建议
4. 如果任何子任务状态为 failed, 在聚合结果中标注失败部分并给出影响评估
5. 输出格式: 结构化 Markdown

请直接输出聚合结果, 不要添加额外解释。
"""
