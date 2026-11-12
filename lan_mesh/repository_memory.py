"""Scoped, versioned user memories; no automatic promotion of model or tool output."""

import json
import time
import uuid

from .repository_registry import Repository
from .repository_history import stream_key


class MemoryRepository(Repository):
    """Record every accepted change with compare-and-swap revision checks."""

    def _visible(self, row: dict, project_id: str, conv_id: str, agent_id: str) -> bool:
        if row['project_id'] != project_id:
            return False
        if row['scope'] == 'project':
            return bool(project_id)
        if row['conv_id'] != conv_id:
            return False
        return row['scope'] == 'conversation' or (
            row['scope'] == 'agent' and bool(agent_id) and row['agent_id'] == agent_id)

    def get(self, memory_id: str, project_id: str, conv_id: str, agent_id: str) -> dict:
        row = self.database._get_conn().execute(
            "SELECT * FROM context_memories WHERE memory_id=?", (memory_id,)).fetchone()
        if not row or not self._visible(dict(row), project_id, conv_id, agent_id):
            raise KeyError("记忆不存在或不属于当前作用域")
        return dict(row)

    def list_visible(self, project_id: str, conv_id: str, agent_id: str,
                     include_inactive: bool = False, limit: int = 100, offset: int = 0) -> list[dict]:
        """Apply scope and source availability before returning or injecting content."""
        conn = self.database._get_conn()
        rows = conn.execute("""
            SELECT m.*, s.conv_id AS source_conv_id, s.agent_id AS source_agent_id
            FROM context_memories m
            LEFT JOIN context_messages h ON h.anchor=m.source_anchor
            LEFT JOIN context_streams s ON s.stream_id=h.stream_id
            WHERE m.project_id=? AND (
                (m.scope='project' AND m.project_id!='') OR
                (m.scope='conversation' AND m.conv_id=?) OR
                (m.scope='agent' AND m.conv_id=? AND m.agent_id=? AND m.agent_id!=''))
            AND (? OR (m.state='active' AND (m.expires_at=0 OR m.expires_at>?)
                       AND s.project_id=m.project_id))
            ORDER BY m.updated_at DESC, m.memory_id LIMIT ? OFFSET ?
        """, (project_id, conv_id, conv_id, agent_id, include_inactive, time.time(),
              max(1, min(limit, 200)), max(0, offset))).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item['effective_state'] = self._effective_state(item)
            items.append(item)
        return items

    def _effective_state(self, item: dict) -> str:
        if item['state'] != 'active':
            return item['state']
        if item['expires_at'] and item['expires_at'] <= time.time():
            return 'expired'
        return 'active' if item['source_conv_id'] is not None else 'source_unavailable'

    def create(self, project_id: str, conv_id: str, agent_id: str, scope: str,
               content: str, source_anchor: str, expires_at: float) -> dict:
        """Commit the first version together with its audit record."""
        now = time.time()
        row = dict(memory_id='mem-' + uuid.uuid4().hex, project_id=project_id,
                   conv_id='' if scope == 'project' else conv_id,
                   agent_id=agent_id if scope == 'agent' else '', scope=scope,
                   revision=1, content=content, source_anchor=source_anchor,
                   source_stream_id=stream_key(project_id, conv_id, agent_id), state='active',
                   expires_at=expires_at, created_at=now, updated_at=now)
        conn = self.database._get_conn()
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            self._check_source(conn, source_anchor, project_id, conv_id, agent_id)
            conn.execute("INSERT INTO context_memories VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         tuple(row.values()))
            self._audit(conn, row)
        return row

    def revise(self, memory_id: str, project_id: str, conv_id: str, agent_id: str,
               expected_revision: int, changes: dict) -> dict:
        """Serialize competing edits and preserve revoked records across restarts."""
        conn = self.database._get_conn()
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self.get(memory_id, project_id, conv_id, agent_id)
            if row['revision'] != expected_revision:
                raise ValueError("revision_conflict")
            if row['state'] != 'active':
                raise ValueError("已撤销记忆不可恢复；需从有效来源明确新建")
            if changes.get('state') != 'revoked':
                self._check_source(conn, changes['source_anchor'], project_id, conv_id, agent_id)
            row.update(changes)
            if changes.get('state') != 'revoked':
                row['source_stream_id'] = stream_key(project_id, conv_id, agent_id)
            row.update(revision=expected_revision + 1, updated_at=time.time())
            conn.execute("""UPDATE context_memories SET revision=?,content=?,source_anchor=?,source_stream_id=?,
                         state=?,expires_at=?,updated_at=? WHERE memory_id=?""",
                         (row['revision'], row['content'], row['source_anchor'], row['source_stream_id'], row['state'],
                          row['expires_at'], row['updated_at'], memory_id))
            self._audit(conn, row)
        return row

    def _check_source(self, conn: object, anchor: str, project_id: str,
                      conv_id: str, agent_id: str) -> None:
        row = conn.execute("""SELECT 1 FROM context_messages h JOIN context_streams s
            ON h.stream_id=s.stream_id WHERE h.anchor=? AND h.role='user'
            AND s.project_id=? AND s.conv_id=? AND s.agent_id=?""",
                           (anchor, project_id, conv_id, agent_id)).fetchone()
        if not row:
            raise ValueError("记忆来源必须是当前作用域中的用户原消息")

    def _audit(self, conn: object, row: dict) -> None:
        conn.execute("INSERT INTO context_memory_revisions VALUES (?,?,?,?)",
                     (row['memory_id'], row['revision'], json.dumps(row, ensure_ascii=False),
                      row['updated_at']))

    def revisions(self, memory_id: str, project_id: str, conv_id: str, agent_id: str) -> list[dict]:
        """Read the audit history only after checking the current scope."""
        self.get(memory_id, project_id, conv_id, agent_id)
        return [json.loads(row[0]) for row in self.database._get_conn().execute(
            "SELECT snapshot_json FROM context_memory_revisions WHERE memory_id=? ORDER BY revision",
            (memory_id,))]

    def is_recorded_source(self, anchor: str) -> bool:
        """Do not reintroduce corrected/revoked/expired memories through automatic history recall."""
        return bool(self.database._get_conn().execute("""SELECT 1 FROM context_memory_revisions
            WHERE json_extract(snapshot_json, '$.source_anchor')=? LIMIT 1""", (anchor,)).fetchone())

    def forget_stream(self, stream_id: str) -> int:
        """Clearing history permanently revokes memories sourced from that stream."""
        conn = self.database._get_conn()
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute("""SELECT * FROM context_memories WHERE state='active' AND
                source_stream_id=?""",
                                (stream_id,)).fetchall()
            for value in rows:
                row = dict(value)
                row.update(state='revoked', revision=row['revision'] + 1, updated_at=time.time())
                conn.execute("UPDATE context_memories SET state=?,revision=?,updated_at=? WHERE memory_id=?",
                             (row['state'], row['revision'], row['updated_at'], row['memory_id']))
                self._audit(conn, row)
        return len(rows)
