"""Transactional schedule claims separate missed triggers from actual task submissions."""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any

from .periodic_spec import latest_due, next_due


class PeriodicRepository:
    def __init__(self, database: Any):
        self.database = database

    def list(self, conv_id: str) -> list[dict]:
        return [dict(row) for row in self.database._get_conn().execute(
            'SELECT * FROM periodic_schedules WHERE conv_id=? ORDER BY created_at DESC', (conv_id,))]

    def get(self, schedule_id: str, conv_id: str) -> dict | None:
        row = self.database._get_conn().execute('SELECT * FROM periodic_schedules WHERE schedule_id=? AND conv_id=?',
                                                (schedule_id, conv_id)).fetchone()
        return dict(row) if row else None

    def create(self, item: dict) -> dict:
        with self.database._get_conn() as conn:
            conn.execute('INSERT INTO periodic_schedules VALUES (?,?,?,?,?,?,?,?,?,?,?)', (
                item['schedule_id'], item['conv_id'], item['project_id'], item['name'],
                json.dumps(item['spec']), json.dumps(item['template'], ensure_ascii=False),
                item['catchup'], item['concurrency'], 1, item['next_run'], time.time()))
        return self.get(item['schedule_id'], item['conv_id'])

    def change(self, schedule_id: str, conv_id: str, enabled: bool, next_run: float,
               target_conv: str = '') -> dict:
        with self.database._get_conn() as conn:
            conn.execute('UPDATE periodic_schedules SET enabled=?,next_run=?,conv_id=? WHERE schedule_id=? AND conv_id=?',
                         (int(enabled), next_run, target_conv or conv_id, schedule_id, conv_id))
        return self.get(schedule_id, target_conv or conv_id)

    def claim_due(self, now: float) -> int:
        count = 0
        with self.database._get_conn() as conn:
            conn.execute('BEGIN IMMEDIATE')
            schedules = conn.execute('SELECT * FROM periodic_schedules WHERE enabled=1 AND next_run<=? ORDER BY next_run LIMIT 32', (now,)).fetchall()
            for row in schedules:
                spec = json.loads(row['spec_json'])
                due = latest_due(spec, row['next_run'], now)
                state = 'reserved'
                if row['catchup'] == 'skip' and now - row['next_run'] > 60:
                    state = 'skipped_missed'
                active = conn.execute('''SELECT 1 FROM periodic_runs r LEFT JOIN tasks t ON t.task_id=r.task_id
                    WHERE r.schedule_id=? AND (r.state IN ('reserved','retry_wait','review_required') OR
                    t.status NOT IN ('completed','failed','cancelled','accepted')) LIMIT 1''', (row['schedule_id'],)).fetchone()
                if state == 'reserved' and row['concurrency'] == 'forbid' and active:
                    state = 'skipped_busy'
                run_id = hashlib.sha256(f"{row['schedule_id']}:{due}".encode()).hexdigest()
                conn.execute('INSERT OR IGNORE INTO periodic_runs(run_id,schedule_id,due_at,state) VALUES (?,?,?,?)',
                             (run_id, row['schedule_id'], due, state))
                conn.execute('UPDATE periodic_schedules SET next_run=? WHERE schedule_id=?',
                             (next_due(spec, now), row['schedule_id']))
                count += 1
        return count

    def pending(self, now: float) -> list[dict]:
        return [dict(row) for row in self.database._get_conn().execute('''SELECT r.*,s.conv_id,s.project_id,
            s.template_json,s.enabled FROM periodic_runs r JOIN periodic_schedules s ON s.schedule_id=r.schedule_id
            WHERE r.state IN ('reserved','retry_wait') AND r.retry_at<=? ORDER BY r.due_at LIMIT 20''', (now,))]

    def finish(self, run_id: str, state: str, task_id: str = '', detail: str = '', retry_at: float = 0) -> None:
        with self.database._get_conn() as conn:
            conn.execute('UPDATE periodic_runs SET state=?,task_id=?,detail=?,retry_at=?,attempts=attempts+1 WHERE run_id=?',
                         (state, task_id, detail[:2000], retry_at, run_id))

    def runs(self, schedule_id: str) -> list[dict]:
        return [dict(row) for row in self.database._get_conn().execute(
            'SELECT * FROM periodic_runs WHERE schedule_id=? ORDER BY due_at DESC LIMIT 100', (schedule_id,))]
