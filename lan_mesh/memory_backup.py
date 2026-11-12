"""Consistent project-scoped memory snapshots preserving raw histories and revision journals."""

import json
import tempfile
from pathlib import Path

from .history_index import _safe_id, history_streams, synchronize_history
from .private_bundle import PrivateBundleStore, atomic_json


def export_memory(chat: object, store: PrivateBundleStore, conv_id: str) -> dict:
    """Hold the chat mutation lock and a DB snapshot while copying source files."""
    with chat._component_lock:
        meta = next((m for m in chat._conv_index if m['id'] == conv_id), None)
        if meta is None:
            raise KeyError('对话不存在')
        project = meta.get('project_id', '')
        selected = [m for m in chat._conv_index if m.get('project_id', '') == project] if project else [meta]
        cids = {m['id'] for m in selected}
        agents = [t.get('pm_id') for m in selected for t in m.get('pm_threads', [])]
        if len(cids) != len(selected) or any(not _safe_id(i) for i in list(cids)+agents) or len(set(agents)) != len(agents):
            raise ValueError('对话或PM线程标识重复或无效')
        synchronize_history(chat)
        files = {(f'history/{cid}/threads/{agent}.jsonl' if agent else f'history/{cid}/main.jsonl'): path
                 for proj, cid, agent, path in history_streams(chat) if cid in cids and path.exists()}
        scope = dict(project_id=project, conv_ids=sorted(cids))
        conn = chat._db._get_conn()
        with tempfile.TemporaryDirectory(prefix='.export-', dir=store.root) as temp, conn:
            conn.execute('BEGIN IMMEDIATE')
            folder = Path(temp)
            atomic_json(folder / 'conversations.json', selected)
            placeholders = ','.join('?' for _ in cids)
            clause = f"project_id=? AND (scope='project' OR conv_id IN ({placeholders}))"
            args = (project, *sorted(cids))
            _dump_rows(folder / 'memories.jsonl', conn.execute(
                'SELECT * FROM context_memories WHERE '+clause+' ORDER BY memory_id', args))
            _dump_rows(folder / 'revisions.jsonl', conn.execute(
                'SELECT * FROM context_memory_revisions WHERE memory_id IN '
                '(SELECT memory_id FROM context_memories WHERE '+clause+') ORDER BY memory_id,revision', args))
            files.update({name:(folder / name) for name in ('conversations.json','memories.jsonl','revisions.jsonl')})
            result = store.create('memory', scope, files)
        print(f'[Chat] 私有记忆快照已创建: {result["bundle_id"][:12]} conversations={len(cids)}')
        return result


def _dump_rows(path: Path, rows: object) -> None:
    with path.open('w', encoding='utf-8') as handle:
        for row in rows:
            handle.write(json.dumps(dict(row), ensure_ascii=False, allow_nan=False) + '\n')
