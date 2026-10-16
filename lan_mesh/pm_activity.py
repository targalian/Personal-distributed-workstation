"""Track PM work and close old instances before a paused task changes execution owner."""
import threading
import weakref
from functools import wraps
from pathlib import Path
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from .pm_agent import ProjectManagerAgent

_INSTANCES = weakref.WeakSet()
_LOCK = threading.RLock()


def register(pm: "ProjectManagerAgent") -> None:
    with _LOCK:
        pm._activities = 0
        pm._retired = False
        _INSTANCES.add(pm)


def activity(method: Callable) -> Callable:
    """Count whole calls, including blocked tools, quality checks and HTTP callbacks."""
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        pm = getattr(self, '_agent', self)
        with _LOCK:
            if pm._retired:
                return
            pm._activities += 1
        try:
            return method(self, *args, **kwargs)
        finally:
            with _LOCK:
                pm._activities -= 1
    return wrapped


def retire_instances(path: Path, pm_id: str, *, commit: bool = True) -> None:
    """Caller holds the start lock. Also find old instances removed from current()."""
    with _LOCK:
        matches = [pm for pm in _INSTANCES if pm.pm_id == pm_id and pm._checkpoint_store
                   and pm._checkpoint_store.path.resolve() == path.resolve()]
        for pm in matches:
            if pm._activities or any(t and t.is_alive() for t in (pm._thread, pm._progress_thread)):
                raise ValueError('原 PM 的在途调用或回调尚未退出，请等待结果保存后重新核实')
            if pm.running:
                raise ValueError('原 PM 仍在运行，不能继续暂停任务')
        if commit:
            for pm in matches:
                pm._retired = True
