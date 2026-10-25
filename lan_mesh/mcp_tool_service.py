"""McpToolService: domain behavior separated from the public compatibility facade."""

from __future__ import annotations
from . import mcp_gateway as policy
from .component_service import ComponentService

class McpToolService(ComponentService):
    """McpToolService business operations with explicit state ownership."""
    OPERATIONS = ('list_servers', 'list_all_tools', 'call_tool', 'get_tool_schema', 'stats')

    def list_servers(self) -> list:
        """列出所有已注册的 Server。"""
        with self.owner._lock:
            return [{'name': name, 'transport': entry['config'].get('transport', 'stdio'), 'connected': entry['client'].is_connected(), 'tool_count': len(entry['tools_cache']), 'connected_at': entry['connected_at']} for name, entry in self.owner._servers.items()]

    def list_all_tools(self, model_type: str=None) -> list:
        """聚合所有 Server 的工具列表。

        Args:
            model_type: 模型类型 (如 gpt-4o, deepseek-v3),用于动态调整描述。
                        弱模型会获得更详细的工具描述和示例。

        Returns:
            工具列表,每个工具附带 source_server 字段标识来源。
        """
        with self.owner._lock:
            all_tools = []
            for name, entry in self.owner._servers.items():
                for tool in entry['tools_cache']:
                    tool_copy = dict(tool)
                    tool_copy['source_server'] = name
                    if model_type and model_type in ('deepseek-v3', 'qwen', 'yi'):
                        desc = tool_copy.get('description', '')
                        if desc and '示例' not in desc:
                            tool_copy['description'] = desc + '\n\n示例: 调用此工具时传入正确的参数即可。'
                    all_tools.append(tool_copy)
            return all_tools

    def call_tool(self, tool_name: str, arguments: dict, server_name: str=None) -> dict:
        """调用工具 — 路由到正确的 MCP Server。

        Args:
            tool_name: 工具名称
            arguments: 工具参数
            server_name: 可选,指定从哪个 Server 调用 (同名工具时消歧)

        Returns:
            {"content": [...], "isError": bool}
        """
        with self.owner._lock:
            if server_name:
                target = server_name
            elif tool_name in self.owner._tool_index:
                target = self.owner._tool_index[tool_name]
            else:
                return {'content': [{'type': 'text', 'text': f'工具不存在: {tool_name}'}], 'isError': True}
            entry = self.owner._servers.get(target)
            if not entry:
                return {'content': [{'type': 'text', 'text': f'Server 不存在: {target}'}], 'isError': True}
            client = entry['client']
            if not client.is_connected():
                if not self.owner._reconnect(target):
                    return {'content': [{'type': 'text', 'text': f'Server {target} 已断开,重连失败'}], 'isError': True}
            result = client.call_tool(tool_name, arguments)
            return result

    def get_tool_schema(self, tool_name: str) -> policy.Optional[dict]:
        """获取单个工具的 JSON Schema。"""
        with self.owner._lock:
            for entry in self.owner._servers.values():
                for tool in entry['tools_cache']:
                    if tool.get('name') == tool_name:
                        return tool
            return None

    def stats(self) -> dict:
        """返回网关统计信息。"""
        with self.owner._lock:
            total_tools = sum((len(e['tools_cache']) for e in self.owner._servers.values()))
            connected = sum((1 for e in self.owner._servers.values() if e['client'].is_connected()))
            return {'servers_total': len(self.owner._servers), 'servers_connected': connected, 'tools_total': total_tools, 'tool_names': list(self.owner._tool_index.keys())}
