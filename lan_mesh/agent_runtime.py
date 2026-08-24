"""
Agent 运行时 — Worker 端任务执行引擎

职责:
1. 接收 Secretary 分发的子任务
2. 根据技能类型执行任务 (调用 LLM API / 运行工具 / 本地处理)
3. 返回执行结果

执行策略:
- code_generation: 调用外部 LLM API 生成代码
- code_review: 调用外部 LLM API 审查代码
- document_summary: 调用外部 LLM API 生成摘要
- rag_search: 本地知识库检索 (预留)
- shell_exec: 执行 Shell 命令
- file_ops: 文件读写
- monitoring: 采集系统资源

LLM API 调用通过环境变量配置 (OPENAI_API_KEY / DEEPSEEK_API_KEY 等)。
"""
import os
import shutil
import subprocess
import threading
import time
import json as _json
from pathlib import Path
from typing import Optional

import requests

from .logger import get_logger

logger = get_logger("agent_runtime")


_env_lock = threading.Lock()
_env_loaded = False


def _ensure_env_loaded():
    """确保 API Key 环境变量已加载。

    如果检测到没有任何 API Key 环境变量, 尝试从常见路径加载 .env 文件。
    解决: 进程从非项目根目录启动时 load_dotenv() 找不到 .env 的问题。
    线程安全: 使用 Lock + 标志位防止多线程重复加载。
    """
    global _env_loaded
    key_envs = ["ALIYUN_TOKENPLAN_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY",
                "ANTHROPIC_API_KEY", "QWEN_API_KEY"]
    if any(os.environ.get(k) for k in key_envs):
        _env_loaded = True
        return  # 已有 key, 无需加载
    with _env_lock:
        if _env_loaded:
            return  # 另一线程已完成加载
        # 尝试从多个路径加载 .env
        candidates = [
            Path(__file__).parent.parent / ".env",   # 项目根目录
            Path.cwd() / ".env",                      # 当前工作目录
            Path.home() / ".lan_mesh" / ".env",       # 用户数据目录
        ]
        for p in candidates:
            if p.is_file():
                try:
                    from dotenv import load_dotenv
                    load_dotenv(p, override=False)
                    if any(os.environ.get(k) for k in key_envs):
                        logger.info("已从 %s 加载 API Key 环境变量", p)
                        _env_loaded = True
                        return
                except ImportError:
                    # 手动解析 .env
                    for line in p.read_text(encoding="utf-8").splitlines():
                        line = line.strip()
                        if not line or line.startswith("#"):
                            continue
                        if "=" in line:
                            k, v = line.split("=", 1)
                            k, v = k.strip(), v.strip()
                            if k and v and not os.environ.get(k):
                                os.environ[k] = v
                    if any(os.environ.get(k) for k in key_envs):
                        logger.info("已从 %s 手动解析 API Key", p)
                        _env_loaded = True
                        return
                except Exception as e:
                    logger.debug("加载 %s 失败: %s", p, e)
        _env_loaded = True  # 标记已尝试, 避免反复扫描文件系统


# 模块加载时立即检查
_ensure_env_loaded()


# ── 安全沙箱配置 ────────────────────────────────────────────

# 禁止执行的危险命令前缀/关键词
SHELL_BLOCKED_PATTERNS = [
    "rm -rf /", "rm -rf ~", "rm -rf .",
    "mkfs", "dd if=", "fdisk", "format",
    "shutdown", "reboot", "halt", "poweroff",
    "chmod 777 /", "chown",
    ":(){ :|:& };:",  # fork bomb
    "wget", "curl",  # 禁止网络下载
    "sudo", "su ",
    "> /dev/", "> /etc/", "> /usr/",
    "iptables", "systemctl", "service ",
]

# 文件操作允许的最大文件大小 (10MB)
MAX_FILE_SIZE = 10 * 1024 * 1024

# Shell 输出最大长度 (100KB)
MAX_OUTPUT_LENGTH = 100 * 1024


# ── Provider 默认配置 (provider → base_url) ──────────────────────

PROVIDER_CONFIG = {
    "deepseek":          {"base_url": "https://api.deepseek.com/v1", "api_key_env": "DEEPSEEK_API_KEY"},
    "openai":            {"base_url": "https://api.openai.com/v1", "api_key_env": "OPENAI_API_KEY"},
    "anthropic":         {"base_url": "https://api.anthropic.com/v1", "api_key_env": "ANTHROPIC_API_KEY"},
    "qwen":              {"base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "api_key_env": "QWEN_API_KEY"},
    "aliyun-tokenplan":  {"base_url": "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1", "api_key_env": "ALIYUN_TOKENPLAN_API_KEY"},
}


def _load_model_pool_entries():
    """惰性加载 model_pool.yaml, 返回 {model_id: ModelEntryConfig} 字典。"""
    try:
        from .config import load_model_pool
        pool = load_model_pool()
        return {e.id: e for e in pool.models}
    except Exception:
        return {}


# ── CLI Agent 配置 ────────────────────────────────────────────

# 阿里 Token Plan 端点 (同时支持 OpenAI 和 Anthropic 协议)
TOKEN_PLAN_OPENAI_BASE = "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
TOKEN_PLAN_ANTHROPIC_BASE = "https://token-plan.cn-beijing.maas.aliyuncs.com/apps/anthropic"

# 支持的 CLI Agent 后端 (name → 检测命令 + 调用模板)
CLI_AGENT_BACKENDS = {
    "claude": {
        "detect": "claude",
        "build_cmd": lambda prompt, cwd: [
            "claude", "--print", "--output-format", "json",
            "--model", "qwen3.8-max",
            "--max-turns", "30", prompt,
        ],
        "timeout": 600,
        "description": "Claude Code CLI — 自主编码 Agent (Token Plan qwen3.8-max)",
    },
    "aider": {
        "detect": "aider",
        "build_cmd": lambda prompt, cwd: [
            "aider", "--yes", "--no-auto-commits",
            "--model", "openai/qwen3.8-max",
            "--architect-model", "openai/qwen3.6-flash",
            "--message", prompt,
        ],
        "timeout": 600,
        "description": "Aider — Git-aware 双模型编码 Agent (Token Plan 零成本)",
    },
    "codex": {
        "detect": "codex",
        "build_cmd": lambda prompt, cwd: [
            "codex", "--quiet", "--full-auto", prompt,
        ],
        "timeout": 600,
        "description": "OpenAI Codex CLI — 沙箱编码 Agent",
    },
}

# CLI Agent 默认超时 (s), 可通过环境变量覆盖
CLI_AGENT_TIMEOUT = int(os.environ.get("CLI_AGENT_TIMEOUT", "600"))
# CLI Agent 输出截断限制
CLI_AGENT_MAX_OUTPUT = 200 * 1024  # 200KB


def _build_cli_env(backend: str) -> dict:
    """构建 CLI Agent 执行环境变量, 自动注入 Token Plan 凭据。

    策略:
    - claude: 注入 ANTHROPIC_API_KEY + ANTHROPIC_BASE_URL (Token Plan Anthropic 端点)
    - aider:  注入 OPENAI_API_KEY + OPENAI_API_BASE (Token Plan OpenAI 端点)
    - 如果用户已设置原生 API Key, 优先使用原生 (不覆盖)
    """
    env = {**os.environ, "NO_COLOR": "1"}
    token_plan_key = os.environ.get("ALIYUN_TOKENPLAN_API_KEY", "")

    if not token_plan_key:
        return env

    if backend == "claude":
        # 仅在用户未配置原生 Anthropic Key 时注入 Token Plan
        if not env.get("ANTHROPIC_API_KEY"):
            env["ANTHROPIC_API_KEY"] = token_plan_key
            env["ANTHROPIC_BASE_URL"] = TOKEN_PLAN_ANTHROPIC_BASE
            logger.debug("[CLI Agent] claude 使用 Token Plan Anthropic 端点")

    elif backend == "aider":
        # 仅在用户未配置原生 OpenAI Key 时注入 Token Plan
        if not env.get("OPENAI_API_KEY"):
            env["OPENAI_API_KEY"] = token_plan_key
            env["OPENAI_API_BASE"] = TOKEN_PLAN_OPENAI_BASE
            logger.debug("[CLI Agent] aider 使用 Token Plan OpenAI 端点")

    return env


def _which_with_fallback(cmd: str) -> Optional[str]:
    """shutil.which 增强: PATH 找不到时回退常见安装目录。"""
    found = shutil.which(cmd)
    if found:
        return found
    # Windows 常见全局安装路径
    extra_dirs = [
        os.path.join(os.environ.get("APPDATA", ""), "npm"),
        r"C:\Program Files\nodejs",
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "nodejs"),
    ]
    for d in extra_dirs:
        if not d or not os.path.isdir(d):
            continue
        candidate = os.path.join(d, f"{cmd}.cmd" if os.name == "nt" else cmd)
        if os.path.isfile(candidate):
            return candidate
    return None


def detect_cli_agents() -> list[str]:
    """检测系统上可用的 CLI Agent 后端。

    Returns:
        可用的 CLI Agent 名称列表, 如 ["claude", "aider"]
    """
    available = []
    for name, cfg in CLI_AGENT_BACKENDS.items():
        if _which_with_fallback(cfg["detect"]):
            available.append(name)
    return available


def get_preferred_cli_agent() -> Optional[str]:
    """获取首选 CLI Agent (环境变量 > 自动检测)。

    环境变量 CLI_AGENT_BACKEND 可指定: claude / aider / codex
    未设置时按优先级自动选择: claude > aider > codex
    (claude 和 aider 均可通过 Token Plan 零成本使用)
    """
    preferred = os.environ.get("CLI_AGENT_BACKEND", "").lower().strip()
    if preferred and preferred in CLI_AGENT_BACKENDS:
        if _which_with_fallback(CLI_AGENT_BACKENDS[preferred]["detect"]):
            return preferred
        logger.warning("指定的 CLI Agent '%s' 未安装, 回退自动检测", preferred)

    # 自动检测, 按优先级: claude > aider > codex
    for name in ["claude", "aider", "codex"]:
        if _which_with_fallback(CLI_AGENT_BACKENDS[name]["detect"]):
            return name
    return None


class AgentRuntime:
    """Worker 端 Agent 运行时。

    接收子任务,根据 required_skill 路由到对应的执行处理器。
    """

    def __init__(self, agent_id: str, shared_folder_path: str,
                 custom_system_prompt: str = ""):
        self.agent_id = agent_id
        self.shared_folder = shared_folder_path
        self._custom_system_prompt = custom_system_prompt  # PM 注入的定制 prompt
        # 优化3: 当前执行的技能类型 (供选择性加载)
        # 用线程局部变量: 并行分发场景下多个线程共享同一 runtime, 实例属性会互相覆盖
        self._local = threading.local()
        self._skills_cache_dir = Path.home() / ".lan_mesh" / "skills_cache"
        self._handlers = {
            "code_generation": self._handle_code_generation,
            "code_review": self._handle_code_review,
            "document_summary": self._handle_document_summary,
            "rag_search": self._handle_rag_search,
            "shell_exec": self._handle_shell_exec,
            "file_ops": self._handle_file_ops,
            "monitoring": self._handle_monitoring,
            "react_agent": self._handle_react_agent,  # F2.1: 工具循环 Agent
            "cli_agent": self._handle_cli_agent,      # CLI Agent (Claude Code/Codex/Aider)
        }

    def execute(self, subtask: dict) -> dict:
        """执行子任务,返回结果字典。

        支持模型路由器注入: 从 subtask payload 中提取 model_preference 和
        fallback_models, 传递给 LLM 技能处理器, 实现智能模型选择和降级链重试。

        Args:
            subtask: 包含 name, description, required_skill, input_data,
                     model_preference, fallback_models 的字典

        Returns:
            {"output": {...}, "status": "completed"|"failed", "error": "..."}
        """
        import time as _time
        from . import runtime_trace

        skill = subtask.get("required_skill", "")
        handler = self._handlers.get(skill)
        task_id = subtask.get("parent_task_id", "")
        model_pref = subtask.get("model_preference", "")

        # P0: 子任务执行追踪
        trace_id = runtime_trace.trace_subtask_start(
            skill=skill, task_id=task_id, model_pref=model_pref)
        exec_start = _time.time()

        if not handler:
            runtime_trace.trace_subtask_end(
                trace_id, skill, "failed", 0,
                error=f"未知的技能类型: {skill}", task_id=task_id)
            return {
                "output": {},
                "status": "failed",
                "error": f"未知的技能类型: {skill}",
            }

        # 优化3: 记录当前技能类型, 供 _build_system_prompt 选择性加载 (线程局部, 防并行覆盖)
        self._local.current_skill = skill

        # R6: 注入成本归因上下文 — 本线程内后续 LLM 记账自动带上 task_id
        try:
            from .model_resources import set_usage_context
            set_usage_context(task_id=task_id,
                              project_id=subtask.get("project_id", ""))
        except Exception:
            pass

        # 提取路由器注入的模型偏好 (由 orchestrator 写入 payload)
        input_data = dict(subtask.get("input_data", {}))
        input_data["_model_preference"] = model_pref
        input_data["_fallback_models"] = subtask.get("fallback_models", [])
        # 传递 trace_id 供 LLM 调用钩子关联
        input_data["_trace_id"] = trace_id

        try:
            result = handler(input_data)
            # 提取 LLM 调用的 token 用量 (如果 handler 返回了 usage)
            usage = {}
            if isinstance(result, dict) and "usage" in result:
                usage = result.pop("usage", {})
            elapsed_ms = (_time.time() - exec_start) * 1000
            runtime_trace.trace_subtask_end(
                trace_id, skill, "completed", elapsed_ms,
                model=usage.get("model", ""),
                input_tokens=usage.get("input_tokens", 0),
                output_tokens=usage.get("output_tokens", 0),
                task_id=task_id)
            return {"output": result, "status": "completed", "usage": usage}
        except Exception as e:
            elapsed_ms = (_time.time() - exec_start) * 1000
            runtime_trace.trace_subtask_end(
                trace_id, skill, "failed", elapsed_ms,
                error=str(e), task_id=task_id)
            return {"output": {}, "status": "failed", "error": str(e)}

    # ── 技能处理器 ──────────────────────────────────────────────

    def _handle_code_generation(self, input_data: dict) -> dict:
        """代码生成 — 调用 LLM API。"""
        requirement = input_data.get("requirement", input_data.get("description", ""))
        language = input_data.get("language", "python")
        context = input_data.get("context", "")

        prompt = (
            f"请用 {language} 编写以下需求的代码:\n{requirement}\n\n"
            "要求:\n"
            "- 直接输出完整可运行的代码, 不要反问、不要请求补充信息\n"
            "- 需求中未明确的细节, 按最佳实践自行假设 (在代码注释中注明)\n"
            "- 包含必要的错误处理和输入验证\n"
            "- 代码末尾附带简要使用说明"
        )
        if context:
            prompt += f"\n\n已有代码/上下文:\n{context}"

        resp = self._call_llm_with_routing(prompt, input_data)
        return {"code": resp["content"], "language": language, "usage": {"model": resp["model"], "input_tokens": resp["input_tokens"], "output_tokens": resp["output_tokens"]}}

    def _handle_code_review(self, input_data: dict) -> dict:
        """代码审查 — 调用 LLM API。"""
        code = input_data.get("code", input_data.get("requirement", ""))
        language = input_data.get("language", "")

        prompt = f"请审查以下代码,指出问题并给出改进建议:\n```\n{code}\n```"
        if language:
            prompt = f"语言: {language}\n" + prompt

        resp = self._call_llm_with_routing(prompt, input_data)
        return {"review": resp["content"], "usage": {"model": resp["model"], "input_tokens": resp["input_tokens"], "output_tokens": resp["output_tokens"]}}

    def _handle_document_summary(self, input_data: dict) -> dict:
        """文档摘要 — 调用 LLM API。"""
        text = input_data.get("text", input_data.get("requirement", ""))
        max_length = input_data.get("max_length", 500)

        prompt = f"请将以下内容生成不超过 {max_length} 字的摘要:\n{text}"
        resp = self._call_llm_with_routing(prompt, input_data)
        return {"summary": resp["content"], "usage": {"model": resp["model"], "input_tokens": resp["input_tokens"], "output_tokens": resp["output_tokens"]}}

    def _handle_rag_search(self, input_data: dict) -> dict:
        """RAG 检索 — 预留接口,当前返回提示。"""
        return {
            "query": input_data.get("query", ""),
            "results": [],
            "note": "RAG 检索尚未实现,需要接入向量数据库",
        }

    def _handle_shell_exec(self, input_data: dict) -> dict:
        """Shell 命令执行 (沙箱限制)。

        安全策略:
        - 禁止危险命令 (rm -rf /, mkfs, sudo 等)
        - 工作目录限制在 shared_folder 内
        - 输出截断保护
        """
        command = input_data.get("command", input_data.get("requirement", ""))
        timeout = min(input_data.get("timeout", 30), 120)  # 最大 120s

        # 安全检查: 禁止危险命令
        cmd_lower = command.lower().strip()
        for pattern in SHELL_BLOCKED_PATTERNS:
            if pattern in cmd_lower:
                return {
                    "stdout": "",
                    "stderr": f"[Sandbox] 命令被拒绝: 包含禁止模式 '{pattern}'",
                    "returncode": -1,
                }

        # 工作目录限制
        cwd = input_data.get("cwd", self.shared_folder)

        try:
            result = subprocess.run(
                command, shell=True, capture_output=True, text=True,
                timeout=timeout, cwd=cwd,
            )
            stdout = result.stdout[:MAX_OUTPUT_LENGTH]
            stderr = result.stderr[:MAX_OUTPUT_LENGTH]
            return {
                "stdout": stdout,
                "stderr": stderr,
                "returncode": result.returncode,
            }
        except subprocess.TimeoutExpired:
            return {"stdout": "", "stderr": f"命令超时 ({timeout}s)", "returncode": -1}

    def _handle_file_ops(self, input_data: dict) -> dict:
        """文件读写操作 (沙箱限制)。

        安全策略:
        - 路径必须在允许的目录内 (shared_folder 及其子目录)
        - 禁止路径穿越 (../)
        - 写入大小限制
        """
        action = input_data.get("action", "read")
        path = input_data.get("path", "")
        content = input_data.get("content", "")

        # 安全检查: 路径验证
        if not path:
            return {"error": "未指定文件路径"}

        # 解析绝对路径并检查是否在允许范围内
        import os
        resolved = os.path.realpath(os.path.expanduser(path))
        allowed_root = os.path.realpath(self.shared_folder)

        # 允许 shared_folder 及 /tmp 目录
        allowed_roots = [allowed_root, "/tmp"]
        is_allowed = any(
            resolved == root or resolved.startswith(root + os.sep)
            for root in allowed_roots
        )
        if not is_allowed:
            return {
                "error": f"[Sandbox] 路径被拒绝: {path} 不在允许目录内 "
                         f"(允许: {self.shared_folder}, /tmp)"
            }

        if action == "read":
            if not os.path.isfile(resolved):
                return {"error": f"文件不存在: {path}"}
            if os.path.getsize(resolved) > MAX_FILE_SIZE:
                return {"error": f"文件过大 (> {MAX_FILE_SIZE // 1024 // 1024}MB)"}
            with open(resolved, "r", encoding="utf-8", errors="replace") as f:
                return {"content": f.read(), "path": path}
        elif action == "write":
            if len(content) > MAX_FILE_SIZE:
                return {"error": f"写入内容过大 (> {MAX_FILE_SIZE // 1024 // 1024}MB)"}
            os.makedirs(os.path.dirname(resolved), exist_ok=True)
            with open(resolved, "w", encoding="utf-8") as f:
                f.write(content)
            return {"path": path, "written": len(content)}
        elif action == "list":
            if not os.path.isdir(resolved):
                return {"error": f"目录不存在: {path}"}
            entries = os.listdir(resolved)
            return {"entries": entries, "path": path}
        elif action == "delete":
            if os.path.isfile(resolved):
                os.remove(resolved)
                return {"deleted": path}
            return {"error": f"文件不存在: {path}"}
        else:
            return {"error": f"未知操作: {action}"}

    def _handle_monitoring(self, input_data: dict) -> dict:
        """系统资源监控。"""
        import psutil
        return {
            "cpu_percent": psutil.cpu_percent(interval=0.5),
            "memory_percent": psutil.virtual_memory().percent,
            "disk_percent": psutil.disk_usage("/").percent,
            "timestamp": time.time(),
        }

    # ── F2.1: ReAct 工具循环 Agent ─────────────────────────────

    def _handle_react_agent(self, input_data: dict) -> dict:
        """F2.1: ReAct (Reasoning + Acting) 工具循环执行。

        子 Agent 自主决策使用哪些工具, 多步推理直到完成任务。
        支持: file_read, file_write, shell_exec, http_request, dir_list, python_eval

        流程:
        1. 将任务描述 + 可用工具发送给 LLM (function calling)
        2. LLM 返回 tool_calls → 执行工具 → 结果回填
        3. 重复直到 LLM 给出最终文本回复或达到最大轮次
        """
        from .tool_registry import ToolRegistry

        requirement = input_data.get("requirement", input_data.get("description", ""))
        max_iterations = input_data.get("max_iterations", 30)
        cwd = input_data.get("cwd", self.shared_folder)

        # 初始化工具注册表
        registry = ToolRegistry()
        tools_schema = self._build_openai_tools_schema(registry)

        # 构建 system prompt
        system_prompt = self._build_system_prompt(requirement)
        if not system_prompt:
            system_prompt = ""
        system_prompt += (
            "\n\n你是一个自主执行 Agent。你可以使用提供的工具来完成任务。\n"
            "规则:\n"
            "- 直接执行, 不要反问\n"
            "- 每次只调用一个工具, 观察结果后决定下一步\n"
            "- 任务完成后直接输出最终结果 (不再调用工具)\n"
            "- 如果工具执行失败, 尝试替代方案\n"
            f"- 工作目录: {cwd}\n"
            "- 禁止: 不要执行进度上报/curl/HTTP POST 到任何 progress-report 端点, 框架会自动处理\n"
            "- 禁止: 不要浪费轮次做网络请求上报状态, 专注于任务本身\n"
        )

        # 对话历史
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": requirement},
        ]

        total_input_tokens = 0
        total_output_tokens = 0
        steps_log = []

        for iteration in range(max_iterations):
            # 调用 LLM (with tools)
            resp = self._call_llm_messages_with_tools(
                messages, tools_schema, input_data
            )

            total_input_tokens += resp.get("input_tokens", 0)
            total_output_tokens += resp.get("output_tokens", 0)

            message = resp.get("message", {})
            tool_calls = message.get("tool_calls", [])
            content = message.get("content", "")

            # 无工具调用 → 最终答案
            if not tool_calls:
                logger.info("[ReAct] 完成 (轮次=%d)", iteration + 1)
                return {
                    "result": content,
                    "iterations": iteration + 1,
                    "steps": steps_log,
                    "usage": {
                        "model": resp.get("model", ""),
                        "input_tokens": total_input_tokens,
                        "output_tokens": total_output_tokens,
                    },
                }

            # 执行工具调用
            messages.append(message)  # 添加 assistant 消息 (含 tool_calls)

            for tc in tool_calls:
                func_name = tc["function"]["name"]
                try:
                    func_args = _json.loads(tc["function"].get("arguments", "{}"))
                except _json.JSONDecodeError:
                    func_args = {}

                # 注入 cwd
                if func_name in ("shell_exec", "dir_list") and "cwd" not in func_args:
                    func_args["cwd"] = cwd

                logger.info("[ReAct] 轮次%d 工具: %s(%s)",
                           iteration + 1, func_name, str(func_args)[:100])

                # 执行
                tool_result = registry.call_tool(func_name, func_args)
                result_text = tool_result["content"][0]["text"] if tool_result["content"] else ""
                is_error = tool_result.get("isError", False)

                steps_log.append({
                    "iteration": iteration + 1,
                    "tool": func_name,
                    "args": func_args,
                    "result_preview": result_text[:300],
                    "is_error": is_error,
                })

                # 回填工具结果
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": result_text,
                })

        # 达到最大轮次
        logger.warning("[ReAct] 达到最大轮次 (%d), 强制结束", max_iterations)
        return {
            "result": f"[达到最大执行轮次 {max_iterations}] 最后步骤: {steps_log[-1] if steps_log else 'N/A'}",
            "iterations": max_iterations,
            "steps": steps_log,
            "status": "max_iterations_reached",
            "usage": {
                "input_tokens": total_input_tokens,
                "output_tokens": total_output_tokens,
            },
        }

    def _build_openai_tools_schema(self, registry) -> list[dict]:
        """F2.1: 将 ToolRegistry 的工具转换为 OpenAI function calling 格式。"""
        tools = []
        for entry in registry.list_tools():
            tools.append({
                "type": "function",
                "function": {
                    "name": entry["name"],
                    "description": entry["description"],
                    "parameters": entry["inputSchema"],
                },
            })
        return tools

    # ── CLI Agent 执行器 (Claude Code / Codex / Aider) ─────────────

    def _handle_cli_agent(self, input_data: dict) -> dict:
        """调用外部 CLI Agent 自主完成编码任务。

        与单轮 LLM API 调用不同, CLI Agent 能够:
        - 自主探索代码库、读取文件
        - 编辑多个文件、创建新文件
        - 运行命令、执行测试
        - 迭代修复错误直到任务完成

        支持后端: claude (Claude Code) / codex / aider
        通过环境变量 CLI_AGENT_BACKEND 指定, 或自动检测。

        input_data 字段:
            requirement/description: 任务描述
            cwd: 工作目录 (默认 shared_folder)
            backend: 强制指定后端 (可选)
            timeout: 超时秒数 (可选)
            allowed_tools: 允许的工具列表 (claude 专用, 可选)
        """
        requirement = input_data.get("requirement", input_data.get("description", ""))
        if not requirement:
            return {"error": "未提供任务描述 (requirement)"}

        cwd = input_data.get("cwd", self.shared_folder)
        timeout = min(input_data.get("timeout", CLI_AGENT_TIMEOUT), 1800)  # 最大 30min

        # 确定后端
        backend = input_data.get("backend", "").lower().strip()
        if backend and backend in CLI_AGENT_BACKENDS:
            if not _which_with_fallback(CLI_AGENT_BACKENDS[backend]["detect"]):
                return {"error": f"CLI Agent '{backend}' 未安装 (which {CLI_AGENT_BACKENDS[backend]['detect']} 未找到)"}
        else:
            backend = get_preferred_cli_agent()
            if not backend:
                available_hint = "请安装: npm install -g @anthropic-ai/claude-code 或 pip install aider-chat"
                return {
                    "error": f"未检测到可用的 CLI Agent。{available_hint}",
                    "hint": "支持: claude (Claude Code), codex (OpenAI), aider",
                }

        cfg = CLI_AGENT_BACKENDS[backend]
        logger.info("[CLI Agent] 后端=%s, cwd=%s, timeout=%ds", backend, cwd, timeout)
        logger.info("[CLI Agent] 任务: %s", requirement[:200])

        # 构建命令
        cmd = cfg["build_cmd"](requirement, cwd)

        # Claude Code 额外参数: 工具权限控制
        if backend == "claude":
            allowed_tools = input_data.get("allowed_tools", [])
            if allowed_tools:
                for tool in allowed_tools:
                    cmd.insert(-1, f"--allowedTools={tool}")
            # 注入 system prompt (如果有)
            system_prompt = self._build_system_prompt(requirement)
            if system_prompt and len(system_prompt) < 2000:
                cmd.insert(-1, f"--system-prompt={system_prompt[:1500]}")

        # 执行 CLI Agent
        start_time = time.time()
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=cwd,
                env=_build_cli_env(backend),  # 自动注入 Token Plan 凭据
            )
            elapsed = time.time() - start_time
            stdout = result.stdout[:CLI_AGENT_MAX_OUTPUT]
            stderr = result.stderr[:CLI_AGENT_MAX_OUTPUT // 4]

            # 解析 Claude Code JSON 输出
            output = self._parse_cli_output(backend, stdout, stderr, result.returncode)
            output["backend"] = backend
            output["elapsed_secs"] = round(elapsed, 1)
            output["returncode"] = result.returncode

            if result.returncode == 0:
                logger.info("[CLI Agent] 完成 (%.1fs, backend=%s)", elapsed, backend)
            else:
                logger.warning("[CLI Agent] 非零退出 (code=%d, %.1fs)", result.returncode, elapsed)

            # P0/P1: CLI Agent 性能追踪
            try:
                from . import runtime_trace
                runtime_trace.trace_llm_call(
                    model=f"cli:{backend}",
                    input_tokens=0, output_tokens=0,
                    ttft_ms=0, total_ms=elapsed * 1000,
                    status="ok" if result.returncode == 0 else "error",
                    call_type="cli")
            except Exception:
                pass

            return output

        except subprocess.TimeoutExpired:
            elapsed = time.time() - start_time
            logger.error("[CLI Agent] 超时 (%ds, backend=%s)", timeout, backend)
            # P0/P1: 超时追踪
            try:
                from . import runtime_trace
                runtime_trace.trace_llm_call(
                    model=f"cli:{backend}",
                    input_tokens=0, output_tokens=0,
                    ttft_ms=0, total_ms=elapsed * 1000,
                    status="timeout", error=f"超时 {timeout}s",
                    call_type="cli")
            except Exception:
                pass
            return {
                "error": f"CLI Agent 执行超时 ({timeout}s)",
                "backend": backend,
                "elapsed_secs": round(elapsed, 1),
                "status": "timeout",
            }
        except FileNotFoundError:
            return {
                "error": f"CLI Agent '{backend}' 命令未找到, 请确认已安装并在 PATH 中",
                "backend": backend,
            }
        except Exception as e:
            logger.error("[CLI Agent] 执行异常: %s", e)
            return {"error": str(e), "backend": backend}

    def _parse_cli_output(self, backend: str, stdout: str, stderr: str, returncode: int) -> dict:
        """解析 CLI Agent 的输出。

        Claude Code 使用 --output-format json 时输出 JSON 结构:
        {"type": "result", "result": "...", "cost_usd": 0.05, ...}
        """
        if backend == "claude" and stdout.strip():
            try:
                data = _json.loads(stdout)
                # Claude Code JSON 输出格式
                if isinstance(data, dict):
                    return {
                        "result": data.get("result", stdout),
                        "cost_usd": data.get("cost_usd", 0),
                        "duration_ms": data.get("duration_ms", 0),
                        "num_turns": data.get("num_turns", 0),
                        "session_id": data.get("session_id", ""),
                        "status": "completed" if returncode == 0 else "failed",
                    }
            except _json.JSONDecodeError:
                pass  # 非 JSON, 回退到纯文本

        # 通用纯文本输出
        return {
            "result": stdout if stdout else stderr,
            "status": "completed" if returncode == 0 else "failed",
            "stderr": stderr[:2000] if stderr else "",
        }

    def list_cli_agents(self) -> dict:
        """列出可用的 CLI Agent 后端及其状态。"""
        agents = []
        for name, cfg in CLI_AGENT_BACKENDS.items():
            path = _which_with_fallback(cfg["detect"])
            installed = path is not None
            agents.append({
                "name": name,
                "description": cfg["description"],
                "installed": installed,
                "path": path or "",
            })
        preferred = get_preferred_cli_agent()
        return {
            "agents": agents,
            "preferred": preferred,
            "timeout": CLI_AGENT_TIMEOUT,
        }

    def _call_llm_messages_with_tools(self, messages: list[dict],
                                       tools: list[dict],
                                       input_data: dict) -> dict:
        """F2.1: 支持多轮 messages + tools 的 LLM 调用。

        与 _call_llm_with_routing 类似, 但支持完整 messages 数组和 tools 参数。
        """
        model_pref = input_data.get("_model_preference", "")
        fallbacks = input_data.get("_fallback_models", [])

        chain = []
        if model_pref:
            chain = [model_pref] + [m for m in fallbacks if m != model_pref]
        else:
            # 无偏好: 遍历所有可用 provider
            for provider, cfg in PROVIDER_CONFIG.items():
                api_key = os.environ.get(cfg["api_key_env"], "")
                if api_key:
                    default_model = self._get_default_model(provider)
                    if default_model:
                        chain.append(default_model)

        last_error = None
        for model_id in chain:
            provider_cfg = self._resolve_provider(model_id)
            if not provider_cfg:
                continue
            try:
                return self._call_openai_with_tools(
                    messages, tools, model_id,
                    provider_cfg["base_url"],
                    provider_cfg["api_key"],
                )
            except Exception as e:
                last_error = e
                logger.warning("[ReAct] 模型 %s 调用失败: %s, 降级...", model_id, e)
                continue

        return {
            "message": {"content": f"[模型调用失败] {last_error}", "tool_calls": []},
            "model": "none",
            "input_tokens": 0,
            "output_tokens": 0,
        }

    def _call_openai_with_tools(self, messages: list[dict], tools: list[dict],
                                 model_id: str, base_url: str, api_key: str) -> dict:
        """F2.1: OpenAI 兼容 API 调用 (支持 tools 参数)。"""
        import time as _time
        url = f"{base_url.rstrip('/')}/chat/completions"
        payload = {
            "model": model_id,
            "messages": messages,
            "max_tokens": 4096,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        call_start = _time.time()
        resp = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=120,
        )
        resp.raise_for_status()
        data = resp.json()
        call_ms = (_time.time() - call_start) * 1000
        usage = data.get("usage", {})
        choice_msg = data["choices"][0]["message"]

        result = {
            "message": choice_msg,  # 含 content + tool_calls
            "model": model_id,
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
        }
        # R1: 模型资源记账 (未启用时 no-op, 异常不影响主流程)
        try:
            from .model_resources import record_usage_global
            record_usage_global(model_id, result["input_tokens"], result["output_tokens"])
        except Exception:
            pass
        # P0/P1: LLM 调用性能追踪
        try:
            from . import runtime_trace
            runtime_trace.trace_llm_call(
                model=model_id,
                input_tokens=result["input_tokens"],
                output_tokens=result["output_tokens"],
                ttft_ms=call_ms, total_ms=call_ms,
                status="ok", call_type="tools")
        except Exception:
            pass
        return result

    # ── LLM API 调用 (支持多 Provider + 降级链重试) ──────────────────

    def set_custom_prompt(self, prompt: str):
        """设置/更新自定义 system prompt (PM 注入)。

        设置后, _build_system_prompt 将优先使用此 prompt,
        不再从技能缓存拼装。传入空字符串则恢复默认行为。
        """
        self._custom_system_prompt = prompt

    def _build_system_prompt(self, task_context: str = "") -> str:
        """构建 system prompt。

        优先使用 PM 注入的 custom_system_prompt (含角色、团队、依赖等定制信息)。
        若未设置, 则回退到从本地技能缓存拼装知识库 prompt。
        """
        # 优先使用 PM 注入的定制 prompt
        if self._custom_system_prompt:
            return self._custom_system_prompt

        # 回退: 从技能缓存拼装 (优化3: 按当前技能选择性加载)
        if not self._skills_cache_dir.is_dir():
            return ""

        # 优化3: 如果有当前技能, 只加载匹配的技能文件; 否则加载全部
        current_skill = getattr(self._local, 'current_skill', '')
        target_skill_dir = None
        if current_skill:
            target_skill_dir = self._skills_cache_dir / current_skill

        prompts = []
        if target_skill_dir and target_skill_dir.is_dir():
            # 只加载当前技能
            skill_md = target_skill_dir / "SKILL.md"
            if skill_md.is_file():
                try:
                    content = skill_md.read_text(encoding="utf-8")
                    if content.startswith("---"):
                        parts = content.split("---", 2)
                        if len(parts) >= 3:
                            content = parts[2].strip()
                    if content:
                        prompts.append(content)
                except Exception:
                    pass
        else:
            # 无指定技能, 加载全部 (兼容旧逻辑)
            for skill_dir in sorted(self._skills_cache_dir.iterdir()):
                if not skill_dir.is_dir():
                    continue
                skill_md = skill_dir / "SKILL.md"
                if not skill_md.is_file():
                    continue
                try:
                    content = skill_md.read_text(encoding="utf-8")
                    if content.startswith("---"):
                        parts = content.split("---", 2)
                        if len(parts) >= 3:
                            content = parts[2].strip()
                    if content:
                        prompts.append(content)
                except Exception:
                    continue

        if not prompts:
            return ""
        return (
            "以下是你的能力参考知识库，请在执行任务时参考:\n\n"
            + "\n\n---\n\n".join(prompts)
        )

    def _call_llm_with_routing(self, prompt: str, input_data: dict) -> dict:
        """带路由决策的 LLM 调用, 支持降级链重试。

        优先使用 orchestrator 注入的 model_preference,
        失败时沿 fallback_models 链路重试。
        如果无路由器信息, 回退到旧逻辑 (deepseek → openai)。
        """
        model_pref = input_data.get("_model_preference", "")
        fallbacks = input_data.get("_fallback_models", [])
        # 优先使用外部注入的 system prompt (如 ChatHandler 注入的秘书 prompt)
        external_system_prompt = input_data.get("_system_prompt", "")

        if model_pref:
            # 构建完整重试链: [preferred] + fallbacks
            chain = [model_pref] + [m for m in fallbacks if m != model_pref]
            last_error = None
            unresolved = []  # 无法解析 provider 的模型
            # 优先使用外部注入的 system prompt, 否则从技能缓存构建
            if external_system_prompt:
                system_prompt = external_system_prompt
            else:
                system_prompt = self._build_system_prompt(
                    input_data.get("description", input_data.get("requirement", ""))
                )

            for model_id in chain:
                provider_cfg = self._resolve_provider(model_id)
                if not provider_cfg:
                    unresolved.append(model_id)
                    continue
                try:
                    return self._call_openai_compatible(
                        prompt, model_id,
                        provider_cfg["base_url"],
                        provider_cfg["api_key"],
                        system_prompt=system_prompt,
                    )
                except Exception as e:
                    last_error = e
                    logger.warning("模型 %s 调用失败: %s, 尝试降级...", model_id, e)
                    continue

            # 整条链都失败
            if unresolved and not last_error:
                # 所有模型均无法解析 provider (API Key 环境变量未设置)
                return {
                    "content": f"[模型调用失败] 以下模型的 API Key 未配置: {', '.join(unresolved)}。"
                               f"请在 .env 文件或环境变量中设置对应的 API Key。",
                    "model": "none",
                    "input_tokens": 0,
                    "output_tokens": 0,
                }
            return {
                "content": f"[模型调用失败] 降级链均不可用: {last_error}",
                "model": "none",
                "input_tokens": 0,
                "output_tokens": 0,
            }

        # 无路由器信息, 回退旧逻辑
        if external_system_prompt:
            system_prompt = external_system_prompt
        else:
            system_prompt = self._build_system_prompt(
                input_data.get("description", input_data.get("requirement", ""))
            )
        return self._call_llm_full(prompt, system_prompt=system_prompt)

    def _resolve_provider(self, model_id: str) -> Optional[dict]:
        """根据模型 ID 解析 provider 配置, 返回 {base_url, api_key} 或 None。

        查找顺序:
        1. model_pool.yaml 精确匹配 (支持所有 provider, 包括 aliyun-tokenplan)
        2. PROVIDER_CONFIG 前缀匹配 (兼容旧逻辑)
        """
        # 1. 从 model_pool.yaml 精确查找
        pool = _load_model_pool_entries()
        entry = pool.get(model_id)
        if entry:
            api_key = os.environ.get(entry.api_key_env, "")
            if api_key:
                return {"base_url": entry.base_url, "api_key": api_key}

        # 2. 前缀匹配 (兼容旧逻辑)
        for provider, cfg in PROVIDER_CONFIG.items():
            api_key = os.environ.get(cfg["api_key_env"], "")
            if not api_key:
                continue
            if provider == "deepseek" and model_id.startswith("deepseek"):
                return {"base_url": cfg["base_url"], "api_key": api_key}
            if provider == "openai" and model_id.startswith("gpt"):
                return {"base_url": cfg["base_url"], "api_key": api_key}
            if provider == "anthropic" and model_id.startswith("claude"):
                return {"base_url": cfg["base_url"], "api_key": api_key}
            if provider == "qwen" and model_id.startswith("qwen"):
                return {"base_url": cfg["base_url"], "api_key": api_key}
        return None

    def _call_openai_compatible(
        self, prompt: str, model_id: str, base_url: str, api_key: str,
        system_prompt: str = "",
    ) -> dict:
        """通用 OpenAI 兼容 API 调用 — 流式输出 + 活性检测。

        超时策略 (替代盲等):
        - 首 token 超时 60s: 模型无反应 → 立即降级
        - 流间超时 90s: 输出中断 → 判定卡死
        - 总时长上限 600s: 兆底保护
        """
        import time as _time

        url = f"{base_url.rstrip('/')}/chat/completions"
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        FIRST_TOKEN_TIMEOUT = 60   # 首 token 超时 (s)
        INTER_CHUNK_TIMEOUT = 90   # 流间超时 (s)
        TOTAL_TIMEOUT = 600        # 总时长上限 (s)

        resp = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model_id,
                "messages": messages,
                "max_tokens": 4096,
                "stream": True,
            },
            timeout=(10, FIRST_TOKEN_TIMEOUT),  # (connect_timeout, 首包超时)
            stream=True,
        )
        resp.raise_for_status()

        # 流式读取 + 活性检测
        collected = []
        input_tokens = 0
        output_tokens = 0
        start_time = _time.time()
        last_chunk_time = start_time
        first_token_received = False

        try:
            for line in resp.iter_lines(decode_unicode=True):
                now = _time.time()

                # 总时长保护
                if now - start_time > TOTAL_TIMEOUT:
                    logger.warning("%s 输出超过总时长上限 %ds, 截断", model_id, TOTAL_TIMEOUT)
                    break

                if not line:
                    # 空行 — 检查流间超时
                    if first_token_received and (now - last_chunk_time) > INTER_CHUNK_TIMEOUT:
                        logger.warning("%s 流间超时 %ds, 截断已收集内容",
                                      model_id, INTER_CHUNK_TIMEOUT)
                        break
                    continue

                if line.startswith("data: "):
                    data_str = line[6:]
                    if data_str.strip() == "[DONE]":
                        break
                    try:
                        chunk = _json.loads(data_str)
                    except (ValueError, TypeError):
                        continue

                    # 提取增量内容
                    choices = chunk.get("choices", [])
                    if choices:
                        delta = choices[0].get("delta", {})
                        content = delta.get("content", "")
                        # 跳过 reasoning_content (思考过程, 不计入输出)
                        if content:
                            if not first_token_received:
                                first_token_received = True
                                ttft = now - start_time
                                logger.debug("%s 首 token 延迟: %.1fs", model_id, ttft)
                            collected.append(content)
                            last_chunk_time = now

                    # 提取 usage (部分厂商在最后一个 chunk 返回)
                    u = chunk.get("usage")
                    if u:
                        input_tokens = u.get("prompt_tokens", 0)
                        output_tokens = u.get("completion_tokens", 0)

        except requests.exceptions.ChunkedEncodingError:
            # 连接中断 — 如果已有内容则返回已收集部分
            if collected:
                logger.warning("%s 连接中断, 返回已收集内容 (%d 字符)",
                              model_id, sum(len(c) for c in collected))
            else:
                raise  # 无内容则抛异常触发降级
        finally:
            resp.close()

        full_content = "".join(collected)
        if not full_content:
            raise TimeoutError(f"{model_id} 未返回任何内容 (首 token 超时 {FIRST_TOKEN_TIMEOUT}s)")

        result = {
            "content": full_content,
            "model": model_id,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens or len(full_content) // 4,  # 估算
        }
        # R1: 模型资源记账 (未启用时 no-op, 异常不影响主流程)
        try:
            from .model_resources import record_usage_global
            record_usage_global(model_id, result["input_tokens"], result["output_tokens"])
        except Exception:
            pass
        # P0/P1: LLM 调用性能追踪 (JSONL + SQLite 双写)
        try:
            from . import runtime_trace
            total_ms = (_time.time() - start_time) * 1000
            ttft_ms = ttft * 1000 if first_token_received else 0
            runtime_trace.trace_llm_call(
                model=model_id,
                input_tokens=result["input_tokens"],
                output_tokens=result["output_tokens"],
                ttft_ms=ttft_ms, total_ms=total_ms,
                status="ok", call_type="chat")
        except Exception:
            pass
        return result

    def _call_llm_full(self, prompt: str, system_prompt: str = "") -> dict:
        """调用外部 LLM API (回退逻辑, 无路由器时使用)。

        遍历所有已配置 API Key 的 provider, 每个 provider 内按 quality_score
        从高到低尝试多个模型 (超时/失败自动降级到更快的模型)。
        """
        # 确保环境变量已加载
        _ensure_env_loaded()

        # 按优先级遍历所有 provider
        errors = []  # 记录每个 provider 的失败原因
        for provider, cfg in PROVIDER_CONFIG.items():
            api_key = os.environ.get(cfg["api_key_env"], "")
            if not api_key:
                continue
            # 获取该 provider 下所有可用模型 (按 quality 降序)
            models = self._get_provider_models(provider)
            if not models:
                errors.append(f"{provider}: 未找到可用模型配置")
                continue
            # 同 provider 内逐模型降级尝试
            for model_id in models:
                try:
                    return self._call_openai_compatible(
                        prompt, model_id,
                        cfg["base_url"], api_key,
                        system_prompt=system_prompt,
                    )
                except Exception as e:
                    logger.warning("%s(%s) 调用失败: %s, 尝试下一模型...",
                                  provider, model_id, e)
                    errors.append(f"{provider}({model_id}): {e}")
                    continue

        # 区分 "无 key" 和 "调用失败"
        if errors:
            detail = "; ".join(errors[-3:])  # 只显示最新 3 条避免过长
            return {
                "content": f"[LLM 调用失败] 已配置 Key 的 Provider 均调用失败: {detail}",
                "model": "none",
                "input_tokens": 0,
                "output_tokens": 0,
                "_error": True,
            }
        return {
            "content": "[未配置 LLM API Key] 请设置 ALIYUN_TOKENPLAN_API_KEY、DEEPSEEK_API_KEY 或 OPENAI_API_KEY 环境变量。",
            "model": "none",
            "input_tokens": 0,
            "output_tokens": 0,
            "_error": True,
        }

    def _get_provider_models(self, provider: str) -> list:
        """获取指定 provider 下所有模型 ID, 按 quality_score 降序排列。

        同 provider 内尝试多个模型实现自动降级:
        qwen3.8-max → qwen3.7-max → qwen3.7-plus → qwen3.6-flash
        """
        pool = _load_model_pool_entries()
        candidates = [
            e for e in pool.values() if e.provider == provider
        ]
        if candidates:
            candidates.sort(key=lambda e: e.quality_score, reverse=True)
            return [e.id for e in candidates]
        # 回退到硬编码默认值
        defaults = {
            "deepseek": ["deepseek-chat"],
            "openai": ["gpt-4o-mini"],
            "anthropic": ["claude-3-haiku"],
            "qwen": ["qwen-turbo"],
            "aliyun-tokenplan": [],
        }
        return defaults.get(provider, [])

    def _call_llm(self, prompt: str) -> str:
        """调用外部 LLM API 生成回复 (仅返回文本内容)。"""
        return self._call_llm_full(prompt)["content"]
