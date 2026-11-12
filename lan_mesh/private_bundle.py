"""Immutable private bundles with bounded validation and resumable authenticated transport."""

import base64
import hashlib
import json
import os
import re
import tempfile
import time
import zipfile
from pathlib import Path
from typing import BinaryIO, Iterator

from .runtime_artifacts import _sync_directory

MAX_BUNDLE_BYTES = 512 * 1024 * 1024
CHUNK_BYTES = 512 * 1024
MAX_FILES = 10000


def file_digest(path: Path) -> str:
    """Hash bytes without reading a whole archive or history into memory."""
    with path.open('rb') as handle:
        return hashlib.file_digest(handle, 'sha256').hexdigest()


def atomic_json(path: Path, value: dict | list) -> None:
    """Durably publish a small journal or metadata index."""
    if path.is_symlink():
        raise ValueError('元数据路径不可为符号链接')
    fd, temporary = tempfile.mkstemp(prefix='.pending-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(value, handle, ensure_ascii=False, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _sync_directory(path.parent)
    finally:
        Path(temporary).unlink(missing_ok=True)


def valid_name(name: str) -> bool:
    return bool(re.fullmatch(r'[A-Za-z0-9_.\-/]{1,240}', name)) and all(
        part not in ('', '.', '..') for part in name.split('/'))


class PrivateBundleStore:
    """Private archives are never added to shared downloads or cloud synchronization."""

    def __init__(self, folder: Path):
        self.root = folder.resolve() / '.private_replicas'
        if self.root.is_symlink():
            raise ValueError('私有备份目录不可为符号链接')
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)

    def path(self, bundle_id: str, suffix: str = '.zip') -> Path:
        if not re.fullmatch(r'[0-9a-f]{64}', bundle_id) or suffix not in ('.zip', '.part'):
            raise ValueError('无效备份标识')
        path = self.root / (bundle_id + suffix)
        if self.root.is_symlink() or path.is_symlink():
            raise ValueError('备份路径不可为符号链接')
        return path

    def create(self, kind: str, scope: dict, files: dict[str, Path]) -> dict:
        if len(files) > MAX_FILES or any(not valid_name(name) for name in files):
            raise ValueError('备份文件清单无效')
        fd, temp_name = tempfile.mkstemp(prefix='.building-', dir=self.root)
        os.close(fd)
        temporary = Path(temp_name)
        try:
            manifest = dict(version=1, kind=kind, scope=scope, created_at=time.time(), files={})
            marks = {name: path.stat() for name, path in files.items()}
            total = 0
            with zipfile.ZipFile(temporary, 'w', compression=zipfile.ZIP_STORED) as archive:
                for name, path in files.items():
                    if path.is_symlink():
                        raise ValueError('备份来源不可为符号链接')
                    before = path.stat()
                    total += before.st_size
                    if total > MAX_BUNDLE_BYTES - 4 * 1024 * 1024:
                        raise ValueError('单份备份超过512MiB，请拆分历史后重试')
                    digest = hashlib.sha256()
                    with path.open('rb') as src, archive.open(name, 'w', force_zip64=True) as dst:
                        self._copy(src, dst, digest)
                    after = path.stat()
                    if (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                            after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                        raise OSError('备份期间源文件发生变化，请重试')
                    manifest['files'][name] = dict(bytes=before.st_size, sha256=digest.hexdigest())
                archive.writestr('manifest.json', json.dumps(manifest, ensure_ascii=False))
            for name, path in files.items():
                before, after = marks[name], path.stat()
                if (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                        after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                    raise OSError('备份期间源文件发生变化，请重试')
            with temporary.open('rb') as handle:
                os.fsync(handle.fileno())
            if sum(p.stat().st_size for p in self.root.iterdir() if p.is_file()) > 2 * 1024**3:
                raise ValueError('私有备份目录达到2GiB配额，请清理不再需要的快照')
            bundle_id = file_digest(temporary)
            target = self.path(bundle_id)
            os.replace(temporary, target)
            _sync_directory(self.root)
            return dict(bundle_id=bundle_id, bytes=target.stat().st_size, manifest=manifest)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _copy(src: BinaryIO, dst: BinaryIO, digest: object = None) -> None:
        while block := src.read(CHUNK_BYTES):
            dst.write(block)
            if digest is not None:
                digest.update(block)

    def validate(self, bundle_id: str) -> dict:
        path = self.path(bundle_id)
        if path.stat().st_size > MAX_BUNDLE_BYTES or file_digest(path) != bundle_id:
            raise ValueError('备份校验失败或大小超过上限')
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            names = [item.filename for item in infos]
            if len(names) > MAX_FILES + 1 or len(set(names)) != len(names) or any(not valid_name(n) for n in names):
                raise ValueError('备份文件名、数量或重复条目无效')
            if sum(i.file_size for i in infos) > MAX_BUNDLE_BYTES:
                raise ValueError('备份展开大小超过上限')
            if archive.getinfo('manifest.json').file_size > 4 * 1024 * 1024:
                raise ValueError('备份清单过大')
            manifest = json.loads(archive.read('manifest.json'))
            if (not isinstance(manifest, dict) or manifest.get('version') != 1
                    or not isinstance(manifest.get('files'), dict) or not isinstance(manifest.get('scope'), dict)
                    or not isinstance(manifest.get('kind'), str)
                    or set(manifest['files']) != set(names) - {'manifest.json'}):
                raise ValueError('备份清单不完整')
            for name, meta in manifest['files'].items():
                if not isinstance(meta, dict) or type(meta.get('bytes')) is not int or not isinstance(meta.get('sha256'), str):
                    raise ValueError('备份文件清单格式无效')
                if archive.getinfo(name).file_size != meta['bytes']:
                    raise ValueError('备份文件长度不一致')
                with archive.open(name) as handle:
                    if hashlib.file_digest(handle, 'sha256').hexdigest() != meta['sha256']:
                        raise ValueError('备份文件内容校验失败')
        return manifest

    def status(self, bundle_id: str) -> dict:
        done, part = self.path(bundle_id), self.path(bundle_id, '.part')
        if not done.exists() and part.exists() and file_digest(part) == bundle_id:
            os.replace(part, done)
            _sync_directory(self.root)
        return dict(bundle_id=bundle_id, complete=done.exists(),
                    offset=done.stat().st_size if done.exists() else part.stat().st_size if part.exists() else 0)

    def receive(self, bundle_id: str, payload: dict) -> dict:
        """A replayed identical chunk is acknowledged; conflicting bytes are rejected."""
        offset, total = payload.get('offset'), payload.get('total')
        if type(offset) is not int or type(total) is not int or not 0 <= offset < total <= MAX_BUNDLE_BYTES:
            raise ValueError('分片位置或总长度无效')
        encoded = payload.get('data', '')
        if not isinstance(encoded, str) or len(encoded) > CHUNK_BYTES * 4 // 3 + 8:
            raise ValueError('分片过大')
        data = base64.b64decode(encoded, validate=True)
        if not data or len(data) > CHUNK_BYTES or offset + len(data) > total:
            raise ValueError('分片长度无效')
        if hashlib.sha256(data).hexdigest() != payload.get('sha256'):
            raise ValueError('分片校验失败')
        done, part = self.path(bundle_id), self.path(bundle_id, '.part')
        path = done if done.exists() else part
        size = path.stat().st_size if path.exists() else 0
        if offset < size:
            with path.open('rb') as handle:
                handle.seek(offset)
                if handle.read(len(data)) != data:
                    raise ValueError('重复分片内容冲突')
        elif offset == size and not done.exists():
            if sum(p.stat().st_size for p in self.root.iterdir() if p.is_file()) + len(data) > 2 * 1024**3:
                raise ValueError('私有备份目录达到2GiB配额')
            with part.open('ab') as handle:
                handle.write(data); handle.flush(); os.fsync(handle.fileno())
        else:
            raise ValueError('分片位置不连续，请查询已接收位置')
        if not done.exists() and part.stat().st_size == total:
            if file_digest(part) != bundle_id:
                raise ValueError('完整备份校验失败')
            os.replace(part, done)
            _sync_directory(self.root)
        return self.status(bundle_id)

    def json_rows(self, bundle_id: str, name: str) -> Iterator[dict]:
        with zipfile.ZipFile(self.path(bundle_id)) as archive, archive.open(name) as handle:
            while line := handle.readline(256 * 1024 + 1):
                if len(line) > 256 * 1024:
                    raise ValueError('记忆修订行过大')
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ValueError('记忆修订格式无效')
                yield value
