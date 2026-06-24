"""
Agent 运行时 — Worker 端任务执行引擎

职责:
1. 接收 Master 分发的子任务
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
from typing import Optional

import requests


class AgentRuntime:
    """Worker 端 Agent 运行时。

    接收子任务,根据 required_skill 路由到对应的执行处理器。
    """

    def __init__(self, agent_id: str, shared_folder_path: str):
        self.agent_id = agent_id
        self.shared_folder = shared_folder_path
        self._handlers = {
            "code_generation": self._handle_code_generation,
            "code_review": self._handle_code_review,
            "document_summary": self._handle_document_summary,
            "rag_search": self._handle_rag_search,
            "shell_exec": self._handle_shell_exec,
            "file_ops": self._handle_file_ops,
            "monitoring": self._handle_monitoring,
        }

    def execute(self, subtask: dict) -> dict:
        """执行子任务,返回结果字典。

        Args:
            subtask: 包含 name, description, required_skill, input_data 的字典

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

        try:
            result = handler(subtask.get("input_data", {}))
            return {"output": result, "status": "completed"}
        except Exception as e:
            return {"output": {}, "status": "failed", "error": str(e)}

    # ── 技能处理器 ──────────────────────────────────────────────

    def _handle_code_generation(self, input_data: dict) -> dict:
        """代码生成 — 调用 LLM API。"""
        requirement = input_data.get("requirement", input_data.get("description", ""))
        language = input_data.get("language", "python")
        context = input_data.get("context", "")

        prompt = f"请用 {language} 编写以下需求的代码:\n{requirement}"
        if context:
            prompt += f"\n\n已有代码/上下文:\n{context}"

        result = self._call_llm(prompt)
        return {"code": result, "language": language}

    def _handle_code_review(self, input_data: dict) -> dict:
        """代码审查 — 调用 LLM API。"""
        code = input_data.get("code", input_data.get("requirement", ""))
        language = input_data.get("language", "")

        prompt = f"请审查以下代码,指出问题并给出改进建议:\n```\n{code}\n```"
        if language:
            prompt = f"语言: {language}\n" + prompt

        result = self._call_llm(prompt)
        return {"review": result}

    def _handle_document_summary(self, input_data: dict) -> dict:
        """文档摘要 — 调用 LLM API。"""
        text = input_data.get("text", input_data.get("requirement", ""))
        max_length = input_data.get("max_length", 500)

        prompt = f"请将以下内容生成不超过 {max_length} 字的摘要:\n{text}"
        result = self._call_llm(prompt)
        return {"summary": result}

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

    # ── LLM API 调用 ────────────────────────────────────────────

    def _call_llm(self, prompt: str) -> str:
        """调用外部 LLM API 生成回复。

        优先使用 DeepSeek (成本低),其次 OpenAI。
        如果没有配置 API Key,返回提示信息。
        """
        # DeepSeek API
        deepseek_key = os.environ.get("DEEPSEEK_API_KEY")
        if deepseek_key:
            return self._call_deepseek(prompt, deepseek_key)

        # OpenAI API
        openai_key = os.environ.get("OPENAI_API_KEY")
        if openai_key:
            return self._call_openai(prompt, openai_key)

        return "[未配置 LLM API Key] 请设置 DEEPSEEK_API_KEY 或 OPENAI_API_KEY 环境变量。"

    def _call_deepseek(self, prompt: str, api_key: str) -> str:
        """调用 DeepSeek API。"""
        resp = requests.post(
            "https://api.deepseek.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": "deepseek-chat",
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 4096,
            },
            timeout=120,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]

    def _call_openai(self, prompt: str, api_key: str) -> str:
        """调用 OpenAI API。"""
        resp = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": "gpt-4o-mini",
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 4096,
            },
            timeout=120,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]
