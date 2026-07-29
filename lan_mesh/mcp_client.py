"""
MCP 客户端 — 轻量级 JSON-RPC 2.0 客户端

支持两种传输方式:
1. stdio — 启动本地子进程 (如 npx @modelcontextprotocol/server-filesystem)
2. HTTP — 连接远程 MCP Server (HTTP + JSON-RPC)

协议遵循 MCP 规范:
- initialize: 握手
- tools/list: 列出工具
- tools/call: 调用工具
"""
import json
import subprocess
import threading
import time
from typing import Optional

import requests

from .logger import get_logger

logger = get_logger("mcp_client")


class MCPStdioClient:
    """stdio 传输 — 启动本地 MCP Server 子进程并通过 stdin/stdout 通信。

    适用于: npx @modelcontextprotocol/server-filesystem 等本地工具。
    """

    def __init__(self, name: str, command: str, args: list = None, env: dict = None):
        self.name = name
        self.command = command
        self.args = args or []
        self.env = env or {}
        self._process: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._msg_id = 0
        self._initialized = False

    def connect(self) -> bool:
        """启动子进程并发送 initialize 握手。"""
        import os
        full_env = {**os.environ, **self.env}
        try:
            self._process = subprocess.Popen(
                [self.command] + self.args,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=full_env,
                text=True,
                bufsize=1,
            )
        except FileNotFoundError as e:
            logger.error("[MCP:%s] 命令不存在: %s — %s", self.name, self.command, e)
            return False

        # 发送 initialize
        resp = self._request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "lan-mesh-gateway", "version": "0.1.0"},
        })
        if resp is None:
            logger.error("[MCP:%s] initialize 握手失败", self.name)
            return False

        # 发送 initialized 通知
        self._notify("notifications/initialized", {})
        self._initialized = True
        logger.info("[MCP:%s] 已连接 (stdio)", self.name)
        return True

    def list_tools(self) -> list:
        """获取工具列表。"""
        resp = self._request("tools/list", {})
        if resp and "result" in resp:
            return resp["result"].get("tools", [])
        return []

    def call_tool(self, tool_name: str, arguments: dict) -> dict:
        """调用工具。"""
        resp = self._request("tools/call", {
            "name": tool_name,
            "arguments": arguments,
        })
        if resp and "result" in resp:
            return resp["result"]
        if resp and "error" in resp:
            return {"isError": True, "content": [{"type": "text", "text": str(resp["error"])}]}
        return {"isError": True, "content": [{"type": "text", "text": "无响应"}]}

    def is_connected(self) -> bool:
        return self._initialized and self._process and self._process.poll() is None

    def disconnect(self):
        if self._process:
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
            self._process = None
        self._initialized = False

    def _request(self, method: str, params: dict) -> Optional[dict]:
        """发送 JSON-RPC 请求并等待响应。"""
        with self._lock:
            if not self._process or self._process.poll() is not None:
                return None
            self._msg_id += 1
            msg = {
                "jsonrpc": "2.0",
                "id": self._msg_id,
                "method": method,
                "params": params,
            }
            try:
                self._process.stdin.write(json.dumps(msg) + "\n")
                self._process.stdin.flush()
                # 读取响应行 (跳过通知消息)
                while True:
                    line = self._process.stdout.readline()
                    if not line:
                        return None
                    line = line.strip()
                    if not line:
                        continue
                    data = json.loads(line)
                    # 只返回匹配 id 的响应,跳过通知
                    if data.get("id") == self._msg_id:
                        return data
            except (BrokenPipeError, json.JSONDecodeError, OSError):
                return None

    def _notify(self, method: str, params: dict):
        """发送 JSON-RPC 通知 (无 id,不需要响应)。"""
        with self._lock:
            if not self._process or self._process.poll() is not None:
                return
            msg = {"jsonrpc": "2.0", "method": method, "params": params}
            try:
                self._process.stdin.write(json.dumps(msg) + "\n")
                self._process.stdin.flush()
            except (BrokenPipeError, OSError):
                pass


class MCPHttpClient:
    """HTTP 传输 — 连接远程 MCP Server。

    适用于: 远程 MCP Server 或局域网内其他 Worker 暴露的工具。
    """

    def __init__(self, name: str, url: str, headers: dict = None):
        self.name = name
        self.url = url.rstrip("/")
        self.headers = headers or {"Content-Type": "application/json"}
        self._msg_id = 0
        self._initialized = False
        self._lock = threading.Lock()

    def connect(self) -> bool:
        """发送 initialize 握手。"""
        resp = self._request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "lan-mesh-gateway", "version": "0.1.0"},
        })
        if resp is None:
            logger.error("[MCP:%s] HTTP 连接失败: %s", self.name, self.url)
            return False
        self._initialized = True
        logger.info("[MCP:%s] 已连接 (http: %s)", self.name, self.url)
        return True

    def list_tools(self) -> list:
        resp = self._request("tools/list", {})
        if resp and "result" in resp:
            return resp["result"].get("tools", [])
        return []

    def call_tool(self, tool_name: str, arguments: dict) -> dict:
        resp = self._request("tools/call", {
            "name": tool_name,
            "arguments": arguments,
        })
        if resp and "result" in resp:
            return resp["result"]
        if resp and "error" in resp:
            return {"isError": True, "content": [{"type": "text", "text": str(resp["error"])}]}
        return {"isError": True, "content": [{"type": "text", "text": "无响应"}]}

    def is_connected(self) -> bool:
        return self._initialized

    def disconnect(self):
        self._initialized = False

    def _request(self, method: str, params: dict) -> Optional[dict]:
        """通过 HTTP POST 发送 JSON-RPC 请求。"""
        with self._lock:
            self._msg_id += 1
            payload = {
                "jsonrpc": "2.0",
                "id": self._msg_id,
                "method": method,
                "params": params,
            }
            try:
                resp = requests.post(
                    self.url, json=payload, headers=self.headers, timeout=30
                )
                if resp.status_code == 200:
                    return resp.json()
                return None
            except requests.RequestException:
                return None


def create_mcp_client(name: str, config: dict):
    """根据配置创建 MCP 客户端。

    配置格式:
        # stdio 方式
        transport: stdio
        command: npx
        args: ["@modelcontextprotocol/server-filesystem", "/tmp"]
        env:
          NODE_ENV: production

        # HTTP 方式
        transport: http
        url: http://192.168.1.100:3000/mcp
        headers:
          Authorization: Bearer xxx
    """
    transport = config.get("transport", "stdio")
    if transport == "stdio":
        return MCPStdioClient(
            name=name,
            command=config.get("command", ""),
            args=config.get("args", []),
            env=config.get("env", {}),
        )
    elif transport == "http":
        return MCPHttpClient(
            name=name,
            url=config.get("url", ""),
            headers=config.get("headers", {}),
        )
    else:
        raise ValueError(f"未知传输方式: {transport}")
