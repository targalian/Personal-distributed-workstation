"""Additional per-call attribution without changing legacy resource-log columns or keys."""

import sqlite3


def migrate_usage(conn: sqlite3.Connection) -> None:
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS usage_call_details (
            usage_id TEXT PRIMARY KEY,
            attempt_id TEXT NOT NULL, purpose TEXT NOT NULL,
            outcome TEXT NOT NULL, usage_known INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_usage_call_attempt ON usage_call_details(attempt_id);
    ''')
