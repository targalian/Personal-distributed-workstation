"""Add scoped memory journals and rebuildable conversation search tables."""

import sqlite3


def migrate_memory(conn: sqlite3.Connection) -> None:
    """Keep existing tables intact; FTS5 is an optional derived accelerator."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS context_streams (
            stream_id TEXT PRIMARY KEY, project_id TEXT NOT NULL,
            conv_id TEXT NOT NULL, agent_id TEXT NOT NULL,
            fingerprint TEXT NOT NULL, invalid_lines INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS context_messages (
            anchor TEXT PRIMARY KEY, stream_id TEXT NOT NULL,
            seq INTEGER NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL,
            search_text TEXT NOT NULL, timestamp REAL NOT NULL,
            UNIQUE(stream_id, seq)
        );
        CREATE TABLE IF NOT EXISTS context_memories (
            memory_id TEXT PRIMARY KEY, project_id TEXT NOT NULL,
            conv_id TEXT NOT NULL, agent_id TEXT NOT NULL, scope TEXT NOT NULL,
            revision INTEGER NOT NULL, content TEXT NOT NULL,
            source_anchor TEXT NOT NULL, source_stream_id TEXT NOT NULL, state TEXT NOT NULL,
            expires_at REAL NOT NULL DEFAULT 0,
            created_at REAL NOT NULL, updated_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_context_memory_scope
            ON context_memories(project_id, conv_id, agent_id, state);
        CREATE TABLE IF NOT EXISTS context_memory_revisions (
            memory_id TEXT NOT NULL, revision INTEGER NOT NULL,
            snapshot_json TEXT NOT NULL, changed_at REAL NOT NULL,
            PRIMARY KEY(memory_id, revision)
        );
    """)
    try:
        conn.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS context_search
                     USING fts5(anchor UNINDEXED, search_text, tokenize='trigram')""")
    except sqlite3.OperationalError:
        pass  # Literal substring search also supports builds without FTS5.
