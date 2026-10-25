"""ToolExecutionService: domain behavior separated from the public compatibility facade."""

from __future__ import annotations
from .component_service import ComponentService

class ToolExecutionService(ComponentService):
    """ToolExecutionService business operations with explicit state ownership."""
    OPERATIONS = ('call_tool',)

    def call_tool(self, name: str, params: dict) -> dict:
        """执行工具调用 (MCP tools/call)。

        Returns:
            {"content": [...], "isError": bool}
            content 遵循 MCP 格式: [{"type": "text", "text": "..."}]
        """
        entry = self.owner._tools.get(name)
        if not entry:
            return {'content': [{'type': 'text', 'text': f'工具不存在: {name}'}], 'isError': True}
        try:
            result = entry['handler'](params)
            import json
            is_error = bool(isinstance(result, dict) and (result.get('timed_out') or result.get('git_safe_directory_denied')))
            return {'content': [{'type': 'text', 'text': json.dumps(result, ensure_ascii=False, default=str)}], 'isError': is_error, 'result': result}
        except Exception as e:
            return {'content': [{'type': 'text', 'text': f'工具执行错误: {e}'}], 'isError': True}
