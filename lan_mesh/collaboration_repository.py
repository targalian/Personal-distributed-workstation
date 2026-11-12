"""Persistent takeover state and idempotent collaboration notices."""

import json
import sqlite3
import time
from typing import Any


def migrate_collaboration(conn: sqlite3.Connection) -> None:
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS collaboration_takeovers (
            task_id TEXT PRIMARY KEY, state TEXT NOT NULL,
            details TEXT NOT NULL, updated_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS collaboration_notices (
            event_id TEXT PRIMARY KEY, task_id TEXT NOT NULL,
            message TEXT NOT NULL, created_at REAL NOT NULL
        );
    ''')


class CollaborationRepository:
    def __init__(self, database: Any):
        self.database = database

    def get(self, task_id: str) -> dict:
        row = self.database._get_conn().execute(
            'SELECT * FROM collaboration_takeovers WHERE task_id=?', (task_id,)).fetchone()
        return dict(row) if row else {'task_id': task_id, 'state': 'automatic'}

    def set(self, task_id: str, state: str, details: dict) -> dict:
        with self.database._get_conn() as conn:
            conn.execute('BEGIN IMMEDIATE')
            previous = self.get(task_id)['state']
            if previous == 'human' and state == 'pausing':
                return self.get(task_id)
            timestamp = time.time()
            conn.execute('''INSERT INTO collaboration_takeovers VALUES (?,?,?,?)
                ON CONFLICT(task_id) DO UPDATE SET state=excluded.state,
                details=excluded.details, updated_at=excluded.updated_at''',
                (task_id, state, json.dumps(details, ensure_ascii=False), timestamp))
            if previous != state and state in {'pausing', 'human'}:
                message = ('自动执行已退出，可以人工接管' if state == 'human'
                           else '已请求暂停，等待执行退出确认')
                conn.execute('INSERT OR IGNORE INTO collaboration_notices VALUES (?,?,?,?)',
                             (f'{task_id}:{state}:{timestamp}', task_id, message, timestamp))
        return self.get(task_id)

    def notice(self, event_id: str, task_id: str, message: str) -> bool:
        with self.database._get_conn() as conn:
            result = conn.execute('INSERT OR IGNORE INTO collaboration_notices VALUES (?,?,?,?)',
                                  (event_id, task_id, message[:2000], time.time()))
            return bool(result.rowcount)

    def notices(self, task_id: str) -> list[dict]:
        return [dict(row) for row in self.database._get_conn().execute(
            'SELECT * FROM collaboration_notices WHERE task_id=? ORDER BY created_at DESC LIMIT 30',
            (task_id,)).fetchall()]
