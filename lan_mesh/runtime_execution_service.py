"""RuntimeExecutionService: domain behavior separated from the public compatibility facade."""

from __future__ import annotations
from . import agent_runtime as policy
from .component_service import ComponentService

class RuntimeExecutionService(ComponentService):
    """RuntimeExecutionService business operations with explicit state ownership."""
    OPERATIONS = ('execute', '_finalize_subtask')

    def execute(self, subtask: dict) -> dict:
        """Scope attribution and skill selection to one execution, including failures."""
        from .model_resources import usage_context
        previous = getattr(self.owner._local, 'current_skill', '')
        try:
            with usage_context(subtask.get('parent_task_id', ''), subtask.get('project_id', '')):
                return self._execute(subtask)
        finally:
            self.owner._local.current_skill = previous

    def _execute(self, subtask: dict) -> dict:
        """执行子任务,返回结果字典。

        支持模型路由器注入: 从 subtask payload 中提取 model_preference 和
        fallback_models, 传递给 LLM 技能处理器, 实现智能模型选择和降级链重试。

        Args:
            subtask: 包含 name, description, required_skill, input_data,
                     model_preference, fallback_models 的字典

        Returns:
            {"output": {...}, "status": "completed"|"failed", "error": "..."}
        """
        import time as _time
        from . import runtime_trace
        skill = subtask.get('required_skill', '')
        handler = self.owner._handlers.get(skill)
        task_id = subtask.get('parent_task_id', '')
        model_pref = subtask.get('model_preference', '')
        trace_id = runtime_trace.trace_subtask_start(skill=skill, task_id=task_id, model_pref=model_pref)
        exec_start = _time.time()
        if not handler:
            runtime_trace.trace_subtask_end(trace_id, skill, 'failed', 0, error=f'未知的技能类型: {skill}', task_id=task_id)
            return {'output': {}, 'status': 'failed', 'error': f'未知的技能类型: {skill}'}
        if self.owner._cancel_event.is_set():
            runtime_trace.trace_subtask_end(trace_id, skill, 'cancelled', 0, task_id=task_id)
            return {'output': {}, 'status': 'cancelled'}
        self.owner._local.current_skill = skill
        input_data = dict(subtask.get('input_data', {}))
        input_data['_model_preference'] = model_pref
        input_data['_fallback_models'] = subtask.get('fallback_models', [])
        input_data['_trace_id'] = trace_id
        try:
            result = handler(input_data)
            if isinstance(result, dict) and result.get('status') == 'cancelled':
                elapsed_ms = (_time.time() - exec_start) * 1000
                runtime_trace.trace_subtask_end(trace_id, skill, 'cancelled', elapsed_ms, task_id=task_id)
                return {'output': result, 'status': 'cancelled'}
            usage = {}
            if isinstance(result, dict) and 'usage' in result:
                usage = result.pop('usage', {})
            elapsed_ms = (_time.time() - exec_start) * 1000
            return self.owner._finalize_subtask(result, usage, skill, trace_id, task_id, elapsed_ms)
        except Exception as e:
            elapsed_ms = (_time.time() - exec_start) * 1000
            runtime_trace.trace_subtask_end(trace_id, skill, 'failed', elapsed_ms, error=str(e), task_id=task_id)
            return {'output': {}, 'status': 'failed', 'error': str(e)}

    def _finalize_subtask(self, result, usage: dict, skill: str, trace_id, task_id: str, elapsed_ms: float) -> dict:
        """收尾子任务: 采纳 handler 内层成败信号并写追踪 (BUG-039 根因3)。

        各技能处理器用**返回值** (`{"error": ...}` / `{"status": "failed"}`)
        而非异常表达失败。原先只要 handler 未抛异常就一律记 `completed`,
        于是护栏拒绝、CLI 非零退出、执行超时全被上报为成功 —— PM 侧的重试与
        升级链永不触发, 交付物是错的却显示一切正常。

        Args:
            result: handler 返回值
            usage: 已提取的 token 用量
            skill: 技能类型
            trace_id: 追踪 ID
            task_id: 父任务 ID
            elapsed_ms: 执行耗时 (毫秒)

        Returns:
            子任务结果字典 (含真实 status)
        """
        from . import runtime_trace
        outcome, inner_err = policy.infer_result_status(result)
        runtime_trace.trace_subtask_end(trace_id, skill, outcome, elapsed_ms, model=usage.get('model', ''), input_tokens=usage.get('input_tokens', 0), output_tokens=usage.get('output_tokens', 0), error=inner_err[:500], task_id=task_id)
        if outcome != 'completed':
            policy.logger.warning('[AgentRuntime] 子任务技能 %s 返回失败: %s', skill, inner_err[:200])
            return {'output': result, 'status': outcome, 'error': inner_err[:500], 'usage': usage}
        return {'output': result, 'status': 'completed', 'usage': usage}
