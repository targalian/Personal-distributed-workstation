"""Explicit failover restore with a durable prepare record and an atomic visibility index."""

import json
import os
import re
import tempfile
import time
import zipfile
from pathlib import Path

from .history_index import _safe_id, _safe_path, synchronize_history
from .private_bundle import PrivateBundleStore, atomic_json, file_digest
from .runtime_artifacts import _sync_directory


def migrate_private_restore(conn: object) -> None:
    conn.execute('''CREATE TABLE IF NOT EXISTS private_restore_jobs (
        bundle_id TEXT PRIMARY KEY, kind TEXT NOT NULL, scope_json TEXT NOT NULL,
        state TEXT NOT NULL, created_at REAL NOT NULL, completed_at REAL NOT NULL DEFAULT 0)''')


def memory_metadata(store: PrivateBundleStore, bundle_id: str, manifest: dict) -> list[dict]:
    """Validate exact conversation/thread ownership before constructing local paths."""
    if manifest.get('kind') != 'memory':
        raise ValueError('不是记忆快照')
    if manifest['files']['conversations.json']['bytes'] > 4 * 1024 * 1024:
        raise ValueError('对话元数据过大')
    with zipfile.ZipFile(store.path(bundle_id)) as archive:
        rows = json.loads(archive.read('conversations.json'))
    if not isinstance(rows, list) or not rows or len(rows) > 1000:
        raise ValueError('对话清单无效')
    ids, agents = set(), set()
    for meta in rows:
        if not isinstance(meta, dict) or not isinstance(meta.get('pm_threads', []), list):
            raise ValueError('对话元数据格式无效')
        cid = meta.get('id')
        if not _safe_id(cid) or cid in ids or meta.get('project_id', '') != manifest['scope']['project_id']:
            raise ValueError('对话或项目作用域不一致')
        ids.add(cid)
        for thread in meta.get('pm_threads', []):
            if not isinstance(thread, dict):
                raise ValueError('PM线程元数据格式无效')
            agent = thread.get('pm_id')
            if not _safe_id(agent) or agent in agents:
                raise ValueError('PM线程标识重复或无效')
            agents.add(agent)
    if sorted(ids) != sorted(manifest['scope']['conv_ids']):
        raise ValueError('对话清单与快照作用域不一致')
    allowed = {'conversations.json', 'memories.jsonl', 'revisions.jsonl'} | set(_history_paths(rows))
    if not set(manifest['files']).issubset(allowed) or not {'memories.jsonl','revisions.jsonl'}.issubset(manifest['files']):
        raise ValueError('快照含未登记文件或缺少记忆账本')
    return rows


def _history_paths(metadata: list[dict]) -> dict[str, str]:
    paths = {}
    for meta in metadata:
        cid = meta['id']
        paths[f'history/{cid}/main.jsonl'] = f'{cid}.jsonl'
        for thread in meta.get('pm_threads', []):
            agent = thread['pm_id']
            paths[f'history/{cid}/threads/{agent}.jsonl'] = f'pm_threads/{agent}.jsonl'
    return paths


def _check_target(chat: object, metadata: list[dict], bundle_id: str, prepared: bool) -> bool:
    ids = {m['id'] for m in metadata}
    existing = [m for m in chat._conv_index if m['id'] in ids]
    if len(existing) == len(ids) and prepared and all(m.get('_memory_bundle_id') == bundle_id for m in existing):
        return True
    project = metadata[0].get('project_id', '')
    if existing or (project and any(m.get('project_id') == project for m in chat._conv_index)):
        raise ValueError('目标已有此会话或项目记忆，拒绝覆盖；请在干净的备用实例恢复')
    new_agents = {t['pm_id'] for m in metadata for t in m.get('pm_threads', [])}
    if any(t.get('pm_id') in new_agents for m in chat._conv_index for t in m.get('pm_threads', [])):
        raise ValueError('目标PM线程标识已绑定到其他会话')
    return False


def _insert_journals(chat: object, store: PrivateBundleStore, bundle_id: str,
                     metadata: list[dict], manifest: dict) -> None:
    conn = chat._db._get_conn()
    rows, revisions = {}, {}
    for row in store.json_rows(bundle_id, 'memories.jsonl'):
        _check_memory(row, metadata)
        if row['memory_id'] in rows:
            raise ValueError('记忆标识重复')
        rows[row['memory_id']] = row
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        if conn.execute('SELECT 1 FROM private_restore_jobs WHERE bundle_id=?', (bundle_id,)).fetchone():
            return
        for row in rows.values():
            keys = ('memory_id','project_id','conv_id','agent_id','scope','revision','content','source_anchor',
                    'source_stream_id','state','expires_at','created_at','updated_at')
            conn.execute('INSERT INTO context_memories VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)', tuple(row[k] for k in keys))
        for audit in store.json_rows(bundle_id, 'revisions.jsonl'):
            mid, revision = audit['memory_id'], audit['revision']
            snapshot = json.loads(audit['snapshot_json'])
            _check_memory(snapshot, metadata, historical=True)
            if mid not in rows or snapshot['memory_id'] != mid or snapshot['revision'] != revision or revision != revisions.get(mid, 0) + 1:
                raise ValueError('修订历史不连续或身份不一致')
            if revision == rows[mid]['revision'] and snapshot != rows[mid]:
                raise ValueError('最新修订与记忆内容不一致')
            conn.execute('INSERT INTO context_memory_revisions VALUES (?,?,?,?)',
                         (mid, revision, audit['snapshot_json'], audit['changed_at']))
            revisions[mid] = revision
        if any(revisions.get(mid) != row['revision'] for mid, row in rows.items()):
            raise ValueError('记忆缺少完整修订历史')
        conn.execute('INSERT INTO private_restore_jobs VALUES (?,?,?,?,?,0)',
                     (bundle_id, 'memory', json.dumps(manifest['scope']), 'prepared', time.time()))


def _check_memory(row: dict, metadata: list[dict], historical: bool = False) -> None:
    project = metadata[0].get('project_id', '')
    by_id = {m['id']:m for m in metadata}
    if row['project_id'] != project or row['scope'] not in {'project','conversation','agent'}:
        raise ValueError('记忆越出快照范围')
    if row['scope'] == 'project':
        valid = bool(project) and not row['conv_id'] and not row['agent_id']
    else:
        meta = by_id.get(row['conv_id'], {})
        valid = bool(meta) and (not row['agent_id'] if row['scope'] == 'conversation' else
                               (_safe_id(row['agent_id']) and (historical or row['state'] == 'revoked' or
                                any(t['pm_id'] == row['agent_id'] for t in meta.get('pm_threads', [])))))
    if not valid or type(row['revision']) is not int or row['revision'] < 1 or row['state'] not in {'active','revoked'}:
        raise ValueError('记忆归属、修订或状态无效')
    if not re.fullmatch(r'mem-[0-9a-f]{32}', row['memory_id']) or not isinstance(row['content'], str):
        raise ValueError('记忆标识或正文无效')


def _install_sources(chat: object, store: PrivateBundleStore, bundle_id: str,
                     metadata: list[dict], manifest: dict, prepared: bool) -> None:
    base = Path(chat._get_conv_dir()).resolve()
    with zipfile.ZipFile(store.path(bundle_id)) as archive:
        for name, relative in _history_paths(metadata).items():
            target = _safe_path(base, relative)
            if (base / relative).is_symlink():
                raise ValueError('恢复目标不可为符号链接')
            if target.exists():
                if not prepared or name not in manifest['files'] or file_digest(target) != manifest['files'][name]['sha256']:
                    raise ValueError('目标存在不同的历史文件，拒绝覆盖')
                continue
            if name not in manifest['files']:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix='.restore-', dir=target.parent)
            try:
                with os.fdopen(fd, 'wb') as dst, archive.open(name) as src:
                    PrivateBundleStore._copy(src, dst)
                    dst.flush(); os.fsync(dst.fileno())
                os.link(temporary, target)  # Fail if a concurrent writer created the target.
                _sync_directory(target.parent)
            finally:
                Path(temporary).unlink(missing_ok=True)


def restore_memory(chat: object, store: PrivateBundleStore, bundle_id: str) -> dict:
    """Make restored conversations visible only after all journals and files exist."""
    manifest = store.validate(bundle_id)
    metadata = memory_metadata(store, bundle_id, manifest)
    with chat._component_lock:
        conn = chat._db._get_conn()
        job = conn.execute('SELECT state FROM private_restore_jobs WHERE bundle_id=?', (bundle_id,)).fetchone()
        if job and job['state'] == 'completed':
            return dict(bundle_id=bundle_id, state='completed', already_restored=True)
        visible = _check_target(chat, metadata, bundle_id, bool(job))
        if not visible:
            _preflight_files(chat, metadata, manifest, bool(job))
            _insert_journals(chat, store, bundle_id, metadata, manifest)
            _install_sources(chat, store, bundle_id, metadata, manifest, bool(job))
            restored = [dict(m, _memory_bundle_id=bundle_id) for m in metadata]
            updated = chat._conv_index + restored
            atomic_json(Path(chat._get_conv_dir()) / 'index.json', updated)
            chat._conv_index = updated
            for meta in metadata:
                chat._conversations.pop(meta['id'], None)
                for thread in meta.get('pm_threads', []):
                    chat._pm_thread_messages.pop(thread['pm_id'], None)
        synchronize_history(chat)
        with conn:
            conn.execute("UPDATE private_restore_jobs SET state='completed',completed_at=? WHERE bundle_id=?", (time.time(), bundle_id))
        print(f'[Chat] 记忆快照已恢复: {bundle_id[:12]} conversations={len(metadata)}')
        return dict(bundle_id=bundle_id, state='completed', conv_ids=[m['id'] for m in metadata])


def _preflight_files(chat: object, metadata: list[dict], manifest: dict, prepared: bool) -> None:
    base = Path(chat._get_conv_dir()).resolve()
    for name, relative in _history_paths(metadata).items():
        path = _safe_path(base, relative)
        if (base / relative).is_symlink():
            raise ValueError('恢复目标不可为符号链接')
        if path.exists() and (not prepared or name not in manifest['files'] or file_digest(path) != manifest['files'][name]['sha256']):
            raise ValueError('目标存在不同的历史文件，拒绝覆盖')
