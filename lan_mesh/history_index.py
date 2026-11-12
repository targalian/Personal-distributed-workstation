"""Read authoritative JSONL safely and refresh only changed conversation indexes."""

import json
import math
import re
from collections.abc import Iterator
from pathlib import Path

from .repository_history import stream_key


def history_streams(owner: object) -> list[tuple[str, str, str, Path]]:
    """Resolve existing metadata, never a caller-provided filename or project ID."""
    base = Path(owner._get_conv_dir()).resolve()
    bindings: dict[str, int] = {}
    for meta in owner._conv_index:
        for thread in meta.get('pm_threads', []):
            key = thread.get('pm_id', '')
            bindings[key] = bindings.get(key, 0) + 1
    streams = []
    for meta in owner._conv_index:
        cid, project = meta['id'], meta.get('project_id', '')
        if not _safe_id(cid):
            continue
        streams.append((project, cid, '', _safe_path(base, f'{cid}.jsonl')))
        for thread in meta.get('pm_threads', []):
            agent = thread.get('pm_id', '')
            if _safe_id(agent) and bindings[agent] == 1:
                path = _safe_path(base, f'pm_threads/{agent}.jsonl')
                streams.append((project, cid, agent, path))
    return streams


def _safe_id(value: str) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r'[A-Za-z0-9_-]{1,128}', value))


def _safe_path(base: Path, name: str) -> Path:
    path = (base / name).resolve()
    if not path.is_relative_to(base):
        raise ValueError('历史文件不在会话目录内')
    return path


def _fingerprint(path: Path) -> str:
    try:
        st = path.stat()
        return f'{st.st_dev}:{st.st_ino}:{st.st_size}:{st.st_mtime_ns}:{st.st_ctime_ns}'
    except FileNotFoundError:
        return 'missing'


def read_history_file(path: Path) -> tuple[list[dict], int]:
    """Skip individual damaged lines, preserving every intact line after them."""
    stats = {'invalid': 0}
    messages = list(iter_history_file(path, stats))
    return messages, stats['invalid']


def iter_history_file(path: Path, stats: dict) -> Iterator[dict]:
    """Stream one source line at a time; physical sequence numbers remain stable."""
    try:
        handle = path.open('rb')
    except FileNotFoundError:
        return
    with handle:
        for seq, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                raw = json.loads(line.decode('utf-8'))
                if not isinstance(raw, dict) or not isinstance(raw.get('content'), str):
                    raise ValueError('invalid message')
                role = raw.get('role', '')
                timestamp = float(raw.get('timestamp') or 0)
                if role not in ('user', 'assistant', 'system', 'tool') or not math.isfinite(timestamp):
                    raise ValueError('invalid role or timestamp')
                yield dict(seq=seq, role=role, content=raw['content'], timestamp=timestamp,
                           message_id=str(raw.get('message_id', '')))
            except (ValueError, TypeError, UnicodeError, OverflowError):
                stats['invalid'] = stats.get('invalid', 0) + 1


def _stable_messages(path: Path, mark: str, stats: dict) -> Iterator[dict]:
    yield from iter_history_file(path, stats)
    if _fingerprint(path) != mark:
        raise OSError('历史文件正在变化，请重试索引同步')


def synchronize_history(owner: object, force: bool = False) -> dict:
    """Rebuild derived SQLite rows without modifying original JSONL or memory journals."""
    repository = owner._db.repositories.history
    streams = history_streams(owner)
    valid, indexed, damaged = set(), 0, 0
    for project, cid, agent, path in streams:
        key = stream_key(project, cid, agent)
        valid.add(key)
        mark = _fingerprint(path)
        if not force and repository.fingerprint(key) == mark:
            continue
        stats = {'invalid': 0}
        messages = _stable_messages(path, mark, stats)
        indexed += repository.replace_stream(project, cid, agent, messages, mark, stats)
        damaged += stats['invalid']
    repository.prune_streams(valid)
    return {'streams': len(streams), 'indexed_messages': indexed, 'invalid_lines': damaged}
