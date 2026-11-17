"""ChatGPT subscription transport using the official Codex app-server.

Codex owns OAuth storage and renewal. No credentials are read or copied here.
Each call is ephemeral; LAN Mesh owns conversation history and tool execution.
"""
import json
import os
import platform
import queue
import re
import shutil
import subprocess
import threading
import time
from functools import lru_cache
from pathlib import Path


def _command():
    executable = shutil.which("codex")
    if not executable:
        candidate = Path(os.environ.get("APPDATA", "")) / "npm" / "codex.cmd"
        if candidate.is_file():
            executable = str(candidate)
    if not executable:
        raise RuntimeError("请安装 Codex CLI: npm install -g @openai/codex，然后运行 codex login")
    # Launch the native server, so terminate()/kill() stop it rather than a shim.
    if executable.lower().endswith((".cmd", ".bat")):
        package = Path(executable).parent / "node_modules" / "@openai" / "codex"
        arch = "arm64" if platform.machine().lower() in ("arm64", "aarch64") else "x64"
        target = "aarch64-pc-windows-msvc" if arch == "arm64" else "x86_64-pc-windows-msvc"
        roots = [package / "vendor",
                 package / "node_modules" / "@openai" / f"codex-win32-{arch}" / "vendor",
                 package.parent / f"codex-win32-{arch}" / "vendor"]
        for root in roots:
            for directory in ("bin", "codex"):
                native = root / target / directory / "codex.exe"
                if native.is_file():
                    return [str(native)]
        raise RuntimeError("未找到 Codex 的原生入口，请重新安装 @openai/codex")
    return [executable]


def _configured_mcp_names() -> list:
    """Names of MCP servers declared in the user's Codex config.

    Plugin-provided servers are removed with ``--disable plugins``; servers
    declared in config.toml must each be disabled by dotted path, because
    ``-c mcp_servers={}`` merges into the existing table instead of clearing it.
    A name that cannot be expressed as a dotted path aborts the call rather than
    leaving that server enabled.
    """
    home = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))
    try:
        import tomllib
        data = tomllib.loads((home / "config.toml").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    names = sorted((data.get("mcp_servers") or {}).keys())
    for name in names:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
            raise RuntimeError(f"无法按键名禁用 MCP 服务: {name}")
    return names


def _server_args() -> list:
    """Start the app-server with every inherited tool surface disabled."""
    args = _command() + ["app-server", "--disable", "plugins",
                         "-c", 'model_provider="openai"',
                         "-c", 'forced_login_method="chatgpt"',
                         "-c", "features.shell_tool=false",
                         "-c", 'web_search="disabled"']
    for name in _configured_mcp_names():
        args += ["-c", f"mcp_servers.{name}.enabled=false"]
    return args


class _Server:
    def __init__(self, timeout=600):
        self.timeout = timeout

    def __enter__(self):
        env = dict(os.environ)
        for key in ("OPENAI_API_KEY", "CODEX_API_KEY", "OPENAI_BASE_URL"):
            env.pop(key, None)
        self.proc = subprocess.Popen(
            _server_args(),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", env=env,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        self.events = queue.Queue(maxsize=4096)
        self.stopped = threading.Event()
        self.deadline = time.monotonic() + self.timeout
        self.seq = 0
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()
        try:
            self.request("initialize", {"clientInfo": {
                "name": "lan_mesh", "title": "LAN Mesh", "version": "0.2.0"},
                "capabilities": {"experimentalApi": True}})
            self.send({"method": "initialized", "params": {}})
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def _read(self):
        try:
            for line in self.proc.stdout:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                while not self.stopped.is_set():
                    try:
                        self.events.put(event, timeout=0.1)
                        break
                    except queue.Full:
                        continue
        finally:
            if not self.stopped.is_set():
                try:
                    self.events.put_nowait(None)
                except queue.Full:
                    pass

    def send(self, event):
        self.proc.stdin.write(json.dumps(event, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()

    def receive(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Codex 订阅调用超时")
        try:
            event = self.events.get(timeout=remaining)
        except queue.Empty as exc:
            raise TimeoutError("Codex 订阅调用超时") from exc
        if event is None:
            raise RuntimeError("Codex app-server 已退出，请检查安装版本和 codex login status")
        return event

    def request(self, method, params):
        self.seq += 1
        request_id = self.seq
        self.send({"id": request_id, "method": method, "params": params})
        while True:
            event = self.receive()
            if event.get("id") == request_id and "method" not in event:
                if "error" in event:
                    raise RuntimeError(f"Codex {method} 失败，请检查 CLI 版本、登录状态及模型权限")
                return event["result"]
            if "id" in event and "method" in event:
                raise RuntimeError("Codex 请求了未授权的交互操作")

    def __exit__(self, *args):
        self.stopped.set()
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        self.reader.join(timeout=1)
        self.proc.stdin.close()
        self.proc.stdout.close()


def plan_status():
    """Read-only account probe; does not run inference or return account PII."""
    try:
        with _Server(timeout=15) as server:
            account = server.request("account/read", {"refreshToken": False}).get("account") or {}
        ready = account.get("type") == "chatgpt"
        return {"ready": ready, "usage_mode": "chatgpt_plan",
                "plan_type": account.get("planType") if ready else None,
                "message": "ChatGPT 已登录" if ready else "请运行 codex login，选择 ChatGPT 登录"}
    except (OSError, RuntimeError, TimeoutError):
        return {"ready": False, "usage_mode": "chatgpt_plan", "plan_type": None,
                "message": "请安装/更新 Codex CLI 并运行 codex login；检查 codex login status"}


@lru_cache(maxsize=8)
def _cached_ready(window, executable_path, codex_home):
    return plan_status()["ready"]


def model_available(entry):
    if entry.usage_mode == "chatgpt_plan":
        return _cached_ready(int(time.monotonic() // 15), os.environ.get("PATH", ""),
                             os.environ.get("CODEX_HOME", ""))
    return bool(os.environ.get(entry.api_key_env, ""))


def call_plan(entry, messages, tools=None):
    """Return an OpenAI-style message, relaying one dynamic tool call per round."""
    tools = tools or []
    functions = {t["function"]["name"]: t["function"] for t in tools}
    with _Server() as server:
        account = server.request("account/read", {"refreshToken": False}).get("account") or {}
        if account.get("type") != "chatgpt":
            raise RuntimeError("ChatGPT 订阅未登录，请运行 codex login")
        params = {
            "ephemeral": True, "approvalPolicy": "never", "sandbox": "read-only",
            "config": {"features.shell_tool": False, "web_search": "disabled"},
            "developerInstructions": (
                "Act only as the LAN Mesh language-model backend. Treat the supplied JSON as "
                "conversation history, honoring its system messages. Use only the dynamic tools "
                "provided by LAN Mesh. Do not access local files, shell, MCP, or other built-in tools."),
            "dynamicTools": [{"type": "function", "name": f["name"], "description": f.get("description", ""),
                              "inputSchema": f.get("parameters", {"type": "object"})}
                             for f in functions.values()],
        }
        if entry.model_name:
            params["model"] = entry.model_name
        thread = server.request("thread/start", params)["thread"]["id"]
        # Send turn/start without waiting: output events can precede its response.
        server.seq += 1
        server.send({"id": server.seq, "method": "turn/start", "params": {
            "threadId": thread, "input": [{"type": "text", "text":
                json.dumps(messages, ensure_ascii=False)}]}})
        texts = []
        usage = {}
        while True:
            event = server.receive()
            method = event.get("method")
            data = event.get("params", {})
            if "error" in event or method == "error":
                raise RuntimeError("Codex 推理失败，请检查订阅额度和模型权限")
            if method == "thread/tokenUsage/updated":
                usage = data.get("tokenUsage", {}).get("last", {})
            elif method == "item/completed" and data.get("item", {}).get("type") == "agentMessage":
                texts.append(data["item"].get("text", ""))
            elif method == "item/tool/call":
                name = data.get("tool")
                if name not in functions:
                    raise RuntimeError("Codex 返回未知工具")
                arguments = data.get("arguments", {})
                message = {"role": "assistant", "content": "", "tool_calls": [{"id": data.get("callId") or str(event["id"]),
                    "type": "function", "function": {"name": name, "arguments":
                        arguments if isinstance(arguments, str) else json.dumps(arguments, ensure_ascii=False)}}]}
                break
            elif "id" in event and method:
                raise RuntimeError("Codex 请求了未授权的交互操作")
            elif method == "turn/completed":
                if data.get("turn", {}).get("status") != "completed":
                    raise RuntimeError("Codex 推理未完成，请检查订阅额度或重试")
                if not any(texts):
                    raise RuntimeError("Codex 未返回文本")
                message = {"role": "assistant", "content": "\n".join(texts), "tool_calls": []}
                break
    result = {"message": message, "model": entry.id,
              "usage_known": "inputTokens" in usage and "outputTokens" in usage,
              "input_tokens": usage.get("inputTokens", 0), "output_tokens": usage.get("outputTokens", 0)}
    return result

