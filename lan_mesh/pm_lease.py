"""Keep a process-lifetime lock on the original local PM database before execution."""
import hashlib
import os
import threading
from pathlib import Path

_LEASES = {}
_LOCK = threading.Lock()


def lease_identity(path: Path) -> dict:
    """Recovery currently requires a Linux machine identity and the original path."""
    try:
        machine = Path('/etc/machine-id').read_text().strip()
    except OSError:
        machine = ''
    return {"format": 1, "machine": hashlib.sha256(machine.encode()).hexdigest() if machine else '',
            "path": str(Path(path).resolve())}


def acquire_pm_lease(path: Path) -> dict:
    """Never release while the process lives: stopped PMs can still have tool threads."""
    path = Path(path).resolve()
    with _LOCK:
        existing = _LEASES.get(str(path))
        if existing and existing[0] == os.getpid():
            return lease_identity(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(str(path) + '.process-lock', 'a+b')
        try:
            if os.name == 'nt':
                import msvcrt
                handle.seek(0)
                if not handle.read(1):
                    handle.write(b'0')
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, ImportError) as exc:
            handle.close()
            raise ValueError('原节点进程仍持有 PM 数据库，禁止并行启动或接管') from exc
        _LEASES[str(path)] = (os.getpid(), handle)
        return lease_identity(path)
