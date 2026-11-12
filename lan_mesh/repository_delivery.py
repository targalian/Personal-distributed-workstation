"""Durable delivery, inbox deduplication, and task execution receipts."""

from __future__ import annotations

import json
import time
from typing import Optional

from .repository_registry import Repository


class DeliveryRepository(Repository):
    """Persist delivery state through the owning Database thread-local connection."""

    def enqueue(self, delivery_id: str, destination: str, payload: dict,
                kind: str = "result") -> bool:
        """Insert a delivery once and return whether this call inserted it."""
        now = time.time()
        conn = self.database._get_conn()
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        cur = conn.execute(
            """INSERT OR IGNORE INTO mesh_delivery_outbox
               (delivery_id, kind, destination, payload_json, next_attempt_at,
                created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (delivery_id, kind, destination,
             raw, now, now, now),
        )
        conn.commit()
        row = conn.execute("SELECT destination, payload_json FROM mesh_delivery_outbox WHERE delivery_id=?",
                           (delivery_id,)).fetchone()
        if row["destination"] != destination or json.loads(row["payload_json"]) != payload:
            raise ValueError("outbox ID reused with a different message")
        return cur.rowcount > 0

    def claim_due(self, limit: int = 20, lease_seconds: float = 30.0) -> list[dict]:
        """Claim pending or expired leased deliveries for one sender."""
        now = time.time()
        conn = self.database._get_conn()
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                """SELECT * FROM mesh_delivery_outbox
                   WHERE (state = 'pending' AND next_attempt_at <= ?)
                      OR (state = 'sending' AND lease_until <= ?)
                   ORDER BY created_at LIMIT ?""", (now, now, max(1, limit)),
            ).fetchall()
            claimed = []
            for row in rows:
                conn.execute(
                    """UPDATE mesh_delivery_outbox SET state='sending', attempts=attempts+1,
                       lease_until=?, updated_at=? WHERE delivery_id=?""",
                    (now + lease_seconds, now, row["delivery_id"]),
                )
                item = dict(row)
                item["attempts"] = int(item["attempts"]) + 1
                claimed.append(item)
        return claimed

    def acknowledge(self, delivery_id: str) -> bool:
        """Mark a delivery acknowledged; repeated acknowledgements are harmless."""
        conn = self.database._get_conn()
        cur = conn.execute(
            """UPDATE mesh_delivery_outbox SET state='acked', lease_until=0,
               updated_at=? WHERE delivery_id=? AND state != 'acked'""",
            (time.time(), delivery_id),
        )
        conn.commit()
        return cur.rowcount > 0

    def supersede(self, delivery_id: str) -> None:
        """Record cancelled/obsolete dispatches separately from remote acknowledgements."""
        conn = self.database._get_conn()
        conn.execute("""UPDATE mesh_delivery_outbox SET state='superseded', lease_until=0,
                       updated_at=? WHERE delivery_id=?""", (time.time(), delivery_id))
        conn.commit()

    def fail(self, delivery_id: str, error: str, backoff: float) -> None:
        """Return a leased delivery to the retry queue."""
        now = time.time()
        conn = self.database._get_conn()
        conn.execute(
            """UPDATE mesh_delivery_outbox SET state='pending', lease_until=0,
               next_attempt_at=?, last_error=?, updated_at=? WHERE delivery_id=?""",
            (now + max(0.1, backoff), str(error)[:1000], now, delivery_id),
        )
        conn.commit()

    def accept_inbox(self, delivery_id: str, kind: str, payload: dict) -> str:
        """Record an incoming item and return new, processed, or in_progress."""
        now = time.time()
        conn = self.database._get_conn()
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        cur = conn.execute(
            """INSERT OR IGNORE INTO mesh_delivery_inbox
               (delivery_id, kind, payload_json, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?)""",
            (delivery_id, kind, raw, now, now),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM mesh_delivery_inbox WHERE delivery_id=?",
                           (delivery_id,)).fetchone()
        if row["payload_json"] != raw:
            raise ValueError("delivery ID reused with different payload")
        return "new" if cur.rowcount else row["state"]

    def complete_inbox(self, delivery_id: str, result: dict) -> None:
        """Mark an incoming item processed after its handler has returned."""
        conn = self.database._get_conn()
        conn.execute(
            """UPDATE mesh_delivery_inbox SET state='processed', result_json=?,
               updated_at=? WHERE delivery_id=?""",
            (json.dumps(result, ensure_ascii=False), time.time(), delivery_id),
        )
        conn.commit()

    def begin_execution(self, execution_id: str, task_id: str, attempt_id: str,
                        payload: dict) -> dict:
        """Create an execution receipt or return the cached receipt."""
        now = time.time()
        conn = self.database._get_conn()
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        cur = conn.execute(
            """INSERT OR IGNORE INTO mesh_task_executions
               (execution_id, task_id, attempt_id, payload_json, started_at,
                updated_at, status) VALUES (?, ?, ?, ?, ?, ?, 'queued')""",
            (execution_id, task_id, attempt_id,
             raw, now, now),
        )
        conn.commit()
        row = dict(conn.execute("SELECT * FROM mesh_task_executions WHERE execution_id=?",
                                (execution_id,)).fetchone())
        if row["payload_json"] != raw:
            raise ValueError("execution ID reused with different payload")
        return {"new": bool(cur.rowcount), **row}

    def finish_execution(self, execution_id: str, status: str, result: dict,
                         destination: str = "", report: Optional[dict] = None) -> bool:
        """Persist an execution result before sending its network callback."""
        conn = self.database._get_conn()
        now = time.time()
        with conn:
            cur = conn.execute(
                """UPDATE mesh_task_executions SET status=?, result_json=?,
                   completed_at=?, updated_at=? WHERE execution_id=?
                   AND status IN ('queued','started','cancel_requested')
                   AND (status != 'cancel_requested' OR ? = 'cancelled')""",
                (status, json.dumps(result, ensure_ascii=False), now, now, execution_id, status),
            )
            if not cur.rowcount:
                return False
            if report is not None:
                conn.execute(
                    """INSERT OR IGNORE INTO mesh_delivery_outbox
                       (delivery_id, kind, destination, payload_json, created_at, updated_at)
                       VALUES (?, 'result', ?, ?, ?, ?)""",
                    (report["result_id"], destination,
                     json.dumps(report, ensure_ascii=False), now, now),
                )
        return True

    def executions(self, status: str) -> list[dict]:
        """Read execution receipts in one state, including complete task payloads."""
        return [dict(row) for row in self.database._get_conn().execute(
            "SELECT * FROM mesh_task_executions WHERE status=? ORDER BY started_at",
            (status,)).fetchall()]

    def claim_execution(self, execution_id: str) -> bool:
        """Only one process can transition a queued execution to started."""
        conn = self.database._get_conn()
        cur = conn.execute("""UPDATE mesh_task_executions SET status='started',
                           updated_at=? WHERE execution_id=? AND status='queued'
                           AND NOT EXISTS (SELECT 1 FROM mesh_task_executions AS busy
                               WHERE busy.status IN ('started','cancel_requested')
                               AND busy.execution_id != mesh_task_executions.execution_id
                               AND json_extract(busy.payload_json, '$.reporter_id') =
                                   json_extract(mesh_task_executions.payload_json, '$.reporter_id'))""",
                           (time.time(), execution_id))
        conn.commit()
        return bool(cur.rowcount)

    def cancel_execution(self, execution_id: str) -> str:
        """Persist cancellation before signalling any runtime; it survives restart."""
        conn = self.database._get_conn()
        conn.execute("""UPDATE mesh_task_executions SET status='cancel_requested',
                       updated_at=? WHERE execution_id=? AND status IN ('queued','started')""",
                     (time.time(), execution_id))
        conn.commit()
        row = conn.execute("SELECT status FROM mesh_task_executions WHERE execution_id=?",
                           (execution_id,)).fetchone()
        return row["status"]

    def execution_status(self, execution_id: str) -> str:
        """Read the persistent cancellation fence before final result publication."""
        row = self.database._get_conn().execute(
            "SELECT status FROM mesh_task_executions WHERE execution_id=?", (execution_id,)).fetchone()
        return row["status"] if row else ""

    def require_review(self, delivery_id: str, reason: str) -> None:
        """Expose interrupted PM application without automatically repeating effects."""
        conn = self.database._get_conn()
        conn.execute("""UPDATE mesh_delivery_inbox SET state='review_required',
                       result_json=?, updated_at=? WHERE delivery_id=?""",
                     (json.dumps({"error": reason}, ensure_ascii=False), time.time(), delivery_id))
        conn.commit()

    def received_results(self, limit: int = 20) -> list[dict]:
        """Read durable results awaiting their PM instance."""
        return [dict(row) for row in self.database._get_conn().execute(
            """SELECT * FROM mesh_delivery_inbox WHERE kind='result'
               AND state='received' ORDER BY created_at LIMIT ?""", (limit,)).fetchall()]

    def claim_result(self, delivery_id: str) -> bool:
        """Reserve application separately from durable network receipt."""
        conn = self.database._get_conn()
        cur = conn.execute("""UPDATE mesh_delivery_inbox SET state='applying',
                           updated_at=? WHERE delivery_id=? AND state='received'""",
                           (time.time(), delivery_id))
        conn.commit()
        return bool(cur.rowcount)

    def recover_applying(self) -> int:
        """Expose crash ambiguity for review rather than repeat PM side effects."""
        conn = self.database._get_conn()
        cur = conn.execute("""UPDATE mesh_delivery_inbox SET state='review_required',
                           updated_at=? WHERE state='applying'""", (time.time(),))
        conn.commit()
        return cur.rowcount

    def save_pm(self, pm_id: str, task_id: str, snapshot: dict,
                status: str = "running", delivery_id: str = "") -> None:
        """Commit PM state and its processed result receipt in one transaction."""
        conn = self.database._get_conn()
        now = time.time()
        with conn:
            conn.execute("""INSERT INTO mesh_delivery_pm_state
                         (pm_id, task_id, status, state_json, updated_at)
                         VALUES (?, ?, ?, ?, ?) ON CONFLICT(pm_id) DO UPDATE SET
                         task_id=excluded.task_id, status=excluded.status,
                         state_json=excluded.state_json, updated_at=excluded.updated_at""",
                         (pm_id, task_id, status, json.dumps(snapshot, ensure_ascii=False), now))
            if delivery_id:
                conn.execute("""UPDATE mesh_delivery_inbox SET state='processed',
                             updated_at=? WHERE delivery_id=?""", (now, delivery_id))

    def get_pm(self, pm_id: str) -> Optional[dict]:
        """Read the local delivery checkpoint without contacting Secretary."""
        row = self.database._get_conn().execute(
            "SELECT * FROM mesh_delivery_pm_state WHERE pm_id=?", (pm_id,)).fetchone()
        return dict(row) if row else None

    def list_outbox(self, state: str = "") -> list[dict]:
        """List durable deliveries for diagnostics and acceptance tests."""
        conn = self.database._get_conn()
        if state:
            rows = conn.execute(
                "SELECT * FROM mesh_delivery_outbox WHERE state=? ORDER BY created_at",
                (state,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM mesh_delivery_outbox ORDER BY created_at"
            ).fetchall()
        return [dict(row) for row in rows]
