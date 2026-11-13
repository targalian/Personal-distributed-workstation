"""RuntimeReactService: bounded tool execution with durable, scoped observations."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from . import agent_runtime as policy
from .component_service import ComponentService
from .runtime_context import ReactContextBudget, bounded_int
from .runtime_react_archive import ReactArchive


class RuntimeReactService(ComponentService):
    """RuntimeReactService business operations with explicit state ownership."""
    OPERATIONS = ('_handle_react_agent', '_build_openai_tools_schema')

    def _handle_react_agent(self, input_data: dict) -> dict:
        """Own and close the temporary tool container for this execution."""
        from .tool_registry import ToolRegistry
        registry = ToolRegistry()
        from .browser_session_service import register_browser_tool
        register_browser_tool(self.owner, input_data, registry)
        try:
            return self._run_react(input_data, registry)
        finally:
            runtime = vars(registry).get('services')
            if runtime:
                runtime.close()

    def _run_react(self, input_data: dict, registry: Any) -> dict:
        budget = ReactContextBudget.from_input(input_data)
        state = {'steps': [], 'usage': {'input_tokens': 0, 'output_tokens': 0},
                 'model_requests': [], '_iteration': 0}
        try:
            if self.owner._cancel_event.is_set():
                return self._finish(state, budget, 0, 'cancelled', '任务已取消')
            archive = ReactArchive(self.owner, input_data, registry)
            state['_archive'] = archive
            state['execution'] = dict(execution_id=archive.store.execution_id, scope=archive.store.scope)
            requirement = input_data.get('requirement', input_data.get('description', ''))
            cwd = input_data.get('cwd') or self.owner.shared_folder
            prompt = self.owner._build_system_prompt(requirement) or ''
            prompt += policy._build_react_rules_prompt(cwd)
            prompt += policy._build_blueprint_prompt(input_data.get('_project_blueprint', ''))
            from .pm_memory import runtime_memory
            prompt += runtime_memory(self.owner, input_data)
            messages = [{'role': 'system', 'content': prompt}, {'role': 'user', 'content': requirement}]
            for message in messages:
                archive.append(message)
            data = {**input_data, '_react_archive': archive}
            return self._loop(data, registry, cwd, messages, state, budget)
        except Exception as exc:
            return self._finish(state, budget, state['_iteration'], 'failed',
                                f'执行或产物归档失败，需核对已执行工具：{type(exc).__name__}', 'execution_archive_failed')

    def _loop(self, data: dict, registry: Any, cwd: str, messages: list[dict],
              state: dict, budget: ReactContextBudget) -> dict:
        tools = self.owner._build_openai_tools_schema(registry)
        maximum = bounded_int(data.get('max_iterations'), 30, 1, 100)
        archive = state['_archive']
        previous_failure, repeated_failures = '', 0
        for iteration in range(1, maximum + 1):
            state['_iteration'] = iteration
            if self.owner._cancel_event.is_set():
                return self._finish(state, budget, iteration - 1, 'cancelled', '任务已取消')
            budget.checkpoint_ref = archive.checkpoint(state, 'before_model')['artifact_id']
            if not budget.fit(messages, tools):
                return self._finish(state, budget, iteration - 1, 'failed',
                                    '上下文预算不足；原始需求、规则和调用记录已保留，请拆分任务。', 'context_limit')
            resp = self.owner._call_llm_messages_with_tools(messages, tools, data)
            for key in ('input_tokens', 'output_tokens'):
                state['usage'][key] += resp.get(key, 0)
            state['usage']['model'] = resp.get('model', '')
            state['model_requests'].extend(resp.get('context_candidates', []))
            archive.append({**(resp.get('message') or {}), 'role': 'assistant'})
            if self.owner._cancel_event.is_set():
                return self._finish(state, budget, iteration, 'cancelled', '任务已取消')
            if resp.get('_error') or resp.get('model') == 'none':
                error = (resp.get('message') or {}).get('content') or '模型调用失败'
                print(f"[AgentRuntime] ReAct 模型调用失败: {error}")
                return self._finish(state, budget, iteration, 'failed', error, 'llm_failed')
            outcome, error = policy.infer_result_status(resp)
            if outcome != 'completed':
                return self._finish(state, budget, iteration, outcome, error, 'llm_failed')
            message = resp.get('message') or {}
            tool_calls, content = message.get('tool_calls') or [], message.get('content') or ''
            if resp.get('finish_reason') in ('length', 'content_filter'):
                return self._finish(state, budget, iteration, 'failed',
                                    '模型输出被截断或过滤，未执行未完成的答复。', 'incomplete_response')
            if not tool_calls:
                if not content.strip():
                    return self._finish(state, budget, iteration, 'failed',
                                        '模型未返回完整的最终答复。', 'incomplete_response')
                return self._finish(state, budget, iteration, 'completed', content)
            messages.append({**message, 'role': 'assistant'})
            batch = self._observe_tools(tool_calls, registry, cwd, iteration, messages, state, budget)
            if self.owner._cancel_event.is_set():
                return self._finish(state, budget, iteration, 'cancelled', '任务已取消')
            signature = self._failure_signature(batch)
            repeated_failures = repeated_failures + 1 if signature and signature == previous_failure else 1
            previous_failure = signature
            if signature and repeated_failures >= 3:
                return self._finish(state, budget, iteration, 'failed',
                                    '相同工具参数连续三轮返回相同错误，已停止无效重试。', 'repeated_tool_failure')
        return self._finish(state, budget, maximum, 'max_iterations_reached',
                            f'达到最大执行轮次 {maximum}，任务尚未完成。', 'iteration_limit')

    def _observe_tools(self, calls: list[dict], registry: Any, cwd: str, iteration: int,
                       messages: list[dict], state: dict, budget: ReactContextBudget) -> list:
        archive, batch = state['_archive'], []
        ids = [call['id'] for call in calls]
        if len(set(ids)) != len(ids) or not all(isinstance(i, str) and i for i in ids):
            raise ValueError('工具调用ID无效或重复')
        for call in calls:
            if self.owner._cancel_event.is_set():
                break
            state['pending_call'] = call
            archive.checkpoint(state, 'tool_started', pending_call=call)
            if self.owner._cancel_event.is_set():
                state.pop('pending_call', None)
                break
            observation, step, raw = self._execute_tool(call, registry, cwd, iteration)
            reference = archive.store.save('tool_result', dict(call=call, step=step,
                                           observation=observation, result=raw))
            step['artifact'] = reference
            state['steps'].append(step)
            full = {'role': 'tool', 'tool_call_id': call['id'], 'content': observation}
            archive.append(full)
            state.pop('pending_call', None)
            archive.checkpoint(state, 'tool_observed')
            batch.append((step['tool'], step['args'], observation, step['is_error']))
            messages.append({**full, 'content': budget.bound_output(observation, reference['artifact_id'])})
        return batch

    def _execute_tool(self, call: dict, registry: Any, cwd: str, iteration: int) -> tuple[str, dict, dict]:
        """Reject invalid arguments without running a tool with guessed defaults."""
        function, args, result = call['function'], {}, {}
        name, raw_args = function['name'], function.get('arguments', '{}')
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
        return text, step, result

    @staticmethod
    def _failure_signature(batch: list) -> str:
        if not batch or not all(item[3] for item in batch):
            return ''
        return hashlib.sha256(json.dumps(batch, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    @staticmethod
    def _finish(state: dict, budget: ReactContextBudget, iterations: int, status: str,
                result: str, reason: str = '') -> dict:
        output = {k: v for k, v in state.items() if not k.startswith('_')}
        output.update(result=result, iterations=iterations, status=status, context=budget.stats(),
                      review_required=bool(state.get('pending_call')))
        if status not in ('completed', 'cancelled'):
            output.update(error=result, stop_reason=reason)
        archive = state.get('_archive')
        if archive:
            try:
                output['checkpoint'] = archive.checkpoint(output, 'finished')
                archive.store.update(status, phase='finished', stop_reason=reason)
            except Exception as exc:
                output.update(status='failed', stop_reason='artifact_write_failed',
                              error=f'产物归档失败：{type(exc).__name__}；需核对已执行工具。')
        print(f"[AgentRuntime] ReAct 结束: status={output['status']}, iterations={iterations}, reason={output.get('stop_reason', '')}")
        return output

    def _build_openai_tools_schema(self, registry: Any) -> list[dict]:
        return [{'type': 'function', 'function': {'name': entry['name'],
                 'description': entry['description'], 'parameters': entry['inputSchema']}}
                for entry in registry.list_tools()]
