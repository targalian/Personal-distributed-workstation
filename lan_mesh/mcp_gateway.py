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
from .logger import get_logger

logger = get_logger("mcp_gateway")


from typing import Any

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
        self._health_stop = threading.Event()
        self._health_thread = None

    # ── Server 管理 ──────────────────────────────────────────────

    def register_server(self, name: str, config: dict) -> bool:
        return call_component(self, McpConnectionService, 'register_server', name=name, config=config)

    def unregister_server(self, name: str) -> Any:
        return call_component(self, McpConnectionService, 'unregister_server', name=name)

    def _disconnect_server(self, name: str) -> Any:
        return call_component(self, McpConnectionService, '_disconnect_server', name=name)

    def list_servers(self) -> list:
        return call_component(self, McpToolService, 'list_servers')

    # ── 工具发现与调用 ───────────────────────────────────────────

    def list_all_tools(self, model_type: str=None) -> list:
        return call_component(self, McpToolService, 'list_all_tools', model_type=model_type)

    def call_tool(self, tool_name: str, arguments: dict, server_name: str=None) -> dict:
        return call_component(self, McpToolService, 'call_tool', tool_name=tool_name, arguments=arguments, server_name=server_name)

    def get_tool_schema(self, tool_name: str) -> Optional[dict]:
        return call_component(self, McpToolService, 'get_tool_schema', tool_name=tool_name)

    # ── 内部维护 ─────────────────────────────────────────────────

    def _refresh_tools(self, server_name: str) -> Any:
        return call_component(self, McpConnectionService, '_refresh_tools', server_name=server_name)

    def _reconnect(self, server_name: str) -> bool:
        return call_component(self, McpConnectionService, '_reconnect', server_name=server_name)

    def health_check(self) -> Any:
        return call_component(self, McpConnectionService, 'health_check')

    def start_health_check_loop(self, interval: int = 30) -> None:
        """启动后台健康检查循环。"""
        if self._health_thread and self._health_thread.is_alive():
            return
        self._health_stop.clear()
        def _loop():
            while not self._health_stop.wait(interval):
                try:
                    self.health_check()
                except Exception:
                    pass

        t = threading.Thread(target=_loop, name="mcp-gateway-health", daemon=True)
        self._health_thread = t
        t.start()

    def stop(self) -> None:
        """Stop reconnect production and disconnect each owned client."""
        from .application_lifecycle import join_threads
        self._health_stop.set()
        join_threads([self._health_thread])
        with self._lock:
            names = list(self._servers)
        for name in names:
            self.unregister_server(name)

    # ── 配置加载 ─────────────────────────────────────────────────

    def load_config(self, config_path: str) -> Any:
        return call_component(self, McpConnectionService, 'load_config', config_path=config_path)

    # ── 统计 ─────────────────────────────────────────────────────

    def stats(self) -> dict:
        return call_component(self, McpToolService, 'stats')


from typing import Any
from .component_service import call_component
from .mcp_connection_service import McpConnectionService
from .mcp_tool_service import McpToolService
