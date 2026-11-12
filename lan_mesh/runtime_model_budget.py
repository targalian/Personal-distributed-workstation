"""Per-candidate model window budgets using explicit, conservative size estimates."""

import copy
import json
from dataclasses import dataclass

from .runtime_context import ReactContextBudget, bounded_int


def estimate_request_tokens(messages: list[dict], tools: list[dict]) -> int:
    """UTF-8 byte estimate plus framing reserve; never report this as provider usage."""
    raw = json.dumps(dict(messages=messages, tools=tools), ensure_ascii=False)
    return len(raw.encode('utf-8')) + 32 * len(messages) + 64 * len(tools) + 256


@dataclass(frozen=True)
class ModelWindowBudget:
    """Configured ceilings are local policy, not live provider specifications."""

    window: int
    reply_tokens: int
    safety_tokens: int
    known_model: bool

    @classmethod
    def resolve(cls: type['ModelWindowBudget'], entry: object, data: dict) -> 'ModelWindowBudget':
        window = bounded_int(getattr(entry, 'max_context_tokens', None), 32768, 1024, 2000000)
        output = bounded_int(getattr(entry, 'max_output_tokens', None), min(4096, window // 2), 1, window // 2)
        reply = bounded_int(data.get('reply_max_tokens'), output, 1, output)
        return cls(window, reply, max(256, min(2048, window // 20)), entry is not None)

    def prepare(self, messages: list[dict], tools: list[dict], data: dict,
                checkpoint: str = '') -> tuple[list[dict] | None, dict]:
        """Fit a private candidate copy; smaller fallbacks cannot corrupt larger candidates."""
        candidate = copy.deepcopy(messages)
        budget = ReactContextBudget.from_input(data)
        budget.token_limit = self.window - self.reply_tokens - self.safety_tokens
        budget.checkpoint_ref = checkpoint
        accepted = budget.fit(candidate, tools)
        report = dict(context_window=self.window, reply_tokens=self.reply_tokens,
                      safety_tokens=self.safety_tokens, input_budget=budget.token_limit,
                      estimated_input_tokens=estimate_request_tokens(candidate, tools),
                      estimator='utf8-bytes-plus-framing', configured_model=self.known_model,
                      accepted=accepted, **budget.stats())
        return (candidate if accepted else None), report
