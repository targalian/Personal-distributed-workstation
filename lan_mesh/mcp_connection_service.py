"""McpConnectionService: domain behavior separated from the public compatibility facade."""

from __future__ import annotations
from typing import Any
from . import mcp_gateway as policy
from .component_service import ComponentService

class McpConnectionService(ComponentService):
    """McpConnectionService business operations with explicit state ownership."""
    OPERATIONS = ('register_server', 'unregister_server', '_disconnect_server', '_refresh_tools', '_reconnect', 'health_check', 'load_config')

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
        with self.owner._lock:
            if name in self.owner._servers:
                policy.logger.info('Server %s 已存在, 先断开旧连接', name)
                self.owner._disconnect_server(name)
            client = policy.create_mcp_client(name, config)
            if not client.connect():
                policy.logger.error('Server %s 连接失败', name)
                return False
            self.owner._servers[name] = {'client': client, 'config': config, 'tools_cache': [], 'connected_at': policy.time.time()}
            self.owner._refresh_tools(name)
            policy.logger.info("Server '%s' 已注册, 工具数: %d", name, len(self.owner._servers[name]['tools_cache']))
            return True

    def unregister_server(self, name: str) -> Any:
        """断开并注销 Server。"""
        with self.owner._lock:
            self.owner._disconnect_server(name)

    def _disconnect_server(self, name: str) -> Any:
        """断开 Server 连接。"""
        entry = self.owner._servers.pop(name, None)
        if entry:
            entry['client'].disconnect()
            self.owner._tool_index = {t: s for t, s in self.owner._tool_index.items() if s != name}

    def _refresh_tools(self, server_name: str) -> Any:
        """刷新指定 Server 的工具缓存。"""
        entry = self.owner._servers.get(server_name)
        if not entry:
            return
        try:
            tools = entry['client'].list_tools()
            entry['tools_cache'] = tools
            for tool in tools:
                self.owner._tool_index[tool.get('name', '')] = server_name
        except Exception as e:
            policy.logger.error('刷新 %s 工具列表失败: %s', server_name, e)

    def _reconnect(self, server_name: str) -> bool:
        """尝试重连断开的 Server。"""
        entry = self.owner._servers.get(server_name)
        if not entry:
            return False
        policy.logger.info('尝试重连 %s...', server_name)
        client = policy.create_mcp_client(server_name, entry['config'])
        if client.connect():
            entry['client'] = client
            self.owner._refresh_tools(server_name)
            return True
        return False

    def health_check(self) -> Any:
        """检查所有 Server 连接状态,自动重连断开的。"""
        with self.owner._lock:
            for name, entry in list(self.owner._servers.items()):
                if not entry['client'].is_connected():
                    policy.logger.warning('%s 已断开, 尝试重连', name)
                    self.owner._reconnect(name)

    def load_config(self, config_path: str) -> Any:
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
        with open(p, 'r', encoding='utf-8') as f:
            config = policy.yaml.safe_load(f) or {}
        for name, server_config in config.get('servers', {}).items():
            self.owner.register_server(name, server_config)
