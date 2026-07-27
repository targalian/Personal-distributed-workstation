"""
F2.4: 任务模板库 — 预置常见任务 DAG

职责:
1. 提供预定义的任务分解模板 (DAG 结构)
2. PM Agent 规划时可匹配模板加速规划
3. 支持用户自定义模板注册
4. 模板变量替换 ({{project_name}}, {{language}} 等)

用法:
    from .task_templates import get_template, list_templates, match_template

    # 列出所有模板
    templates = list_templates()

    # 按任务描述匹配最佳模板
    template = match_template("帮我审查这个 Python 项目的代码质量")

    # 应用模板 (变量替换)
    plan = apply_template(template, {"language": "python", "project_path": "/src"})
"""
import re
from typing import Optional


# ── 预置模板 ──────────────────────────────────────────────────

TEMPLATES = {
    "code_review_full": {
        "name": "完整代码审查",
        "description": "多维度代码审查: 静态分析 + 逻辑审查 + 安全扫描",
        "keywords": ["代码审查", "code review", "审查", "review", "质量检查"],
        "pattern": "orchestrator",
        "team_size": 3,
        "decomposition": [
            {
                "name": "静态分析",
                "skill": "shell_exec",
                "depends_on": [],
                "description": "运行 linter (flake8/pylint/eslint) 对 {{project_path}} 进行静态分析, 收集警告和错误",
            },
            {
                "name": "逻辑审查",
                "skill": "code_review",
                "depends_on": [],
                "description": "审查 {{project_path}} 的代码逻辑、设计模式、可维护性, 重点关注复杂函数和边界条件",
            },
            {
                "name": "安全扫描",
                "skill": "code_review",
                "depends_on": [],
                "description": "检查 {{project_path}} 的安全漏洞: SQL注入、XSS、硬编码密钥、不安全的依赖",
            },
            {
                "name": "汇总报告",
                "skill": "document_summary",
                "depends_on": ["静态分析", "逻辑审查", "安全扫描"],
                "description": "汇总三项审查结果, 按严重程度排序, 生成改进建议和优先级列表",
            },
        ],
    },

    "feature_development": {
        "name": "功能开发",
        "description": "从需求到代码的完整功能开发流程",
        "keywords": ["开发", "实现", "功能", "feature", "develop", "编写"],
        "pattern": "orchestrator",
        "team_size": 3,
        "decomposition": [
            {
                "name": "需求分析",
                "skill": "react_agent",
                "depends_on": [],
                "description": "分析项目 {{project_path}} 的现有结构, 确定新功能的接入点、依赖和影响范围",
            },
            {
                "name": "代码实现",
                "skill": "react_agent",
                "depends_on": ["需求分析"],
                "description": "根据需求分析结果, 在 {{project_path}} 中实现功能代码, 包含错误处理和类型标注",
            },
            {
                "name": "单元测试",
                "skill": "react_agent",
                "depends_on": ["代码实现"],
                "description": "为新实现的功能编写单元测试, 覆盖正常路径和边界条件, 运行测试确认通过",
            },
        ],
    },

    "bug_fix": {
        "name": "Bug 修复",
        "description": "定位、修复、验证 Bug 的标准流程",
        "keywords": ["修复", "bug", "fix", "错误", "异常", "问题"],
        "pattern": "orchestrator",
        "team_size": 2,
        "decomposition": [
            {
                "name": "问题定位",
                "skill": "react_agent",
                "depends_on": [],
                "description": "在 {{project_path}} 中复现并定位 Bug: 阅读错误日志、追踪调用链、确定根因",
            },
            {
                "name": "修复实现",
                "skill": "react_agent",
                "depends_on": ["问题定位"],
                "description": "根据定位结果修复代码, 添加回归测试, 验证修复有效且无副作用",
            },
        ],
    },

    "documentation": {
        "name": "文档生成",
        "description": "自动生成项目文档 (API 文档 / README / 架构说明)",
        "keywords": ["文档", "document", "README", "说明", "API文档"],
        "pattern": "orchestrator",
        "team_size": 2,
        "decomposition": [
            {
                "name": "代码扫描",
                "skill": "react_agent",
                "depends_on": [],
                "description": "扫描 {{project_path}} 的模块结构、公共 API、类/函数签名和 docstring",
            },
            {
                "name": "文档撰写",
                "skill": "code_generation",
                "depends_on": ["代码扫描"],
                "description": "根据扫描结果生成 {{doc_type}} 文档, 包含模块说明、API 列表、使用示例",
            },
        ],
    },

    "refactoring": {
        "name": "代码重构",
        "description": "安全重构: 分析→重构→验证",
        "keywords": ["重构", "refactor", "优化", "整理", "清理"],
        "pattern": "orchestrator",
        "team_size": 2,
        "decomposition": [
            {
                "name": "重构分析",
                "skill": "code_review",
                "depends_on": [],
                "description": "分析 {{project_path}} 中需要重构的部分: 重复代码、过长函数、耦合度过高的模块",
            },
            {
                "name": "执行重构",
                "skill": "react_agent",
                "depends_on": ["重构分析"],
                "description": "按分析建议执行重构, 每次修改后运行测试确保行为不变",
            },
        ],
    },

    "data_pipeline": {
        "name": "数据处理流水线",
        "description": "ETL 风格的数据采集→处理→输出流水线",
        "keywords": ["数据", "data", "ETL", "处理", "爬取", "采集"],
        "pattern": "orchestrator",
        "team_size": 3,
        "decomposition": [
            {
                "name": "数据采集",
                "skill": "react_agent",
                "depends_on": [],
                "description": "从指定来源采集数据: {{data_source}}",
            },
            {
                "name": "数据清洗",
                "skill": "react_agent",
                "depends_on": ["数据采集"],
                "description": "清洗和转换采集到的数据: 去重、格式标准化、缺失值处理",
            },
            {
                "name": "结果输出",
                "skill": "react_agent",
                "depends_on": ["数据清洗"],
                "description": "将处理后的数据输出到目标格式: {{output_format}}",
            },
        ],
    },
}


# ── 公共 API ──────────────────────────────────────────────────

def list_templates() -> list[dict]:
    """列出所有可用模板 (摘要信息)。"""
    return [
        {
            "id": tid,
            "name": t["name"],
            "description": t["description"],
            "keywords": t["keywords"],
            "pattern": t["pattern"],
            "team_size": t["team_size"],
            "steps": len(t["decomposition"]),
        }
        for tid, t in TEMPLATES.items()
    ]


def get_template(template_id: str) -> Optional[dict]:
    """获取指定模板的完整定义。"""
    return TEMPLATES.get(template_id)


def match_template(task_description: str) -> Optional[dict]:
    """根据任务描述匹配最佳模板。

    匹配策略: 关键词命中数最多的模板。
    返回 None 表示无匹配 (应走 LLM 自主规划)。
    """
    desc_lower = task_description.lower()
    best_match = None
    best_score = 0

    for tid, template in TEMPLATES.items():
        score = sum(1 for kw in template["keywords"] if kw.lower() in desc_lower)
        if score > best_score:
            best_score = score
            best_match = tid

    if best_match and best_score >= 1:
        result = TEMPLATES[best_match].copy()
        result["template_id"] = best_match
        result["match_score"] = best_score
        return result
    return None


def apply_template(template: dict, variables: dict = None) -> dict:
    """应用模板, 替换变量占位符。

    变量格式: {{variable_name}}
    未提供的变量保留原样。

    Returns:
        与 PM Agent _analyze_with_skill 返回格式兼容的 plan dict
    """
    import json

    variables = variables or {}
    # 深拷贝
    plan = json.loads(json.dumps(template))

    # 替换变量
    def _replace_vars(text: str) -> str:
        for key, value in variables.items():
            text = text.replace("{{" + key + "}}", str(value))
        return text

    for sub in plan.get("decomposition", []):
        sub["description"] = _replace_vars(sub.get("description", ""))
        sub["name"] = _replace_vars(sub.get("name", ""))

    # 返回 PM 兼容格式
    return {
        "complexity": "moderate",
        "pattern": plan.get("pattern", "orchestrator"),
        "team_size": plan.get("team_size", 2),
        "decomposition": plan.get("decomposition", []),
        "reasoning": f"匹配模板: {plan.get('name', '')} (score={plan.get('match_score', 0)})",
        "template_id": plan.get("template_id", ""),
    }
