"""
工具注册表 — 插件化工具管理系统

参考 Anthropic MCP 的 Tool 概念,提供:
1. 内置工具 (file_read, file_write, shell_exec, http_request)
2. 配置文件加载的插件工具 (YAML)
3. 运行时动态注册
4. 工具执行调度

每个工具包含:
- name: 唯一标识
- description: 自然语言描述
- input_schema: JSON Schema 描述输入参数
- handler: 可调用的执行函数
"""
import importlib
import os
import subprocess
from pathlib import Path
from typing import Any, Callable, Optional

import yaml

from .protocol import ToolDef


# ── 工具执行处理器 ──────────────────────────────────────────────

def _tool_file_read(params: dict) -> dict:
    """读取文件内容。"""
    path = params.get("path", "")
    encoding = params.get("encoding", "utf-8")
    with open(path, "r", encoding=encoding) as f:
        return {"content": f.read(), "path": path}


def _tool_file_write(params: dict) -> dict:
    """写入文件内容。"""
    path = params.get("path", "")
    content = params.get("content", "")
    encoding = params.get("encoding", "utf-8")
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding=encoding) as f:
        f.write(content)
    return {"path": path, "written": len(content)}


def _tool_shell_exec(params: dict) -> dict:
    """执行 Shell 命令。"""
    command = params.get("command", "")
    timeout = params.get("timeout", 30)
    cwd = params.get("cwd")
    try:
        result = subprocess.run(
            command, shell=True, capture_output=True, text=True,
            timeout=timeout, cwd=cwd,
        )
        return {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "returncode": result.returncode,
        }
    except subprocess.TimeoutExpired:
        return {"stdout": "", "stderr": f"命令超时 ({timeout}s)", "returncode": -1}


def _tool_http_request(params: dict) -> dict:
    """发起 HTTP 请求。"""
    import requests as req
    url = params.get("url", "")
    method = params.get("method", "GET").upper()
    headers = params.get("headers", {})
    body = params.get("body", "")
    timeout = params.get("timeout", 30)

    resp = req.request(
        method, url, headers=headers, data=body if body else None, timeout=timeout
    )
    return {
        "status_code": resp.status_code,
        "headers": dict(resp.headers),
        "body": resp.text[:10000],  # 限制返回大小
    }


def _tool_dir_list(params: dict) -> dict:
    """列出目录内容。"""
    path = params.get("path", ".")
    pattern = params.get("pattern", "*")
    entries = []
    for entry in sorted(Path(path).glob(pattern)):
        entries.append({
            "name": entry.name,
            "path": str(entry),
            "is_dir": entry.is_dir(),
            "size": entry.stat().st_size if entry.is_file() else 0,
        })
    return {"entries": entries, "path": path}


def _tool_python_eval(params: dict) -> dict:
    """执行 Python 表达式 (受限沙箱)。"""
    code = params.get("code", "")
    try:
        local_ns = {}
        exec(code, {"__builtins__": {}}, local_ns)
        return {"result": str(local_ns), "success": True}
    except Exception as e:
        return {"result": str(e), "success": False}


# ── 内置工具定义 ────────────────────────────────────────────────

BUILTIN_TOOLS = {
    "file_read": {
        "tool": ToolDef(
            name="file_read",
            description="读取指定路径的文件内容",
            mcp_compatible=True,
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件路径"},
                    "encoding": {"type": "string", "description": "文件编码,默认 utf-8"},
                },
                "required": ["path"],
            },
        ),
        "handler": _tool_file_read,
    },
    "file_write": {
        "tool": ToolDef(
            name="file_write",
            description="将内容写入指定文件",
            mcp_compatible=True,
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件路径"},
                    "content": {"type": "string", "description": "写入内容"},
                    "encoding": {"type": "string", "description": "文件编码,默认 utf-8"},
                },
                "required": ["path", "content"],
            },
        ),
        "handler": _tool_file_write,
    },
    "shell_exec": {
        "tool": ToolDef(
            name="shell_exec",
            description="执行 Shell 命令并返回输出",
            mcp_compatible=True,
            input_schema={
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Shell 命令"},
                    "timeout": {"type": "integer", "description": "超时秒数,默认 30"},
                    "cwd": {"type": "string", "description": "工作目录"},
                },
                "required": ["command"],
            },
        ),
        "handler": _tool_shell_exec,
    },
    "http_request": {
        "tool": ToolDef(
            name="http_request",
            description="发起 HTTP 请求并返回响应",
            mcp_compatible=True,
            input_schema={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "请求 URL"},
                    "method": {"type": "string", "description": "HTTP 方法,默认 GET"},
                    "headers": {"type": "object", "description": "请求头"},
                    "body": {"type": "string", "description": "请求体"},
                    "timeout": {"type": "integer", "description": "超时秒数,默认 30"},
                },
                "required": ["url"],
            },
        ),
        "handler": _tool_http_request,
    },
    "dir_list": {
        "tool": ToolDef(
            name="dir_list",
            description="列出目录内容",
            mcp_compatible=True,
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "目录路径,默认当前目录"},
                    "pattern": {"type": "string", "description": "glob 匹配模式,默认 *"},
                },
            },
        ),
        "handler": _tool_dir_list,
    },
    "python_eval": {
        "tool": ToolDef(
            name="python_eval",
            description="在受限沙箱中执行 Python 代码 (无内置函数)",
            mcp_compatible=True,
            input_schema={
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "Python 代码"},
                },
                "required": ["code"],
            },
        ),
        "handler": _tool_python_eval,
    },
}


class ToolRegistry:
    """工具注册表 — 管理工具的注册、查询和执行。

    支持三种注册方式:
    1. 内置工具 (BUILTIN_TOOLS)
    2. 配置文件加载 (YAML 插件定义)
    3. 运行时动态注册 (register_tool)
    """

    def __init__(self):
        self._tools: dict[str, dict] = {}
        # 注册内置工具
        for name, entry in BUILTIN_TOOLS.items():
            self._tools[name] = entry.copy()

    def register_tool(self, tool: ToolDef, handler: Callable):
        """动态注册一个工具。"""
        self._tools[tool.name] = {"tool": tool, "handler": handler}

    def unregister_tool(self, name: str):
        """注销工具。"""
        self._tools.pop(name, None)

    def list_tools(self) -> list[dict]:
        """列出所有已注册工具 (MCP tools/list 格式)。"""
        return [
            {
                "name": entry["tool"].name,
                "description": entry["tool"].description,
                "inputSchema": entry["tool"].input_schema,
            }
            for entry in self._tools.values()
        ]

    def list_tool_defs(self) -> list[ToolDef]:
        """返回所有 ToolDef 对象。"""
        return [entry["tool"] for entry in self._tools.values()]

    def get_tool(self, name: str) -> Optional[dict]:
        """获取单个工具定义。"""
        return self._tools.get(name)

    def call_tool(self, name: str, params: dict) -> dict:
        """执行工具调用 (MCP tools/call)。

        Returns:
            {"content": [...], "isError": bool}
            content 遵循 MCP 格式: [{"type": "text", "text": "..."}]
        """
        entry = self._tools.get(name)
        if not entry:
            return {
                "content": [{"type": "text", "text": f"工具不存在: {name}"}],
                "isError": True,
            }

        try:
            result = entry["handler"](params)
            import json
            return {
                "content": [{
                    "type": "text",
                    "text": json.dumps(result, ensure_ascii=False, default=str),
                }],
                "isError": False,
                "result": result,
            }
        except Exception as e:
            return {
                "content": [{"type": "text", "text": f"工具执行错误: {e}"}],
                "isError": True,
            }

    def load_plugins(self, config_path: str):
        """从 YAML 配置文件加载插件工具。

        配置格式:
            tools:
              - name: my_custom_tool
                description: 自定义工具
                module: my_package.my_module
                function: my_handler
                input_schema:
                  type: object
                  properties:
                    param1:
                      type: string
                  required: [param1]
        """
        p = Path(config_path).expanduser()
        if not p.is_file():
            return

        with open(p, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f) or {}

        for item in config.get("tools", []):
            name = item.get("name", "")
            if not name or name in self._tools:
                continue
            module_name = item.get("module", "")
            func_name = item.get("function", "")
            if not module_name or not func_name:
                continue
            try:
                mod = importlib.import_module(module_name)
                handler = getattr(mod, func_name)
                tool = ToolDef(
                    name=name,
                    description=item.get("description", ""),
                    mcp_compatible=True,
                    input_schema=item.get("input_schema", {}),
                )
                self.register_tool(tool, handler)
                print(f"[ToolRegistry] 插件工具已加载: {name} ({module_name}.{func_name})")
            except (ImportError, AttributeError) as e:
                print(f"[ToolRegistry] 插件加载失败: {name} → {e}")

    def tool_count(self) -> int:
        """返回已注册工具数量。"""
        return len(self._tools)
