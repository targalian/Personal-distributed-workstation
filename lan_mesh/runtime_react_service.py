"""RuntimeReactService: domain behavior separated from the public compatibility facade."""

from __future__ import annotations
from typing import Any
from . import agent_runtime as policy
from .component_service import ComponentService

class RuntimeReactService(ComponentService):
    """RuntimeReactService business operations with explicit state ownership."""
    OPERATIONS = ('_handle_react_agent', '_build_openai_tools_schema')

    def _handle_react_agent(self, input_data: dict) -> dict:
        """Own and close the temporary tool container for this execution."""
        from .tool_registry import ToolRegistry
        registry = ToolRegistry()
        try:
            return self._run_react(input_data, registry)
        finally:
            runtime = vars(registry).get('services')
            if runtime:
                runtime.close()

    def _run_react(self, input_data: dict, registry: Any) -> dict:
        """F2.1: ReAct (Reasoning + Acting) 工具循环执行。

        子 Agent 自主决策使用哪些工具, 多步推理直到完成任务。
        支持: file_read, file_write, shell_exec, http_request, dir_list, python_eval

        流程:
        1. 将任务描述 + 可用工具发送给 LLM (function calling)
        2. LLM 返回 tool_calls → 执行工具 → 结果回填
        3. 重复直到 LLM 给出最终文本回复或达到最大轮次
        """
        requirement = input_data.get('requirement', input_data.get('description', ''))
        max_iterations = input_data.get('max_iterations', 30)
        cwd = input_data.get('cwd') or self.owner.shared_folder
        tools_schema = self.owner._build_openai_tools_schema(registry)
        system_prompt = self.owner._build_system_prompt(requirement)
        if not system_prompt:
            system_prompt = ''
        system_prompt += policy._build_react_rules_prompt(cwd)
        system_prompt += policy._build_blueprint_prompt(input_data.get('_project_blueprint', ''))
        messages = [{'role': 'system', 'content': system_prompt}, {'role': 'user', 'content': requirement}]
        total_input_tokens = 0
        total_output_tokens = 0
        steps_log = []
        for iteration in range(max_iterations):
            if self.owner._cancel_event.is_set():
                return {'result': '任务已取消', 'iterations': iteration, 'steps': steps_log, 'status': 'cancelled'}
            resp = self.owner._call_llm_messages_with_tools(messages, tools_schema, input_data)
            total_input_tokens += resp.get('input_tokens', 0)
            total_output_tokens += resp.get('output_tokens', 0)
            message = resp.get('message', {})
            tool_calls = message.get('tool_calls', [])
            content = message.get('content', '')
            if not tool_calls:
                policy.logger.info('[ReAct] 完成 (轮次=%d)', iteration + 1)
                return {'result': content, 'iterations': iteration + 1, 'steps': steps_log, 'usage': {'model': resp.get('model', ''), 'input_tokens': total_input_tokens, 'output_tokens': total_output_tokens}}
            messages.append(message)
            for tc in tool_calls:
                func_name = tc['function']['name']
                try:
                    func_args = policy._json.loads(tc['function'].get('arguments', '{}'))
                except policy._json.JSONDecodeError:
                    func_args = {}
                if func_name in ('shell_exec', 'dir_list') and 'cwd' not in func_args:
                    func_args['cwd'] = cwd
                if func_name in ('file_read', 'file_write'):
                    func_args['path'] = policy._resolve_tool_path(cwd, func_args.get('path', ''))
                policy.logger.info('[ReAct] 轮次%d 工具: %s(%s)', iteration + 1, func_name, str(func_args)[:100])
                tool_result = registry.call_tool(func_name, func_args)
                result_text = tool_result['content'][0]['text'] if tool_result['content'] else ''
                is_error = tool_result.get('isError', False)
                steps_log.append({'iteration': iteration + 1, 'tool': func_name, 'args': func_args, 'result_preview': result_text[:300], 'is_error': is_error})
                messages.append({'role': 'tool', 'tool_call_id': tc['id'], 'content': result_text})
                if self.owner._cancel_event.is_set():
                    return {'result': '任务已取消', 'iterations': iteration + 1, 'steps': steps_log, 'status': 'cancelled'}
        policy.logger.warning('[ReAct] 达到最大轮次 (%d), 强制结束', max_iterations)
        return {'result': f"[达到最大执行轮次 {max_iterations}] 最后步骤: {(steps_log[-1] if steps_log else 'N/A')}", 'iterations': max_iterations, 'steps': steps_log, 'status': 'max_iterations_reached', 'usage': {'input_tokens': total_input_tokens, 'output_tokens': total_output_tokens}}

    def _build_openai_tools_schema(self, registry) -> list[dict]:
        """F2.1: 将 ToolRegistry 的工具转换为 OpenAI function calling 格式。"""
        tools = []
        for entry in registry.list_tools():
            tools.append({'type': 'function', 'function': {'name': entry['name'], 'description': entry['description'], 'parameters': entry['inputSchema']}})
        return tools
