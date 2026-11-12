"""Bound ReAct requests while preserving instructions and tool-call/result pairs."""

import json
from dataclasses import dataclass


def bounded_int(value: object, default: int, minimum: int, maximum: int) -> int:
    """Normalize an optional limit, including malformed and non-finite inputs."""
    try:
        return max(minimum, min(int(value), maximum))
    except (TypeError, ValueError, OverflowError):
        return default


def clip_tool_output(text: str, limit: int) -> str:
    """Keep the beginning and end, with an explicit notice of missing evidence."""
    if len(text) <= limit:
        return text
    marker = '\n[工具输出已裁剪；中间内容不可见。请缩小查询范围，或用 file_read 的 offset/limit 分段读取。]\n'
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

    @classmethod
    def from_input(cls, data: dict) -> 'ReactContextBudget':
        """Use character budgets, not a claim about a provider's token window."""
        return cls(
            bounded_int(data.get('context_max_chars'), 64000, 4096, 256000),
            bounded_int(data.get('tool_result_max_chars'), 12000, 512, 32000),
        )

    def bound_output(self, text: str) -> str:
        """Limit each tool response before it enters the conversation."""
        clipped = clip_tool_output(text, self.tool_chars)
        if clipped != text:
            self.truncated_outputs += 1
            self.removed_chars += len(text) - len(clipped)
        return clipped

    def fit(self, messages: list[dict], tools: list[dict]) -> bool:
        """Shrink old tool observations first; never discard user or system text."""
        size = self.request_size(messages, tools)
        for message in messages:
            if size <= self.max_chars:
                break
            content = message.get('content')
            if message.get('role') != 'tool' or not isinstance(content, str) or len(content) <= 256:
                continue
            clipped = clip_tool_output(content, 256)
            self.removed_chars += len(content) - len(clipped)
            self.truncated_outputs += 1
            message['content'] = clipped
            size = self.request_size(messages, tools)
        self.peak_request_chars = max(self.peak_request_chars, size)
        return size <= self.max_chars

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
        }
