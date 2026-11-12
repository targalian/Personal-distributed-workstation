"""Process-local execution guards shared by artifact writers, replication and cleanup."""

import threading
import weakref
from contextlib import contextmanager
from functools import wraps
from pathlib import Path
from typing import Any, Callable, Iterator

_registry_lock = threading.Lock()
_locks: weakref.WeakValueDictionary = weakref.WeakValueDictionary()


@contextmanager
def artifact_guard(root: Path, execution_id: str) -> Iterator[None]:
    key = str(root.resolve()) + "/" + execution_id
    with _registry_lock:
        lock = _locks.get(key)
        if lock is None:
            lock = threading.RLock()
            _locks[key] = lock
    with lock:
        yield


def guarded(function: Callable) -> Callable:
    @wraps(function)
    def wrapped(self: Any, *args: Any, **kwargs: Any) -> Any:
        with artifact_guard(self.root, self.execution_id):
            return function(self, *args, **kwargs)

    return wrapped
