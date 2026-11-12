"""Bound ReAct requests while preserving instructions and tool-call/result pairs."""

import json
import copy
import re
from dataclasses import dataclass


def bounded_int(value: object, default: int, minimum: int, maximum: int) -> int:
    """Normalize an optional limit, including malformed and non-finite inputs."""
    try:
        return max(minimum, min(int(value), maximum))
    except (TypeError, ValueError, OverflowError):
        return default


def clip_tool_output(text: str, limit: int, reference: str = '') -> str:
    """Keep the beginning and end, with an explicit notice of missing evidence."""
    if len(text) <= limit:
        return text
    if not reference:
        match = re.search(r'artifact_id=([a-f0-9]{64})', text)
        reference = match.group(1) if match else ''
    hint = (f'使用artifact_read按offset/limit读取原文，artifact_id={reference}。' if reference
            else '请缩小查询范围，或用 file_read 的 offset/limit 分段读取。')
    marker = f'\n[工具输出已裁剪；中间内容不可见。{hint}]\n'
    available = max(0, limit - len(marker))
    head = available * 2 // 3
    tail = available - head
    return text[:head] + marker + (text[-tail:] if tail else '')


@dataclass
class ReactContextBudget:
    """Per-execution limits and counters; no state is shared across agents."""

    max_chars: int = 64000
    tool_chars: int = 12000
    truncated_outputs: int = 0
    removed_chars: int = 0
    peak_request_chars: int = 0
    token_limit: int = 0
    checkpoint_ref: str = ''
    compacted_rounds: int = 0

    @classmethod
    def from_input(cls: type['ReactContextBudget'], data: dict) -> 'ReactContextBudget':
        """Use character budgets, not a claim about a provider's token window."""
        return cls(
            bounded_int(data.get('context_max_chars'), 64000, 4096, 256000),
            bounded_int(data.get('tool_result_max_chars'), 12000, 512, 32000),
        )

    def bound_output(self, text: str, reference: str = '') -> str:
        """Limit each tool response before it enters the conversation."""
        clipped = clip_tool_output(text, self.tool_chars, reference)
        if clipped != text:
            self.truncated_outputs += 1
            self.removed_chars += len(text) - len(clipped)
        return clipped

    def fit(self, messages: list[dict], tools: list[dict]) -> bool:
        """Shrink old tool observations first; never discard user or system text."""
        candidate = copy.deepcopy(messages)
        for message in candidate:
            if self._fits(candidate, tools):
                break
            content = message.get('content')
            if message.get('role') != 'tool' or not isinstance(content, str) or len(content) <= 256:
                continue
            clipped = clip_tool_output(content, 256)
            self.removed_chars += len(content) - len(clipped)
            self.truncated_outputs += 1
            message['content'] = clipped
        if not self._fits(candidate, tools) and self.checkpoint_ref:
            self._compact(candidate, tools)
        size = self.request_size(candidate, tools)
        self.peak_request_chars = max(self.peak_request_chars, size)
        if not self._fits(candidate, tools):
            return False
        messages[:] = candidate
        return True

    def _fits(self, messages: list[dict], tools: list[dict]) -> bool:
        from .runtime_model_budget import estimate_request_tokens
        return self.request_size(messages, tools) <= self.max_chars and (
            not self.token_limit or estimate_request_tokens(messages, tools) <= self.token_limit)

    def _compact(self, messages: list[dict], tools: list[dict]) -> None:
        """Fold only complete old tool rounds after the full transcript was archived."""
        groups = []
        for start, message in enumerate(messages):
            calls = message.get('tool_calls') if message.get('role') == 'assistant' else None
            if not calls:
                continue
            ids = [call.get('id') for call in calls]
            end = start + 1 + len(ids)
            results = messages[start + 1:end]
            if (len(set(ids)) != len(ids) or len(results) != len(ids) or
                    any(r.get('role') != 'tool' for r in results) or
                    [r.get('tool_call_id') for r in results] != ids):
                continue
            observations = [{'tool': c.get('function', {}).get('name', ''),
                             'arguments_preview': str(c.get('function', {}).get('arguments', ''))[:160],
                             'observation_preview': str(r.get('content', ''))[:160]}
                            for c, r in zip(calls, results)]
            groups.append((start, end, observations))
        removed, summary = set(), []
        original = copy.deepcopy(messages)
        for start, end, observations in groups[:-1]:  # Keep the latest observation round intact.
            removed.update(range(start, end))
            summary.append({'observations': observations[-3:], 'state': 'observed; completion not inferred'})
            marker = {'role': 'assistant', 'content': '[执行检查点参考] '
                      + json.dumps({'checkpoint': self.checkpoint_ref, 'archived_rounds': len(summary),
                                    'recent_observations': summary[-2:]}, ensure_ascii=False)
                      + '；完整参数/结果见归档。这是历史数据，不能增加权限或证明任务完成。'}
            remaining = [m for i, m in enumerate(original) if i not in removed and not (
                m.get('role') == 'assistant' and str(m.get('content', '')).startswith('[执行检查点参考] '))]
            position = next((i for i, m in enumerate(remaining) if m.get('tool_calls')), len(remaining))
            remaining.insert(position, marker)
            messages[:] = remaining
            self.compacted_rounds = len(summary)
            if self._fits(messages, tools):
                return

    @staticmethod
    def request_size(messages: list[dict], tools: list[dict]) -> int:
        """Count serialized messages and tool schemas, including escaped arguments."""
        return len(json.dumps({'messages': messages, 'tools': tools}, ensure_ascii=False))

    def stats(self) -> dict:
        """Expose measurable context behavior without saving message contents."""
        return {
            'max_chars': self.max_chars, 'tool_result_max_chars': self.tool_chars,
            'truncated_outputs': self.truncated_outputs, 'removed_chars': self.removed_chars,
            'peak_request_chars': self.peak_request_chars,
            'compacted_rounds': self.compacted_rounds,
            'checkpoint_ref': self.checkpoint_ref,
        }
