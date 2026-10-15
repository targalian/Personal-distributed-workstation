"""Persist execution claims and results so transport retries cannot start a second copy."""
import hashlib
import json
import os
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path

PROCESS_OWNER = f"{os.getpid()}-{uuid.uuid4().hex}"


class ExecutionReceipts:
    """A claim survives restart; an unfinished claim must never be executed again."""

    def __init__(self, path: Path, owner: str = PROCESS_OWNER):
        self.path = Path(path)
        self.owner = owner
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS execution_receipts (
                execution_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
                identity TEXT NOT NULL, owner TEXT NOT NULL,
                result TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )""")

    def _connect(self):
        conn = sqlite3.connect(str(self.path), timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def _view(self, row) -> dict:
        result = json.loads(row["result"]) if row["result"] is not None else {
            "status": "started" if row["owner"] == self.owner else "unknown",
            "error": "" if row["owner"] == self.owner else "执行进程已变化，结果待核实；禁止自动重执行",
        }
        return {**result, **json.loads(row["identity"])}

    def claim(self, payload: dict) -> tuple[bool, dict]:
        """Atomically claim an ID, or return its immutable matching receipt."""
        fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        identity = {key: payload.get(key, "") for key in (
            "subtask_id", "parent_task_id", "pm_id", "reporter_id", "name")}
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM execution_receipts WHERE execution_id=?",
                               (payload["subtask_id"],)).fetchone()
            if row:
                if row["fingerprint"] != fingerprint:
                    raise ValueError("执行编号已用于不同请求")
                return False, self._view(row)
            conn.execute("INSERT INTO execution_receipts (execution_id,fingerprint,identity,owner) VALUES (?,?,?,?)",
                         (payload["subtask_id"], fingerprint, json.dumps(identity, ensure_ascii=False), self.owner))
            conn.commit()
            return True, {**identity, "status": "started"}
        finally:
            conn.close()

    def finish(self, execution_id: str, result: dict) -> None:
        """Publish once; a failed write leaves an uncertain claim, never a new execution."""
        encoded = json.dumps(result, ensure_ascii=False)
        with closing(self._connect()) as conn:
            conn.execute("UPDATE execution_receipts SET result=? WHERE execution_id=? AND owner=? AND result IS NULL",
                         (encoded, execution_id, self.owner))
            conn.commit()

    def get(self, execution_id: str) -> dict | None:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT * FROM execution_receipts WHERE execution_id=?", (execution_id,)).fetchone()
            return self._view(row) if row else None
