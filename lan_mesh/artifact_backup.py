"""Immutable execution replication preserving scope and checksums without replaying tools."""

import json
import os
import re
import shutil
import tempfile
import time
import zipfile
from pathlib import Path

from .artifact_lock import artifact_guard
from .private_bundle import PrivateBundleStore, atomic_json, file_digest
from .runtime_artifacts import RuntimeArtifacts, _sync_directory


def _scope(manifest: dict) -> tuple[str, dict]:
    if manifest.get("kind") != "artifacts":
        raise ValueError("不是执行产物快照")
    identity = manifest["scope"]
    eid, scope = identity["execution_id"], identity["execution_scope"]
    if not isinstance(eid, str) or not re.fullmatch(r"[a-f0-9]{32}", eid):
        raise ValueError("执行标识无效")
    if (
        not isinstance(scope, dict)
        or set(scope) != {"agent_id", "task_id", "project_id", "attempt_id"}
        or any(not isinstance(value, str) or len(value) > 512 for value in scope.values())
    ):
        raise ValueError("执行作用域无效")
    return eid, scope


def _record(raw: bytes, name: str, eid: str, scope: dict) -> dict:
    value = json.loads(raw)
    if not isinstance(value, dict) or value.get("execution_id") != eid or value.get("scope") != scope:
        raise ValueError("产物身份或作用域不一致")
    if name != "run.json":
        import hashlib

        if hashlib.sha256(raw).hexdigest() != Path(name).stem:
            raise ValueError("产物内容地址不一致")
    return value


def export_artifacts(root: Path, store: PrivateBundleStore, execution_id: str, scope: dict) -> dict:
    runtime = RuntimeArtifacts(root, execution_id, scope)
    with artifact_guard(root, execution_id):
        runtime.inspect()
        files = {"run.json": runtime.directory / "run.json"}
        for path in runtime.directory.glob("*.json"):
            if path.name == "run.json":
                continue
            if not re.fullmatch(r"[a-f0-9]{64}\.json", path.name) or path.is_symlink():
                raise ValueError("执行目录含无效产物文件")
            _record(path.read_bytes(), path.name, execution_id, scope)
            files["records/" + path.name] = path
        result = store.create("artifacts", dict(execution_id=execution_id, execution_scope=scope), files)
    print(f"[AgentRuntime] 执行产物快照已创建: {execution_id}")
    return result


def restore_artifacts(root: Path, store: PrivateBundleStore, bundle_id: str) -> dict:
    manifest = store.validate(bundle_id)
    eid, scope = _scope(manifest)
    names = manifest["files"]
    if "run.json" not in names or any(
        name != "run.json" and not re.fullmatch(r"records/[a-f0-9]{64}\.json", name) for name in names
    ):
        raise ValueError("执行快照文件清单无效")
    runtime = RuntimeArtifacts(root, eid, scope)
    with artifact_guard(root, eid), zipfile.ZipFile(store.path(bundle_id)) as archive:
        original = _record(archive.read("run.json"), "run.json", eid, scope)
        for name in names:
            if name != "run.json":
                _record(archive.read(name), name, eid, scope)
        checkpoint = original.get("latest_checkpoint")
        if checkpoint and f"records/{checkpoint}.json" not in names:
            raise ValueError("快照缺少最新检查点")
        if runtime.directory.exists():
            return _existing(runtime, manifest, bundle_id)
        root.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".incoming-", dir=root))
        try:
            for name in names:
                if name != "run.json":
                    with archive.open(name) as src, (staging / Path(name).name).open("wb") as dst:
                        PrivateBundleStore._copy(src, dst)
                        dst.flush()
                        os.fsync(dst.fileno())
            restored = dict(
                original,
                read_only=True,
                replica_bundle_id=bundle_id,
                source_status=original.get("source_status", original["status"]),
                status="replica",
                pinned=True,
                restored_at=time.time(),
                updated_at=time.time(),
            )
            atomic_json(staging / "run.json", restored)
            os.rename(staging, runtime.directory)
            _sync_directory(root)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
    print(f"[AgentRuntime] 执行产物只读副本已恢复: {eid}")
    return dict(execution_id=eid, scope=scope, state="restored", read_only=True)


def _existing(runtime: RuntimeArtifacts, manifest: dict, bundle_id: str) -> dict:
    run = runtime.inspect()
    if run.get("replica_bundle_id") != bundle_id or not run.get("read_only"):
        raise ValueError("目标已有原执行或不同副本，拒绝覆盖")
    for name, meta in manifest["files"].items():
        if name == "run.json":
            continue
        path = runtime.directory / Path(name).name
        if path.is_symlink() or not path.is_file() or file_digest(path) != meta["sha256"]:
            raise ValueError("现有副本不完整或已变化，拒绝覆盖")
    return dict(
        execution_id=runtime.execution_id, scope=runtime.scope, state="restored", already_restored=True, read_only=True
    )
