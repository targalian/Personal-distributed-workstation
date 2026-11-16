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
                conn.execute('INSERT OR IGNORE INTO collaboration_notices '
                             '(event_id, task_id, message, created_at) VALUES (?,?,?,?)',
                             (f'{task_id}:{state}:{timestamp}', task_id, message, timestamp))
        return self.get(task_id)

    def notice(self, event_id: str, task_id: str, message: str) -> bool:
        with self.database._get_conn() as conn:
            result = conn.execute('INSERT OR IGNORE INTO collaboration_notices '
                                  '(event_id, task_id, message, created_at) VALUES (?,?,?,?)',
                                  (event_id, task_id, message[:2000], time.time()))
            return bool(result.rowcount)

    def notices(self, task_id: str) -> list[dict]:
        rows = [dict(row) for row in self.database._get_conn().execute(
            'SELECT * FROM collaboration_notices WHERE task_id=? ORDER BY created_at DESC',
            (task_id,)).fetchall()]
        reads = {row['event_id'][6:]: row['created_at'] for row in rows
                 if row['event_id'].startswith('@read:')}
        return [dict(row, read_at=row.get('read_at') or reads.get(row['event_id']))
                for row in rows if row['message'] and not row['event_id'].startswith('@read:')]

    def mark_read(self, task_id: str, event_id: str = '') -> int:
        """Read receipts use the existing table, also on previously migrated databases."""
        count = 0
        with self.database._get_conn() as conn:
            for notice in self.notices(task_id):
                if notice['read_at'] or (event_id and notice['event_id'] != event_id):
                    continue
                count += conn.execute('INSERT OR IGNORE INTO collaboration_notices '
                    '(event_id,task_id,message,created_at) VALUES (?,?,?,?)',
                    ('@read:' + notice['event_id'], task_id, '', time.time())).rowcount
        return count

    def unread_count(self, task_id: str) -> int:
        return sum(not row['read_at'] for row in self.notices(task_id))

    def transition(self, task_id: str, channel: str, state: str, message: str) -> bool:
        """Persist consecutive-state deduplication, including transitions back to a state."""
        with self.database._get_conn() as conn:
            conn.execute('BEGIN IMMEDIATE')
            prefix = json.dumps([task_id, channel], ensure_ascii=False) + ':'
            rows = conn.execute('SELECT event_id FROM collaboration_notices WHERE task_id=? '
                                'ORDER BY rowid DESC', (task_id,)).fetchall()
            previous = next((r['event_id'] for r in rows if r['event_id'].startswith(prefix)), '')
            if previous and json.loads(previous[len(prefix):])[1] == state:
                return False
            revision = json.loads(previous[len(prefix):])[0] + 1 if previous else 1
            event_id = prefix + json.dumps([revision, state], ensure_ascii=False)
            conn.execute('INSERT INTO collaboration_notices '
                         '(event_id,task_id,message,created_at) VALUES (?,?,?,?)',
                         (event_id, task_id, message, time.time()))
        return True


def notify_transition(database: Any, task_id: str, channel: str, state: str, message: str) -> None:
    """Notification failure must not undo an already committed business operation."""
    try:
        CollaborationRepository(database).transition(task_id, channel, state, message)
    except Exception as exc:
        print(f'[Task] 通知持久化失败: {task_id}: {exc}')
