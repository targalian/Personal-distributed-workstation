"""Explicit retention previews protect active tasks, uncertain tools and acceptance evidence."""

import hashlib
import json
import math
import os
import re
import shutil
import time
import uuid
from pathlib import Path

from .artifact_lock import artifact_guard
from .private_bundle import atomic_json, file_digest
from .runtime_artifacts import RuntimeArtifacts, _sync_directory

TERMINAL = {"completed", "failed", "cancelled"}


def _signature(directory: Path) -> tuple[str, int]:
    records, size = [], 0
    for path in sorted(directory.iterdir()):
        if path.is_symlink() or not path.is_file():
            raise ValueError("产物目录含非普通文件")
        stat = path.stat()
        size += stat.st_size
        records.append((path.name, stat.st_size, stat.st_mtime_ns, file_digest(path)))
    return hashlib.sha256(json.dumps(records).encode()).hexdigest(), size


def protection(owner: object, store: RuntimeArtifacts, run: dict) -> str:
    """A human review can confirm unknown task state; live local tasks stay protected."""
    if run.get("protected_evidence") or run.get("phase") == "acceptance_source":
        return "acceptance_evidence"
    if run.get("pinned"):
        return "pinned"
    if run.get("source_status", run["status"]) not in TERMINAL:
        return "execution_not_terminal"
    review = run.get("retention_review", {})
    reviewed = review.get("task_terminal_confirmed") is True and review.get("observed_updated_at") == run.get(
        "updated_at"
    )
    db = vars(owner).get("db") or vars(owner).get("_delivery_db")
    task = db.get_task(store.scope["task_id"]) if db and store.scope.get("task_id") else None
    if task:
        if task.status not in TERMINAL | {"accepted"}:
            return "task_not_terminal"
        evidence = (task.output_data or {}).get("_delivery", {}).get("acceptance_review", {})
        if store.execution_id in json.dumps(evidence):
            return "acceptance_evidence"
    elif store.scope.get("task_id") and not reviewed:
        return "task_state_unavailable"
    checkpoint = run.get("latest_checkpoint")
    if checkpoint:
        if not isinstance(checkpoint, str) or not re.fullmatch(r"[a-f0-9]{64}", checkpoint):
            return "checkpoint_unavailable"
        try:
            value = json.loads(store.read(checkpoint, limit=16000, view="record")["content"])
        except (ValueError, KeyError, OSError):
            # Large checkpoint records need a complete read, verified by content address.
            path = store.directory / (str(checkpoint) + ".json")
            if path.is_symlink() or not path.exists() or file_digest(path) != checkpoint:
                return "checkpoint_unavailable"
            value = json.loads(path.read_bytes())
        if value.get("scope") != store.scope or value.get("execution_id") != store.execution_id:
            return "checkpoint_unavailable"
        payload = value.get("payload", {})
        state = payload.get("state", {})
        if (state.get("pending_call") or state.get("review_required") or payload.get("pending_call")) and not reviewed:
            return "tool_outcome_unreviewed"
    return ""


def list_runs(owner: object, root: Path, task_id: str, project_id: str) -> dict:
    items = []
    if root.is_symlink():
        raise ValueError("产物目录不可为符号链接")
    for directory in sorted(root.glob("*")):
        if not re.fullmatch(r"[a-f0-9]{32}", directory.name) or directory.is_symlink():
            continue
        with artifact_guard(root, directory.name):
            try:
                path = directory / "run.json"
                if path.is_symlink():
                    continue
                run = json.loads(path.read_bytes())
                scope = run["scope"]
                if scope.get("task_id") != task_id or scope.get("project_id") != project_id:
                    continue
                store = RuntimeArtifacts(root, directory.name, scope)
                store.inspect()
                _, size = _signature(directory)
                reason = protection(owner, store, run)
                items.append(
                    dict(
                        run,
                        bytes=size,
                        retention_protection=reason,
                        updated_at=run.get("updated_at", path.stat().st_mtime),
                        artifacts=[
                            dict(artifact_id=p.stem, bytes=p.stat().st_size)
                            for p in sorted(directory.glob("*.json"))
                            if re.fullmatch(r"[a-f0-9]{64}\.json", p.name)
                        ],
                    )
                )
            except (OSError, ValueError, KeyError, TypeError):
                continue  # Unreadable identities are not cleanup candidates.
    return {"items": items}


def set_retention(root: Path, execution_id: str, scope: dict, payload: dict) -> dict:
    if type(payload.get("pinned")) is not bool:
        raise ValueError("pinned必须为布尔值")
    store = RuntimeArtifacts(root, execution_id, scope)
    with artifact_guard(root, execution_id):
        run = store.inspect()
        run.setdefault("updated_at", (store.directory / "run.json").stat().st_mtime)
        run["pinned"] = payload["pinned"]
        if payload.get("task_terminal_confirmed") is True:
            note = payload.get("note", "")
            if not isinstance(note, str) or not 1 <= len(note.strip()) <= 2000:
                raise ValueError("核对关联任务和工具结果后需填写结论")
            run["retention_review"] = dict(
                task_terminal_confirmed=True,
                note=note.strip(),
                reviewed_at=time.time(),
                observed_updated_at=run["updated_at"],
            )
        atomic_json(store.directory / "run.json", run)
        return run


def preview_cleanup(owner: object, root: Path, task_id: str, project_id: str, days: int) -> dict:
    if type(days) is not int or not 1 <= days <= 3650:
        raise ValueError("保留天数必须为1至3650的整数")
    cutoff = time.time() - days * 86400
    candidates, protected = [], []
    for run in list_runs(owner, root, task_id, project_id)["items"]:
        eid = run["execution_id"]
        with artifact_guard(root, eid):
            store = RuntimeArtifacts(root, eid, run["scope"])
            current = store.inspect()
            updated = current.get("updated_at", (store.directory / "run.json").stat().st_mtime)
            reason = protection(owner, store, current)
            if not isinstance(updated, (int, float)) or not math.isfinite(updated) or updated > cutoff:
                reason = reason or "retention_period"
            if reason:
                protected.append(dict(execution_id=eid, reason=reason))
                continue
            digest, size = _signature(store.directory)
            candidates.append(dict(execution_id=eid, scope=store.scope, signature=digest, bytes=size))
    token = uuid.uuid4().hex
    directory = root / ".retention"
    if directory.is_symlink():
        raise ValueError("清理计划路径不可为符号链接")
    directory.mkdir(parents=True, exist_ok=True)
    plan = dict(
        preview_id=token,
        task_id=task_id,
        project_id=project_id,
        created_at=time.time(),
        candidates=candidates,
        protected=protected,
        bytes=sum(i["bytes"] for i in candidates),
    )
    atomic_json(directory / (token + ".json"), plan)
    return plan


def execute_cleanup(owner: object, root: Path, preview_id: str, confirmed: bool) -> dict:
    if confirmed is not True or not re.fullmatch(r"[a-f0-9]{32}", preview_id):
        raise ValueError("需要有效预览和明确清理确认")
    path = root / ".retention" / (preview_id + ".json")
    if path.is_symlink() or path.parent.is_symlink() or root.is_symlink():
        raise ValueError("无效清理计划路径")
    plan = json.loads(path.read_bytes())
    if time.time() - plan["created_at"] > 3600:
        raise ValueError("清理预览已过期，请重新预览")
    trash = root / ".trash"
    if trash.is_symlink():
        raise ValueError("清理目录不可为符号链接")
    trash.mkdir(exist_ok=True)
    deleted, skipped = [], []
    for item in plan["candidates"]:
        eid = item["execution_id"]
        store = RuntimeArtifacts(root, eid, item["scope"])
        with artifact_guard(root, eid):
            target = trash / (eid + "-" + preview_id)
            if target.is_symlink():
                raise ValueError("清理目标不可为符号链接")
            if store.directory.exists():
                reason = protection(owner, store, store.inspect())
                if reason or _signature(store.directory)[0] != item["signature"]:
                    skipped.append(dict(execution_id=eid, reason=reason or "changed_since_preview"))
                    continue
                if target.exists():
                    raise ValueError("清理残留与活动目录冲突")
                os.rename(store.directory, target)
                _sync_directory(root)
                _sync_directory(trash)
            if target.exists():
                shutil.rmtree(target)
                _sync_directory(trash)
            deleted.append(eid)
    print(f"[AgentRuntime] 产物清理完成: deleted={len(deleted)} skipped={len(skipped)}")
    return dict(preview_id=preview_id, deleted=deleted, skipped=skipped)
