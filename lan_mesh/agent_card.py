"""
Agent Card 生成与管理 — 借鉴 A2A 协议的 Agent Card 机制

每个 Worker 启动时根据自身配置生成能力卡片,
包含技能声明、可用工具、模型偏好等信息。

Master 通过 Agent Card 进行任务匹配与分发。
"""
import socket
from pathlib import Path
from typing import List

from .protocol import AgentCard, Skill, ToolDef


# ── 预置技能库 ──────────────────────────────────────────────────

DEFAULT_SKILLS = {
    "code_generation": Skill(
        name="code_generation",
        description="根据需求描述生成代码 (Python/JavaScript/Go 等)",
        input_schema={
            "type": "object",
            "properties": {
                "language": {"type": "string", "description": "目标编程语言"},
                "requirement": {"type": "string", "description": "需求描述"},
                "context": {"type": "string", "description": "上下文/已有代码"},
            },
            "required": ["requirement"],
        },
        tags=["coding", "llm"],
    ),
    "code_review": Skill(
        name="code_review",
        description="审查代码质量,发现 Bug 和改进建议",
        input_schema={
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "待审查的代码"},
                "language": {"type": "string", "description": "编程语言"},
            },
            "required": ["code"],
        },
        tags=["coding", "analysis"],
    ),
    "document_summary": Skill(
        name="document_summary",
        description="长文档摘要、关键信息提取",
        input_schema={
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "待摘要的文本"},
                "max_length": {"type": "integer", "description": "最大摘要长度"},
            },
            "required": ["text"],
        },
        tags=["nlp", "analysis"],
    ),
    "rag_search": Skill(
        name="rag_search",
        description="基于知识库的 RAG 检索与问答",
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "检索查询"},
                "top_k": {"type": "integer", "description": "返回结果数"},
            },
            "required": ["query"],
        },
        tags=["rag", "retrieval"],
    ),
    "shell_exec": Skill(
        name="shell_exec",
        description="执行 Shell 命令并返回输出",
        input_schema={
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell 命令"},
                "timeout": {"type": "integer", "description": "超时秒数"},
            },
            "required": ["command"],
        },
        tags=["system", "exec"],
    ),
    "file_ops": Skill(
        name="file_ops",
        description="文件读写操作",
        input_schema={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["read", "write", "list", "delete"]},
                "path": {"type": "string", "description": "文件路径"},
                "content": {"type": "string", "description": "写入内容 (write 时)"},
            },
            "required": ["action", "path"],
        },
        tags=["system", "file"],
    ),
    "monitoring": Skill(
        name="monitoring",
        description="系统资源监控与告警 (CPU/内存/磁盘/网络)",
        input_schema={
            "type": "object",
            "properties": {
                "metric": {"type": "string", "description": "监控指标"},
                "threshold": {"type": "number", "description": "告警阈值"},
            },
        },
        tags=["system", "monitor"],
    ),
}


# ── 预置工具库 ──────────────────────────────────────────────────

DEFAULT_TOOLS = {
    "file_read": ToolDef(
        name="file_read",
        description="读取文件内容",
        mcp_compatible=True,
        input_schema={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    ),
    "file_write": ToolDef(
        name="file_write",
        description="写入文件内容",
        mcp_compatible=True,
        input_schema={
            "type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"],
        },
    ),
    "shell_exec": ToolDef(
        name="shell_exec",
        description="执行 Shell 命令",
        mcp_compatible=True,
        input_schema={
            "type": "object",
            "properties": {"command": {"type": "string"}, "timeout": {"type": "integer"}},
            "required": ["command"],
        },
    ),
    "http_request": ToolDef(
        name="http_request",
        description="发起 HTTP 请求",
        mcp_compatible=True,
        input_schema={
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "method": {"type": "string"},
                "headers": {"type": "object"},
                "body": {"type": "string"},
            },
            "required": ["url"],
        },
    ),
}


# ── Agent Card 生成 ─────────────────────────────────────────────

def generate_agent_card(
    device_id: str,
    agent_name: str,
    ip: str,
    api_port: int,
    hostname: str,
    skill_names: List[str] = None,
    tool_names: List[str] = None,
    model_preferences: List[str] = None,
    max_concurrent_tasks: int = 5,
) -> AgentCard:
    """根据配置生成 Agent Card。

    Args:
        device_id: 设备 ID (与 Worker 共享)
        agent_name: Agent 名称
        ip: 本机 IP
        api_port: HTTP API 端口
        hostname: 主机名
        skill_names: 启用的技能列表 (None 则全部启用)
        tool_names: 启用的工具列表 (None 则全部启用)
        model_preferences: 模型偏好列表
        max_concurrent_tasks: 最大并发任务数
    """
    # 选择技能
    if skill_names:
        skills = [DEFAULT_SKILLS[name].to_dict() for name in skill_names if name in DEFAULT_SKILLS]
    else:
        skills = [s.to_dict() for s in DEFAULT_SKILLS.values()]

    # 选择工具
    if tool_names:
        tools = [DEFAULT_TOOLS[name].to_dict() for name in tool_names if name in DEFAULT_TOOLS]
    else:
        tools = [t.to_dict() for t in DEFAULT_TOOLS.values()]

    return AgentCard(
        agent_id=device_id,        # 复用 device_id 作为 agent_id
        agent_name=agent_name,
        version="0.1.0",
        device_id=device_id,
        hostname=hostname,
        ip=ip,
        api_port=api_port,
        skills=skills,
        tools=tools,
        model_preferences=model_preferences or ["deepseek-v3", "gpt-4o-mini"],
        max_concurrent_tasks=max_concurrent_tasks,
        status="idle",
        current_task_count=0,
    )


def get_default_skill_names() -> List[str]:
    """返回所有预置技能名称。"""
    return list(DEFAULT_SKILLS.keys())


def get_default_tool_names() -> List[str]:
    """返回所有预置工具名称。"""
    return list(DEFAULT_TOOLS.keys())
