"""Common event and outcome records for CLI harness adapters."""

from dataclasses import dataclass


@dataclass
class HarnessEvent:
    """One ordered event emitted during a harness run."""

    kind: str
    seq: int
    ts: float
    data: dict


@dataclass
class HarnessOutcome:
    """Execution result with explicit accounting availability and degradation."""

    status: str
    result: str
    usage: dict
    usage_known: bool
    cost_usd: float
    cost_estimated: bool
    session_id: str
    degraded: list[str]
    error_class: str
    elapsed_s: float
