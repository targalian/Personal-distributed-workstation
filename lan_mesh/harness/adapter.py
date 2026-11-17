"""Adapter contract and conservative plain-output implementation.

Preparation accepts an already filtered environment from the caller. It neither
reads credentials nor runs commands; runtime environment policy stays in runtime.
"""

import shutil
from dataclasses import dataclass
from typing import Protocol

from .events import HarnessOutcome
from .spec import HarnessSpec


class HarnessAdapter(Protocol):
    """Interface implemented by registered harness adapters."""

    spec: HarnessSpec

    def detect(self) -> bool: ...

    def prepare(self, request: dict) -> dict: ...

    def parse(self, stdout: str, stderr: str, returncode: int) -> HarnessOutcome: ...

    def classify_error(self, returncode: int, stderr: str, outcome: HarnessOutcome) -> str: ...


@dataclass
class LegacyAdapter:
    """Wrap an existing command template without assuming structured telemetry."""

    spec: HarnessSpec

    def detect(self) -> bool:
        """Check executable availability without launching the CLI."""
        return shutil.which(self.spec.detect) is not None

    def prepare(self, request: dict) -> dict:
        """Build argv and copy the caller's filtered environment."""
        cwd = request["cwd"]
        return {
            "argv": self.spec.build_cmd(request["prompt"], cwd),
            "env": dict(request.get("env", {})),
            "cwd": cwd,
        }

    def parse(self, stdout: str, stderr: str, returncode: int) -> HarnessOutcome:
        """Preserve raw output and mark unavailable telemetry explicitly."""
        outcome = HarnessOutcome(
            status="completed" if returncode == 0 else "failed",
            result=stdout,
            usage={"input_tokens": 0, "output_tokens": 0},
            usage_known=False,
            cost_usd=0.0,
            cost_estimated=True,
            session_id="",
            degraded=["usage_unavailable", "cost_unavailable", "elapsed_unavailable"],
            error_class="",
            elapsed_s=0.0,
        )
        if returncode != 0:
            outcome.error_class = self.classify_error(returncode, stderr, outcome)
        return outcome

    def classify_error(self, returncode: int, stderr: str, outcome: HarnessOutcome) -> str:
        """Classify CLI failures using stable exit-code and diagnostic signals."""
        message = stderr.lower()
        if returncode == 127 or "not found" in message:
            return "not_installed"
        if any(term in message for term in ("usage limit", "rate limit")):
            return "usage_limit"
        if any(term in message for term in ("auth", "login", "401")):
            return "auth"
        if any(term in message for term in (
            "connectionerror", "connection error", "connection refused",
            "connection reset", "network", "timed out", "timeout", "dns",
        )):
            return "network"
        if "sandbox" in message:
            return "sandbox"
        return "unknown"
