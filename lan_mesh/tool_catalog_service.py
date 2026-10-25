"""ToolCatalogService: domain behavior separated from the public compatibility facade."""

from __future__ import annotations
from typing import Any, Optional, Callable
from .protocol import ToolDef
from . import tool_registry as policy
from .component_service import ComponentService

class ToolCatalogService(ComponentService):
    """ToolCatalogService business operations with explicit state ownership."""
    OPERATIONS = ('register_tool', 'unregister_tool', 'list_tools', 'list_tool_defs', 'get_tool', 'load_plugins', 'tool_count')

    def register_tool(self, tool: ToolDef, handler: Callable) -> Any:
        """动态注册一个工具。"""
        self.owner._tools[tool.name] = {'tool': tool, 'handler': handler}

    def unregister_tool(self, name: str) -> Any:
        """注销工具。"""
        self.owner._tools.pop(name, None)

    def list_tools(self) -> list[dict]:
        """列出所有已注册工具 (MCP tools/list 格式)。"""
        return [{'name': entry['tool'].name, 'description': entry['tool'].description, 'inputSchema': entry['tool'].input_schema} for entry in list(self.owner._tools.values())]

    def list_tool_defs(self) -> list[ToolDef]:
        """返回所有 ToolDef 对象。"""
        return [entry['tool'] for entry in list(self.owner._tools.values())]

    def get_tool(self, name: str) -> Optional[dict]:
        """获取单个工具定义。"""
        return self.owner._tools.get(name)

    def load_plugins(self, config_path: str) -> Any:
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
        p = policy.Path(config_path).expanduser()
        if not p.is_file():
            return
        with open(p, 'r', encoding='utf-8') as f:
            config = policy.yaml.safe_load(f) or {}
        for item in config.get('tools', []):
            name = item.get('name', '')
            if not name or name in self.owner._tools:
                continue
            module_name = item.get('module', '')
            func_name = item.get('function', '')
            if not module_name or not func_name:
                continue
            try:
                mod = policy.importlib.import_module(module_name)
                handler = getattr(mod, func_name)
                tool = policy.ToolDef(name=name, description=item.get('description', ''), mcp_compatible=True, input_schema=item.get('input_schema', {}))
                self.owner.register_tool(tool, handler)
                policy.logger.info('插件工具已加载: %s (%s.%s)', name, module_name, func_name)
            except (ImportError, AttributeError) as e:
                policy.logger.error('插件加载失败: %s → %s', name, e)

    def tool_count(self) -> int:
        """返回已注册工具数量。"""
        return len(self.owner._tools)
