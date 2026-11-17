"""CLI harness metadata, adapter contracts, and backend registry."""

from . import registry
from .adapter import HarnessAdapter
from .events import HarnessEvent, HarnessOutcome
from .spec import Cap, HarnessSpec

__all__ = ["Cap", "HarnessAdapter", "HarnessEvent", "HarnessOutcome", "HarnessSpec", "registry"]
