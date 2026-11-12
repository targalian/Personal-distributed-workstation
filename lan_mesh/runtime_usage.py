"""Per-transport call identity, task attribution and honest provider usage capture."""

from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from typing import Any, Callable, Iterator
import time
import uuid

_scope: ContextVar[dict] = ContextVar('usage_scope', default={})
_call: ContextVar[dict | None] = ContextVar('usage_call', default=None)


@contextmanager
def attempt_scope(attempt_id: str = '', purpose: str = 'execution') -> Iterator[None]:
    token = _scope.set(dict(attempt_id=attempt_id, purpose=purpose))
    try:
        yield
    finally:
        _scope.reset(token)


def observe_usage(usage: dict) -> None:
    """Capture reported cumulative counts, including partial streaming failures."""
    current = _call.get()
    if current is not None and isinstance(usage, dict) and usage:
        for source, target in [('prompt_tokens', 'input_tokens'), ('completion_tokens', 'output_tokens')]:
            if source in usage:
                current[target] = max(0, int(usage[source] or 0))
        current['usage_known'] = 'prompt_tokens' in usage and 'completion_tokens' in usage


def current_usage_metadata() -> dict:
    current = _call.get()
    if current is None:
        return {}
    return {key: current[key] for key in ('usage_id', 'attempt_id', 'purpose', 'outcome', 'usage_known')}


def _finish_call(owner: Any, model: str, current: dict, started: float, call_type: str) -> None:
    from .model_resources import _get_usage_context
    from . import runtime_trace
    if owner._cancel_event.is_set():
        current['outcome'] = 'cancelled'
    try:
        owner._record_usage(model, current['input_tokens'], current['output_tokens'])
    except Exception:
        current['accounting_error'] = True
    try:
        runtime_trace.trace_llm_call(model, current['input_tokens'], current['output_tokens'],
            ttft_ms=0, total_ms=(time.monotonic() - started) * 1000,
            status=current['outcome'], task_id=_get_usage_context()[0], call_type=call_type,
            usage_metadata=current_usage_metadata())
    except Exception:
        pass


def metered_call(call_type: str) -> Callable:
    """Create a fresh ID per actual HTTP attempt; transport retries reuse recorded IDs only."""
    def decorate(function: Callable) -> Callable:
        @wraps(function)
        def run(self: Any, *args: Any, **kwargs: Any) -> dict:
            model = kwargs['model_id'] if 'model_id' in kwargs else args[2 if call_type == 'tools' else 1]
            current = dict(usage_id=uuid.uuid4().hex, attempt_id=_scope.get().get('attempt_id', ''),
                           purpose=_scope.get().get('purpose', 'unattributed'), outcome='failed',
                           input_tokens=0, output_tokens=0, usage_known=False)
            token, started = _call.set(current), time.monotonic()
            try:
                result = function(self, *args, **kwargs)
                current['outcome'] = ('failed' if result.get('status') == 'failed' or result.get('finish_reason') in ('length', 'content_filter')
                                      else 'completed')
                result['usage_id'] = current['usage_id']
                return result
            finally:
                try:
                    _finish_call(self.owner, model, current, started, call_type)
                finally:
                    _call.reset(token)
        return run
    return decorate


def call_for_task(runtime: Any, prompt: str, data: dict, task: dict, purpose: str) -> dict:
    """Charge planning, verification, aggregation and review to their owning task."""
    from .model_resources import usage_context
    task_id = str(task.get('task_id') or task.get('parent_task_id') or '')
    project_id = str(task.get('project_id') or task.get('input_data', {}).get('project_id') or '')
    with usage_context(task_id, project_id), attempt_scope(
            str(task.get('attempt_id') or uuid.uuid4().hex), purpose):
        return runtime._call_llm_with_routing(prompt, data)
