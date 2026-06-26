"""
MCP 网关 — 中央工具调度枢纽

架构角色:
  Agent (GPT/Claude/DeepSeek)
    ↓ POST /tools/call
  MCP Gateway (本模块)
    ↓ JSON-RPC (stdio/HTTP)
  MCP Servers (filesystem/shell/git/browser/...)

核心职责:
1. 维护所有 MCP Server 的连接池 (stdio 子进程 + HTTP 远程)
2. 聚合所有 Server 的工具列表 → 统一 /tools/list
3. 路由工具调用到正确的 Server → 统一 /tools/call
4. 自动重连断开的 Server
5. 按工具来源标记,支持调用时指定 server
6. 工具描述按模型类型动态调整 (弱模型加更多示例)

配置加载:
  - 从 mcp_servers.yaml 加载 Server 定义
  - 运行时动态注册 (register_server)
  - 从 LAN Mesh 主机发现自动接入 (Worker 暴露的本地工具)
"""
import threading
import time
from typing import Optional

import yaml

from .mcp_client import create_mcp_client, MCPStdioClient, MCPHttpClient


class MCPGateway:
    """MCP 网关 — 统一工具调度中心。

    部署在 Secretary 节点 (或任意稳定主机),所有 Agent 通过 HTTP 调用网关,
    网关内部路由到具体的 MCP Server。
    """

    def __init__(self):
        self._servers: dict[str, dict] = {}      # server_name → {client, config, tools_cache}
        self._tool_index: dict[str, str] = {}     # tool_name → server_name (路由表)
        self._lock = threading.RLock()
        self._reconnect_interval = 30  # 断线重连间隔 (秒)

    # ── Server 管理 ──────────────────────────────────────────────

    def register_server(self, name: str, config: dict) -> bool:
        """注册并连接一个 MCP Server。

        config 格式:
            transport: stdio | http
            command: npx              # stdio 方式
            args: ["@modelcontextprotocol/server-filesystem", "/tmp"]
            url: http://...           # http 方式
            headers: {}               # http 方式
            env: {}                   # 环境变量
        """
        with self._lock:
            if name in self._servers:
                print(f"[Gateway] Server {name} 已存在,先断开旧连接")
                self._disconnect_server(name)

            client = create_mcp_client(name, config)
            if not client.connect():
                print(f"[Gateway] Server {name} 连接失败")
                return False

            self._servers[name] = {
                "client": client,
                "config": config,
                "tools_cache": [],
                "connected_at": time.time(),
            }

            # 刷新工具索引
            self._refresh_tools(name)
            print(f"[Gateway] Server '{name}' 已注册,工具数: {len(self._servers[name]['tools_cache'])}")
            return True

    def unregister_server(self, name: str):
        """断开并注销 Server。"""
        with self._lock:
            self._disconnect_server(name)

    def _disconnect_server(self, name: str):
        """断开 Server 连接。"""
        entry = self._servers.pop(name, None)
        if entry:
            entry["client"].disconnect()
            # 清理工具索引
            self._tool_index = {
                t: s for t, s in self._tool_index.items() if s != name
            }

    def list_servers(self) -> list:
        """列出所有已注册的 Server。"""
        with self._lock:
            return [
                {
                    "name": name,
                    "transport": entry["config"].get("transport", "stdio"),
                    "connected": entry["client"].is_connected(),
                    "tool_count": len(entry["tools_cache"]),
                    "connected_at": entry["connected_at"],
                }
                for name, entry in self._servers.items()
            ]

    # ── 工具发现与调用 ───────────────────────────────────────────

    def list_all_tools(self, model_type: str = None) -> list:
        """聚合所有 Server 的工具列表。

        Args:
            model_type: 模型类型 (如 gpt-4o, deepseek-v3),用于动态调整描述。
                        弱模型会获得更详细的工具描述和示例。

        Returns:
            工具列表,每个工具附带 source_server 字段标识来源。
        """
        with self._lock:
            all_tools = []
            for name, entry in self._servers.items():
                for tool in entry["tools_cache"]:
                    tool_copy = dict(tool)
                    tool_copy["source_server"] = name
                    # 按模型类型调整描述
                    if model_type and model_type in ("deepseek-v3", "qwen", "yi"):
                        desc = tool_copy.get("description", "")
                        if desc and "示例" not in desc:
                            tool_copy["description"] = desc + "\n\n示例: 调用此工具时传入正确的参数即可。"
                    all_tools.append(tool_copy)
            return all_tools

    def call_tool(self, tool_name: str, arguments: dict, server_name: str = None) -> dict:
        """调用工具 — 路由到正确的 MCP Server。

        Args:
            tool_name: 工具名称
            arguments: 工具参数
            server_name: 可选,指定从哪个 Server 调用 (同名工具时消歧)

        Returns:
            {"content": [...], "isError": bool}
        """
        with self._lock:
            # 确定目标 Server
            if server_name:
                target = server_name
            elif tool_name in self._tool_index:
                target = self._tool_index[tool_name]
            else:
                return {
                    "content": [{"type": "text", "text": f"工具不存在: {tool_name}"}],
                    "isError": True,
                }

            entry = self._servers.get(target)
            if not entry:
                return {
                    "content": [{"type": "text", "text": f"Server 不存在: {target}"}],
                    "isError": True,
                }

            client = entry["client"]
            if not client.is_connected():
                # 尝试重连
                if not self._reconnect(target):
                    return {
                        "content": [{"type": "text", "text": f"Server {target} 已断开,重连失败"}],
                        "isError": True,
                    }

            # 执行调用
            result = client.call_tool(tool_name, arguments)
            return result

    def get_tool_schema(self, tool_name: str) -> Optional[dict]:
        """获取单个工具的 JSON Schema。"""
        with self._lock:
            for entry in self._servers.values():
                for tool in entry["tools_cache"]:
                    if tool.get("name") == tool_name:
                        return tool
            return None

    # ── 内部维护 ─────────────────────────────────────────────────

    def _refresh_tools(self, server_name: str):
        """刷新指定 Server 的工具缓存。"""
        entry = self._servers.get(server_name)
        if not entry:
            return
        try:
            tools = entry["client"].list_tools()
            entry["tools_cache"] = tools
            # 更新路由索引
            for tool in tools:
                self._tool_index[tool.get("name", "")] = server_name
        except Exception as e:
            print(f"[Gateway] 刷新 {server_name} 工具列表失败: {e}")

    def _reconnect(self, server_name: str) -> bool:
        """尝试重连断开的 Server。"""
        entry = self._servers.get(server_name)
        if not entry:
            return False
        print(f"[Gateway] 尝试重连 {server_name}...")
        client = create_mcp_client(server_name, entry["config"])
        if client.connect():
            entry["client"] = client
            self._refresh_tools(server_name)
            return True
        return False

    def health_check(self):
        """检查所有 Server 连接状态,自动重连断开的。"""
        with self._lock:
            for name, entry in list(self._servers.items()):
                if not entry["client"].is_connected():
                    print(f"[Gateway] {name} 已断开,尝试重连")
                    self._reconnect(name)

    def start_health_check_loop(self, interval: int = 30):
        """启动后台健康检查循环。"""
        def _loop():
            while True:
                time.sleep(interval)
                try:
                    self.health_check()
                except Exception:
                    pass

        t = threading.Thread(target=_loop, name="mcp-gateway-health", daemon=True)
        t.start()

    # ── 配置加载 ─────────────────────────────────────────────────

    def load_config(self, config_path: str):
        """从 YAML 配置文件加载 MCP Server 定义。

        配置格式 (mcp_servers.yaml):
            servers:
              filesystem:
                transport: stdio
                command: npx
                args: ["@modelcontextprotocol/server-filesystem", "/tmp"]
                env: {}

              remote-tools:
                transport: http
                url: http://192.168.1.100:3000/mcp
                headers: {}
        """
        from pathlib import Path
        p = Path(config_path).expanduser()
        if not p.is_file():
            return

        with open(p, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f) or {}

        for name, server_config in config.get("servers", {}).items():
            self.register_server(name, server_config)

    # ── 统计 ─────────────────────────────────────────────────────

    def stats(self) -> dict:
        """返回网关统计信息。"""
        with self._lock:
            total_tools = sum(len(e["tools_cache"]) for e in self._servers.values())
            connected = sum(1 for e in self._servers.values() if e["client"].is_connected())
            return {
                "servers_total": len(self._servers),
                "servers_connected": connected,
                "tools_total": total_tools,
                "tool_names": list(self._tool_index.keys()),
            }
