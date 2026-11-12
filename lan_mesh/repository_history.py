"""Derived search over JSONL history with exact scope and immutable source anchors."""

import hashlib
import json
import sqlite3
import unicodedata
from collections.abc import Iterable
from itertools import islice

from .repository_registry import Repository


def normalize_query(text: str) -> str:
    """Normalize full-width characters and case without treating SQL as syntax."""
    return unicodedata.normalize("NFKC", text).casefold()


def stream_key(project_id: str, conv_id: str, agent_id: str = "") -> str:
    """Name the complete scope; an empty project is never a wildcard."""
    raw = json.dumps([project_id, conv_id, agent_id], ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()


class HistoryRepository(Repository):
    """The original JSONL is authoritative; every index can be recreated."""

    def fingerprint(self, stream_id: str) -> str:
        row = self.database._get_conn().execute(
            "SELECT fingerprint FROM context_streams WHERE stream_id=?", (stream_id,)
        ).fetchone()
        return row[0] if row else ""

    def replace_stream(self, project_id: str, conv_id: str, agent_id: str,
                       messages: Iterable[dict], fingerprint: str,
                       invalid_lines: int | dict = 0) -> int:
        """Atomically replace one derived stream, including its FTS rows."""
        key = stream_key(project_id, conv_id, agent_id)
        conn = self.database._get_conn()
        count, iterator = 0, iter(messages)
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            self._delete_messages(conn, key)
            fts = self._has_fts(conn)
            while batch := list(islice(iterator, 500)):
                rows = [self._row(key, msg) for msg in batch]
                conn.executemany("INSERT INTO context_messages VALUES (?,?,?,?,?,?,?)", rows)
                if fts:
                    conn.executemany("INSERT INTO context_search(anchor,search_text) VALUES (?,?)",
                                     [(row[0], row[5]) for row in rows])
                count += len(rows)
            invalid = invalid_lines.get('invalid', 0) if isinstance(invalid_lines, dict) else invalid_lines
            conn.execute("INSERT OR REPLACE INTO context_streams VALUES (?,?,?,?,?,?)",
                         (key, project_id, conv_id, agent_id, fingerprint, invalid))
        return count

    def _row(self, key: str, message: dict) -> tuple:
        raw = json.dumps(message, ensure_ascii=False, sort_keys=True, allow_nan=False)
        anchor = "msg-" + hashlib.sha256((key + raw).encode()).hexdigest()
        return (anchor, key, message['seq'], message['role'], message['content'],
                normalize_query(message['content']), message['timestamp'])

    def _has_fts(self, conn: sqlite3.Connection) -> bool:
        return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE name='context_search'").fetchone())

    def _delete_messages(self, conn: sqlite3.Connection, key: str) -> None:
        if self._has_fts(conn):
            conn.execute("""DELETE FROM context_search WHERE anchor IN
                         (SELECT anchor FROM context_messages WHERE stream_id=?)""", (key,))
        conn.execute("DELETE FROM context_messages WHERE stream_id=?", (key,))

    def prune_streams(self, valid_keys: set[str]) -> None:
        """Forget indexes for removed conversations or detached PM threads."""
        conn = self.database._get_conn()
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            for row in conn.execute("SELECT stream_id FROM context_streams").fetchall():
                if row[0] not in valid_keys:
                    self._delete_messages(conn, row[0])
                    conn.execute("DELETE FROM context_streams WHERE stream_id=?", (row[0],))

    def search(self, project_id: str, conv_id: str, agent_id: str,
               query: str, limit: int = 20, before_seq: int = 0) -> dict:
        """Literal Chinese/Unicode matching; return bounded excerpts and paging anchors."""
        query = normalize_query(query.strip())
        if not query or len(query) > 128:
            raise ValueError("搜索词长度应为1到128字符")
        conn = self.database._get_conn()
        key = stream_key(project_id, conv_id, agent_id)
        clauses = ["stream_id=?", "instr(search_text,?)>0"]
        args: list = [key, query]
        engine = 'substring'
        if len(query) >= 3 and self._has_fts(conn):
            clauses.append("anchor IN (SELECT anchor FROM context_search WHERE context_search MATCH ?)")
            args.append('"' + query.replace('"', '""') + '"')
            engine = 'fts5-trigram'
        if before_seq > 0:
            clauses.append("seq<?")
            args.append(before_seq)
        limit = max(1, min(limit, 50))
        rows = conn.execute("SELECT * FROM context_messages WHERE " + " AND ".join(clauses)
                            + " ORDER BY seq DESC LIMIT ?", (*args, limit + 1)).fetchall()
        return {'items': [self._excerpt(dict(row), query) for row in rows[:limit]],
                'next_before_seq': rows[limit - 1]['seq'] if len(rows) > limit else None,
                'engine': engine}

    def _excerpt(self, row: dict, query: str) -> dict:
        content = row.pop('content')
        start = max(0, row.pop('search_text').find(query) - 80)
        row.update(excerpt=content[start:start + 480], content_chars=len(content))
        return row

    def read_anchor(self, project_id: str, conv_id: str, agent_id: str,
                    anchor: str, offset: int = 0, limit: int = 4000) -> dict:
        """Read original text by anchor and exact stream, with character pagination."""
        row = self.database._get_conn().execute(
            "SELECT * FROM context_messages WHERE stream_id=? AND anchor=?",
            (stream_key(project_id, conv_id, agent_id), anchor)).fetchone()
        if not row:
            raise KeyError("消息不存在或不属于当前作用域")
        item = dict(row)
        content = item.pop('content')
        item.pop('search_text')
        offset, limit = max(0, offset), max(1, min(limit, 16000))
        end = offset + limit
        item.update(content=content[offset:end], content_chars=len(content), offset=offset,
                    next_offset=end if end < len(content) else None)
        return item
