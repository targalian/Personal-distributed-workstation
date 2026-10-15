"""Persist PM execution snapshots before side effects and expose read-only recovery evidence."""
import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from .execution_receipts import PROCESS_OWNER


class PMCheckpoints:
    """One atomic latest snapshot per PM, fenced by process owner and revision."""

    def __init__(self, path: Path, owner: str = PROCESS_OWNER):
        self.path, self.owner = Path(path), owner
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=10)) as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS pm_checkpoints (
                pm_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, device_id TEXT NOT NULL,
                owner TEXT NOT NULL, revision INTEGER NOT NULL, updated_at REAL NOT NULL,
                snapshot TEXT NOT NULL
            )""")

    def save(self, pm_id: str, task_id: str, device_id: str, snapshot: dict, revision: int) -> int:
        if not pm_id or not task_id or snapshot.get("task", {}).get("task_id") != task_id:
            raise ValueError("检查点任务归属不匹配")
        encoded = json.dumps(snapshot, ensure_ascii=False, allow_nan=False)
        with closing(sqlite3.connect(self.path, timeout=10)) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT task_id,device_id,owner,revision FROM pm_checkpoints WHERE pm_id=?",
                               (pm_id,)).fetchone()
            if row is not None and row != (task_id, device_id, self.owner, revision):
                raise ValueError("检查点归属或版本已变化，禁止覆盖")
            if row is None and revision != 0:
                raise ValueError("检查点缺失，禁止从旧版本重建")
            conn.execute("INSERT OR REPLACE INTO pm_checkpoints VALUES (?,?,?,?,?,?,?)",
                         (pm_id, task_id, device_id, self.owner, revision + 1, time.time(), encoded))
        return revision + 1


def read_checkpoint(path: Path, pm_id: str, task_id: str, device_id: str) -> dict | None:
    """Read without creating a database/table or changing execution ownership."""
    path = Path(path)
    if not path.is_file():
        return None
    with closing(sqlite3.connect(path.absolute().as_uri() + "?mode=ro", uri=True, timeout=5)) as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='pm_checkpoints'").fetchone():
            return None
        row = conn.execute("SELECT task_id,device_id,revision,updated_at,snapshot FROM pm_checkpoints WHERE pm_id=?",
                           (pm_id,)).fetchone()
    if row is None:
        return None
    if row[:2] != (task_id, device_id):
        raise ValueError("检查点归属不匹配")
    snapshot = json.loads(row[4])
    if snapshot.get("format") != 1 or snapshot.get("task", {}).get("task_id") != task_id:
        raise ValueError("检查点格式或任务归属不匹配")
    return {"revision": row[2], "updated_at": row[3], "snapshot": snapshot}


def inspect_checkpoint(path: Path, pm_id: str, task_id: str, device_id: str) -> dict:
    """Expose progress counts, never raw prompts, inputs or outputs."""
    try:
        record = read_checkpoint(path, pm_id, task_id, device_id)
        if record is None:
            return {"state": "missing"}
        snapshot = record["snapshot"]
        names = [s["name"] for s in snapshot["plan"].get("decomposition", [])]
        outputs = snapshot["subtask_outputs"]
        attempts = snapshot["execution_attempts"]
        completed = [name for name in names if name in outputs]
        direct = snapshot.get("direct_execution", {})
        uncertain = [name for name in names if name not in outputs and (
            attempts.get(name, {}).get("phase") in ("active", "processing")
            or (name in snapshot["dispatched"] and name not in attempts)
            or direct.get("state") in ("active", "returned"))]
        failed = [name for name in names if name in snapshot["terminal_failures"]]
        unstarted = [name for name in names if name not in outputs and name not in attempts
                     and name not in snapshot["dispatched"] and name not in uncertain]
        return {"state": "available", "revision": record["revision"], "updated_at": record["updated_at"],
                "phase": snapshot["phase"], "completed": len(completed), "uncertain": len(uncertain),
                "unstarted": len(unstarted), "failed": len(failed), "total": len(names),
                "direct_state": direct.get("state", ""), "delivery_state": snapshot.get("delivery", {}).get("state", ""),
                "stopped": snapshot["stopped"], "resume_supported": False}
    except Exception:
        return {"state": "unavailable"}
