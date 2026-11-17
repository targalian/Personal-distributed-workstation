"""Immutable metadata for CLI harness backends."""

from dataclasses import dataclass
from enum import Enum
from typing import Callable


class Cap(str, Enum):
    """Capabilities advertised by a harness backend."""

    JSONL_STREAM = "jsonl_stream"
    USAGE = "usage"
    USD = "usd"
    TOOL_ALLOW = "tool_allowlist"
    SYS_PROMPT = "sys_prompt"
    MODEL_SWAP = "model_swap"
    CANCEL = "cancel"
    RESUME = "resume"
    SANDBOX = "sandbox"


@dataclass(frozen=True)
class HarnessSpec:
    """Command template and declared capabilities; environment keys are names only."""

    name: str
    detect: str
    build_cmd: Callable[[str, str], list[str]]
    timeout: int
    description: str
    capabilities: frozenset[Cap] = frozenset()
    env_keys: frozenset[str] = frozenset()
