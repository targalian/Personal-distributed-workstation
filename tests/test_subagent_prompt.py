"""子 Agent 输出格式、工具边界及 planner 提示契约回归。"""

import json
from copy import deepcopy
from unittest.mock import Mock

import pytest

from lan_mesh.agent_prompt import build_subagent_prompt
from lan_mesh.pm_dispatcher import PMDispatcher
from lan_mesh.pm_planner import PMPlanner


def make_prompt(**kwargs):
    return build_subagent_prompt("pm", "agent", "执行者", "任务", "完成当前任务", **kwargs)


def section(prompt, heading):
    return prompt.split(f"## {heading}\n", 1)[1].split("\n## ", 1)[0]


def test_legacy_call_has_default_contract():
    prompt = make_prompt()
    assert "结构化 Markdown + 简要执行说明" in section(prompt, "预期输出格式")
    tools = section(prompt, "工具使用约束")
    assert "破坏性操作" in tools and "密钥" in tools and "任务范围外" in tools
    assert "不接管队友任务" in section(prompt, "任务边界")
    assert "向 PM 上报阻塞" in section(prompt, "任务边界")
    assert "self_check" in prompt and "/pm/progress-report" in prompt


@pytest.mark.parametrize("skill, expected", [
    ("code_generation", "完整可运行代码 + 文件路径 + 简要说明"),
    ("code_review", "问题清单（严重程度、文件路径、行号、修复建议）"),
    ("document_summary", "结构化 Markdown 摘要"),
    ("unknown", "结构化 Markdown + 简要执行说明"),
])
def test_skill_output_defaults(skill, expected):
    assert expected in section(make_prompt(skill=skill), "预期输出格式")


def test_explicit_contract_overrides_format_and_adds_constraints():
    options = dict(skill="code_generation", output_format="JSONL",
                   boundaries=["只改 a.py"], allowed_tools=["file_read"],
                   forbidden_actions=["禁止联网"])
    original = deepcopy(options)
    prompt = make_prompt(**options)
    output = section(prompt, "预期输出格式")
    assert "JSONL" in output and "完整可运行代码" not in output
    tools = section(prompt, "工具使用约束")
    assert "仅允许使用以下工具: file_read" in tools
    assert "禁止联网" in tools and "破坏性操作" in tools
    assert "只改 a.py" in section(prompt, "任务边界")
    assert options == original


def test_empty_allowed_tools_disables_tools():
    assert "无（不调用工具）" in section(make_prompt(allowed_tools=[]), "工具使用约束")
    assert "已获授权的工具" in section(make_prompt(), "工具使用约束")


@pytest.mark.parametrize("custom", [False, True])
def test_dispatcher_passes_subtask_contract(custom):
    dispatcher = PMDispatcher("pm", Mock(), "", "device", Mock(), Mock())
    sub = dict(name="审查", skill="code_review", description="审查指定文件")
    if custom:
        sub.update(output_format="JSONL", boundaries=["只改 a.py"],
                   allowed_tools=["file_read"])
    original = deepcopy(sub)
    prompt = dispatcher._build_subagent_prompt_for_sub(
        {"input_data": {}}, sub, {"decomposition": [sub]}, "agent", "执行者")
    assert ("JSONL" if custom else "问题清单") in section(prompt, "预期输出格式")
    if custom:
        assert "只改 a.py" in section(prompt, "任务边界")
        assert "仅允许使用以下工具: file_read" in section(prompt, "工具使用约束")
    assert sub == original


@pytest.mark.parametrize("with_contract", [False, True])
def test_planner_prompt_requests_contract_and_preserves_json(monkeypatch, with_contract):
    planner = object.__new__(PMPlanner)
    planner._pm_id = "pm"
    planner._runtime = Mock()
    planner._skill_content = "决策框架"
    planner._build_memory_hint = Mock(return_value="")
    planner._build_blueprint_section = Mock(return_value="")
    monkeypatch.setattr("lan_mesh.task_templates.match_template", lambda _: None)
    sub = dict(name="检查", skill="react_agent", depends_on=[], description="检查文件")
    if with_contract:
        sub.update(output_format="JSONL", boundaries=["只改 a.py"],
                   budget={"max_steps": 7, "max_tokens": 50000})
    plan = dict(complexity="moderate", pattern="orchestrator", team_size=2,
                decomposition=[sub], reasoning="限定范围")
    call = planner._runtime._call_llm_with_routing
    call.return_value = {"content": json.dumps(plan, ensure_ascii=False)}
    result = planner.analyze_with_skill({"name": "检查", "description": "检查文件"})
    prompt = call.call_args.args[0]
    for field in ("output_format", "boundaries", "budget", "max_steps", "max_tokens"):
        assert field in prompt
    assert "10 步 / 80000 tokens" in prompt
    assert "30 步 / 300000 tokens" in prompt
    assert "不是运行时硬限制" in prompt
    assert result == plan
    assert ("budget" in result["decomposition"][0]) is with_contract
