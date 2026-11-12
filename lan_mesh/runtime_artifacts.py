"""Atomic, immutable ReAct observations and checkpoints owned by one execution."""

import hashlib
import json
import os
import re
import tempfile
import time
import uuid
from pathlib import Path
from .artifact_lock import guarded


def artifact_root(shared_folder: str) -> Path:
    """Use the configured work directory; task inputs cannot override storage."""
    base = Path(shared_folder).expanduser().resolve()
    root = base / '.runtime_artifacts'
    if root.is_symlink():
        raise ValueError('产物根目录不可为符号链接')
    return root


def _encoded(value: dict) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode('utf-8')


def _sync_directory(path: Path) -> None:
    if os.name == 'posix':
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


class RuntimeArtifacts:
    """No automatic replay: an interrupted run is evidence for explicit review."""

    def __init__(self, root: Path, execution_id: str, scope: dict):
        if not re.fullmatch(r'[a-f0-9]{32}', execution_id):
            raise ValueError('无效execution_id')
        self.root = root
        self.directory = root / execution_id
        if root.is_symlink() or self.directory.is_symlink():
            raise ValueError('产物目录不可为符号链接')
        self.execution_id = execution_id
        self.scope = scope.copy()
        self.latest = ''

    @classmethod
    def create(cls: type['RuntimeArtifacts'], shared_folder: str, scope: dict) -> 'RuntimeArtifacts':
        """Persist a run identity before any model or tool can execute."""
        value = cls(artifact_root(shared_folder), uuid.uuid4().hex, scope)
        value.directory.mkdir(parents=True, mode=0o700)
        value.update('running')
        _sync_directory(value.root)
        _sync_directory(value.root.parent)
        return value

    def _atomic(self, target: Path, raw: bytes) -> None:
        if self.root.is_symlink() or self.directory.is_symlink() or target.is_symlink():
            raise ValueError('产物路径不可为符号链接')
        fd, temporary = tempfile.mkstemp(prefix='.pending-', dir=self.directory)
        try:
            with os.fdopen(fd, 'wb') as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
            _sync_directory(self.directory)
        finally:
            Path(temporary).unlink(missing_ok=True)

    @guarded
    def update(self, status: str, **details: object) -> None:
        """Publish run status atomically, after the referenced immutable record."""
        old = self.inspect() if (self.directory / 'run.json').exists() else {}
        if old.get('read_only'):
            raise PermissionError('复制的执行产物只读，不可继续原执行')
        now = time.time()
        value = dict(old, created_at=old.get('created_at', now), updated_at=now)
        value.update(dict(
            execution_id=self.execution_id, scope=self.scope, status=status,
            latest_checkpoint=self.latest or old.get('latest_checkpoint', ''), **details))
        self._atomic(self.directory / 'run.json', _encoded(value))

    @guarded
    def save(self, kind: str, payload: dict) -> dict:
        """Retain all content blocks, arguments and errors, with a checksum."""
        if self.inspect().get('read_only'):
            raise PermissionError('复制的执行产物只读，不可追加执行记录')
        record = dict(kind=kind, execution_id=self.execution_id, scope=self.scope, payload=payload)
        raw = _encoded(record)
        digest = hashlib.sha256(raw).hexdigest()
        self._atomic(self.directory / f'{digest}.json', raw)
        return dict(execution_id=self.execution_id, artifact_id=digest, sha256=digest,
                    bytes=len(raw), kind=kind)

    @guarded
    def checkpoint(self, messages: list[dict], state: dict, **details: object) -> dict:
        """Persist full requirements/transcript plus explicit progress, never model reasoning."""
        reference = self.save('checkpoint', dict(messages=messages, state=state, **details))
        self.latest = reference['artifact_id']
        self.update('running', phase=details.get('phase', 'checkpoint'))
        return reference

    @guarded
    def read(self, artifact_id: str, offset: int = 0, limit: int = 4000,
             view: str = 'observation') -> dict:
        """Require the full execution scope, verify checksums, then paginate by characters."""
        self.inspect()
        if not re.fullmatch(r'[a-f0-9]{64}', artifact_id):
            raise ValueError('无效artifact_id')
        path = self.directory / f'{artifact_id}.json'
        if path.is_symlink():
            raise ValueError('产物文件不可为符号链接')
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != artifact_id:
            raise ValueError('产物校验失败')
        record = json.loads(raw)
        if record['scope'] != self.scope or record['execution_id'] != self.execution_id:
            raise PermissionError('产物不属于当前执行范围')
        if view not in ('observation', 'record'):
            raise ValueError('view只支持observation或record')
        text = (record['payload'].get('observation', '') if view == 'observation'
                else raw.decode('utf-8'))
        if offset < 0 or not 1 <= limit <= 16000:
            raise ValueError('offset必须非负，limit应为1..16000')
        end = offset + limit
        return dict(content=text[offset:end], offset=offset, content_chars=len(text),
                    next_offset=end if end < len(text) else None, sha256=artifact_id,
                    execution_id=self.execution_id, artifact_id=artifact_id, kind=record['kind'])

    @guarded
    def inspect(self) -> dict:
        """Report interrupted phases without claiming a completed tool was not executed."""
        path = self.directory / 'run.json'
        if self.root.is_symlink() or self.directory.is_symlink() or path.is_symlink():
            raise ValueError('产物路径不可为符号链接')
        run = json.loads(path.read_text(encoding='utf-8'))
        if run['scope'] != self.scope or run['execution_id'] != self.execution_id:
            raise PermissionError('产物不属于当前执行范围')
        return run
