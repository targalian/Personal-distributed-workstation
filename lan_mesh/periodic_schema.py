"""Durable schedules, trigger receipts, submission identities and IM inboxes."""

import sqlite3


def migrate_periodic(conn: sqlite3.Connection) -> None:
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS periodic_schedules (
            schedule_id TEXT PRIMARY KEY, conv_id TEXT NOT NULL, project_id TEXT NOT NULL,
            name TEXT NOT NULL, spec_json TEXT NOT NULL, template_json TEXT NOT NULL,
            catchup TEXT NOT NULL, concurrency TEXT NOT NULL, enabled INTEGER NOT NULL,
            next_run REAL NOT NULL, created_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_periodic_due ON periodic_schedules(enabled,next_run);
        CREATE TABLE IF NOT EXISTS periodic_runs (
            run_id TEXT PRIMARY KEY, schedule_id TEXT NOT NULL, due_at REAL NOT NULL,
            state TEXT NOT NULL, task_id TEXT NOT NULL DEFAULT '', detail TEXT NOT NULL DEFAULT '',
            retry_at REAL NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0,
            UNIQUE(schedule_id,due_at)
        );
        CREATE TABLE IF NOT EXISTS periodic_submissions (
            event_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, task_id TEXT NOT NULL,
            state TEXT NOT NULL, owner TEXT NOT NULL, response_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE TABLE IF NOT EXISTS im_reply_outbox (
            bot_id TEXT NOT NULL,event_id TEXT NOT NULL,part INTEGER NOT NULL,room_id TEXT NOT NULL,
            message TEXT NOT NULL,state TEXT NOT NULL,retry_at REAL NOT NULL DEFAULT 0,
            PRIMARY KEY(bot_id,event_id,part)
        );
        CREATE TABLE IF NOT EXISTS im_bindings (
            channel TEXT NOT NULL, room_id TEXT NOT NULL, conv_id TEXT NOT NULL,
            project_id TEXT NOT NULL, enabled INTEGER NOT NULL,
            PRIMARY KEY(channel,room_id)
        );
        CREATE TABLE IF NOT EXISTS im_inbox (
            channel TEXT NOT NULL, event_id TEXT NOT NULL, room_id TEXT NOT NULL,
            conv_id TEXT NOT NULL, project_id TEXT NOT NULL,
            fingerprint TEXT NOT NULL, state TEXT NOT NULL, response_json TEXT NOT NULL DEFAULT '{}',
            created_at REAL NOT NULL, PRIMARY KEY(channel,event_id)
        );
    ''')
