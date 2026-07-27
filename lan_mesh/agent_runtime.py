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
import subprocess
import time
import json as _json
from pathlib import Path
from typing import Optional

import requests

from .logger import get_logger

logger = get_logger("agent_runtime")


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


class AgentRuntime:
    """Worker 端 Agent 运行时。

    接收子任务,根据 required_skill 路由到对应的执行处理器。
    """

    def __init__(self, agent_id: str, shared_folder_path: str,
                 custom_system_prompt: str = ""):
        self.agent_id = agent_id
        self.shared_folder = shared_folder_path
        self._custom_system_prompt = custom_system_prompt  # PM 注入的定制 prompt
        self._current_skill = ""  # 优化3: 当前执行的技能类型 (供选择性加载)
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
        skill = subtask.get("required_skill", "")
        handler = self._handlers.get(skill)

        if not handler:
            return {
                "output": {},
                "status": "failed",
                "error": f"未知的技能类型: {skill}",
            }

        # 优化3: 记录当前技能类型, 供 _build_system_prompt 选择性加载
        self._current_skill = skill

        # 提取路由器注入的模型偏好 (由 orchestrator 写入 payload)
        input_data = dict(subtask.get("input_data", {}))
        input_data["_model_preference"] = subtask.get("model_preference", "")
        input_data["_fallback_models"] = subtask.get("fallback_models", [])

        try:
            result = handler(input_data)
            # 提取 LLM 调用的 token 用量 (如果 handler 返回了 usage)
            usage = {}
            if isinstance(result, dict) and "usage" in result:
                usage = result.pop("usage", {})
            return {"output": result, "status": "completed", "usage": usage}
        except Exception as e:
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
        """Shell 命令执行。"""
        command = input_data.get("command", input_data.get("requirement", ""))
        timeout = input_data.get("timeout", 30)

        try:
            result = subprocess.run(
                command, shell=True, capture_output=True, text=True, timeout=timeout
            )
            return {
                "stdout": result.stdout,
                "stderr": result.stderr,
                "returncode": result.returncode,
            }
        except subprocess.TimeoutExpired:
            return {"stdout": "", "stderr": f"命令超时 ({timeout}s)", "returncode": -1}

    def _handle_file_ops(self, input_data: dict) -> dict:
        """文件读写操作。"""
        action = input_data.get("action", "read")
        path = input_data.get("path", "")
        content = input_data.get("content", "")

        if action == "read":
            with open(path, "r", encoding="utf-8") as f:
                return {"content": f.read(), "path": path}
        elif action == "write":
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
            return {"path": path, "written": len(content)}
        elif action == "list":
            import os
            entries = os.listdir(path)
            return {"entries": entries, "path": path}
        elif action == "delete":
            os.remove(path)
            return {"deleted": path}
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
        max_iterations = input_data.get("max_iterations", 10)
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
        url = f"{base_url.rstrip('/')}/chat/completions"
        payload = {
            "model": model_id,
            "messages": messages,
            "max_tokens": 4096,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

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
        usage = data.get("usage", {})
        choice_msg = data["choices"][0]["message"]

        return {
            "message": choice_msg,  # 含 content + tool_calls
            "model": model_id,
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
        }

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
        target_skill_dir = None
        if self._current_skill:
            target_skill_dir = self._skills_cache_dir / self._current_skill

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
                    print(f"[AgentRuntime] 模型 {model_id} 调用失败: {e}, 尝试降级...")
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
        """通用 OpenAI 兼容 API 调用 — 支持所有厂商 (DeepSeek/OpenAI/Qwen/...)。"""
        url = f"{base_url.rstrip('/')}/chat/completions"
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})
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
            },
            timeout=120,
        )
        resp.raise_for_status()
        data = resp.json()
        usage = data.get("usage", {})
        return {
            "content": data["choices"][0]["message"]["content"],
            "model": model_id,
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
        }

    def _call_llm_full(self, prompt: str, system_prompt: str = "") -> dict:
        """调用外部 LLM API (回退逻辑, 无路由器时使用)。

        遍历所有已配置 API Key 的 provider, 依次尝试:
        DeepSeek → OpenAI → 阿里云 Token Plan → Anthropic → 通义千问。
        """
        # 按优先级遍历所有 provider
        for provider, cfg in PROVIDER_CONFIG.items():
            api_key = os.environ.get(cfg["api_key_env"], "")
            if not api_key:
                continue
            # 选择该 provider 下的默认模型
            default_model = self._get_default_model(provider)
            if not default_model:
                continue
            try:
                return self._call_openai_compatible(
                    prompt, default_model,
                    cfg["base_url"], api_key,
                    system_prompt=system_prompt,
                )
            except Exception as e:
                print(f"[AgentRuntime] {provider} 调用失败: {e}, 尝试下一个 provider...")
                continue

        return {
            "content": "[未配置 LLM API Key] 请设置 ALIYUN_TOKENPLAN_API_KEY、DEEPSEEK_API_KEY 或 OPENAI_API_KEY 环境变量。",
            "model": "none",
            "input_tokens": 0,
            "output_tokens": 0,
        }

    def _get_default_model(self, provider: str) -> Optional[str]:
        """获取指定 provider 的默认模型 ID (从 model_pool.yaml 查找)。"""
        pool = _load_model_pool_entries()
        # 找该 provider 下 quality_score 最高的模型
        candidates = [
            e for e in pool.values() if e.provider == provider
        ]
        if candidates:
            best = max(candidates, key=lambda e: e.quality_score)
            return best.id
        # 回退到硬编码默认值
        defaults = {
            "deepseek": "deepseek-chat",
            "openai": "gpt-4o-mini",
            "anthropic": "claude-3-haiku",
            "qwen": "qwen-turbo",
            "aliyun-tokenplan": None,  # 必须从 model_pool 查找
        }
        return defaults.get(provider)

    def _call_llm(self, prompt: str) -> str:
        """调用外部 LLM API 生成回复 (仅返回文本内容)。"""
        return self._call_llm_full(prompt)["content"]
