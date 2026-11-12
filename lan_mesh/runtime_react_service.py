"""RuntimeReactService: domain behavior separated from the public compatibility facade."""

from __future__ import annotations
from typing import Any
from . import agent_runtime as policy
from .component_service import ComponentService
from .runtime_context import ReactContextBudget, bounded_int
import hashlib
import json

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
        """Execute bounded tool rounds with explicit failure and cancellation states."""
        requirement = input_data.get('requirement', input_data.get('description', ''))
        max_iterations = bounded_int(input_data.get('max_iterations'), 30, 1, 100)
        cwd = input_data.get('cwd') or self.owner.shared_folder
        tools = self.owner._build_openai_tools_schema(registry)
        prompt = self.owner._build_system_prompt(requirement) or ''
        prompt += policy._build_react_rules_prompt(cwd)
        prompt += policy._build_blueprint_prompt(input_data.get('_project_blueprint', ''))
        messages = [{'role': 'system', 'content': prompt}, {'role': 'user', 'content': requirement}]
        budget = ReactContextBudget.from_input(input_data)
        state = {'steps': [], 'usage': {'input_tokens': 0, 'output_tokens': 0}}
        previous_failure, repeated_failures = '', 0
        for iteration in range(max_iterations):
            if self.owner._cancel_event.is_set():
                return self._finish(state, budget, iteration, 'cancelled', '任务已取消')
            if not budget.fit(messages, tools):
                return self._finish(state, budget, iteration, 'failed',
                                    '上下文预算不足；原始需求、规则和调用记录已保留，请拆分任务。', 'context_limit')
            resp = self.owner._call_llm_messages_with_tools(messages, tools, input_data)
            for key in ('input_tokens', 'output_tokens'):
                state['usage'][key] += resp.get(key, 0)
            state['usage']['model'] = resp.get('model', '')
            if self.owner._cancel_event.is_set():
                return self._finish(state, budget, iteration + 1, 'cancelled', '任务已取消')
            outcome, error = policy.infer_result_status(resp)
            if outcome != 'completed':
                return self._finish(state, budget, iteration + 1, outcome, error, 'llm_failed')
            message = resp.get('message') or {}
            tool_calls = message.get('tool_calls') or []
            content = message.get('content') or ''
            if resp.get('finish_reason') in ('length', 'content_filter'):
                return self._finish(state, budget, iteration + 1, 'failed',
                                    '模型输出被截断或过滤，未执行未完成的答复。', 'incomplete_response')
            if not tool_calls:
                if not content.strip():
                    return self._finish(state, budget, iteration + 1, 'failed',
                                        '模型未返回完整的最终答复。', 'incomplete_response')
                return self._finish(state, budget, iteration + 1, 'completed', content)
            messages.append({**message, 'role': 'assistant'})
            batch = []
            for call in tool_calls:
                if self.owner._cancel_event.is_set():
                    return self._finish(state, budget, iteration + 1, 'cancelled', '任务已取消')
                observation, step = self._execute_tool(call, registry, cwd, iteration + 1)
                state['steps'].append(step)
                if self.owner._cancel_event.is_set():
                    return self._finish(state, budget, iteration + 1, 'cancelled', '任务已取消')
                batch.append((step['tool'], step['args'], observation, step['is_error']))
                messages.append({'role': 'tool', 'tool_call_id': call['id'],
                                 'content': budget.bound_output(observation)})
            signature = self._failure_signature(batch)
            repeated_failures = repeated_failures + 1 if signature and signature == previous_failure else 1
            previous_failure = signature
            if signature and repeated_failures >= 3:
                return self._finish(state, budget, iteration + 1, 'failed',
                                    '相同工具参数连续三轮返回相同错误，已停止无效重试。', 'repeated_tool_failure')
        if self.owner._cancel_event.is_set():
            return self._finish(state, budget, max_iterations, 'cancelled', '任务已取消')
        return self._finish(state, budget, max_iterations, 'max_iterations_reached',
                            f'达到最大执行轮次 {max_iterations}，任务尚未完成。', 'iteration_limit')

    def _execute_tool(self, call: dict, registry: Any, cwd: str, iteration: int) -> tuple[str, dict]:
        """Reject invalid arguments without running a tool with guessed defaults."""
        function = call['function']
        name = function['name']
        raw_args = function.get('arguments', '{}')
        args = {}
        try:
            args = json.loads(raw_args)
            if not isinstance(args, dict):
                raise ValueError('工具参数必须是 JSON 对象')
        except (TypeError, ValueError) as exc:
            text, is_error = f'工具参数无效: {exc}', True
            args = {'invalid_arguments': str(raw_args)[:300]}
        else:
            if name in ('shell_exec', 'dir_list') and 'cwd' not in args:
                args['cwd'] = cwd
            if name in ('file_read', 'file_write'):
                args['path'] = policy._resolve_tool_path(cwd, args.get('path', ''))
            result = registry.call_tool(name, args)
            text = '\n'.join(str(item.get('text', '')) for item in result.get('content', [])
                             if item.get('type', 'text') == 'text')
            is_error = bool(result.get('isError', False))
        step = {'iteration': iteration, 'tool': name, 'args': args,
                'result_preview': text[:300], 'is_error': is_error}
        return text, step

    @staticmethod
    def _failure_signature(batch: list) -> str:
        if not batch or not all(item[3] for item in batch):
            return ''
        return hashlib.sha256(json.dumps(batch, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    @staticmethod
    def _finish(state: dict, budget: ReactContextBudget, iterations: int, status: str,
                result: str, reason: str = '') -> dict:
        output = {**state, 'result': result, 'iterations': iterations, 'status': status,
                  'context': budget.stats()}
        if status not in ('completed', 'cancelled'):
            output.update(error=result, stop_reason=reason)
        print(f'[AgentRuntime] ReAct 结束: status={status}, iterations={iterations}, reason={reason}')
        return output

    def _build_openai_tools_schema(self, registry) -> list[dict]:
        """F2.1: 将 ToolRegistry 的工具转换为 OpenAI function calling 格式。"""
        tools = []
        for entry in registry.list_tools():
            tools.append({'type': 'function', 'function': {'name': entry['name'], 'description': entry['description'], 'parameters': entry['inputSchema']}})
        return tools
