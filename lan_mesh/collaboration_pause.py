"""Serialize dispatch admission with a pause barrier without holding PM state locks."""

from functools import wraps
from typing import Any, Callable


def dispatch_admission(method: Callable) -> Callable:
    @wraps(method)
    def guarded(self: Any, *args: Any, **kwargs: Any) -> Any:
        with self._dispatch_lock:
            if getattr(self._agent, '_suspended', False) is True:
                return None
            return method(self, *args, **kwargs)
    return guarded
