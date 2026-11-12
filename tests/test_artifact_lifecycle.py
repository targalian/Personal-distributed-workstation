"""Retention safety, scope-preserving replicas and real process crash durability."""

import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lan_mesh.acceptance_evidence import AcceptanceEvidenceService
from lan_mesh.artifact_backup import export_artifacts, restore_artifacts
from lan_mesh.artifact_management_routes import build_artifact_management_routes
from lan_mesh.artifact_management_service import ArtifactManagementService
from lan_mesh.artifact_retention import execute_cleanup, list_runs, preview_cleanup, set_retention
from lan_mesh.component_service import call_component
from lan_mesh.database import Database
from lan_mesh.private_backup_routes import build_private_backup_routes
from lan_mesh.private_backup_service import PrivateBackupService
from lan_mesh.private_bundle import PrivateBundleStore, atomic_json
from lan_mesh.protocol import Task
from lan_mesh.runtime_artifacts import RuntimeArtifacts, artifact_root
from lan_mesh.shared_folder import SharedFolderManager


@pytest.fixture
def owner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> object:
    monkeypatch.setattr(Database, "backup", lambda *a, **k: "")
    db = Database(str(tmp_path / "station.db"))
    db.save_task(Task(task_id="task", project_id="project", status="completed"))
    value = SimpleNamespace(
        db=db, state=SimpleNamespace(shared_folder=SharedFolderManager(str(tmp_path)), device_id="station")
    )
    yield value
    if vars(value).get("services"):
        value.services.close()


def _run(folder: Path, status: str = "completed", task: str = "task", pending: bool = False) -> RuntimeArtifacts:
    store = RuntimeArtifacts.create(
        str(folder), dict(task_id=task, project_id="project", agent_id="agent", attempt_id="attempt")
    )
    store.save("tool_result", dict(observation="完整中文观察" * 2000))
    store.checkpoint([{"role": "user", "content": "原始要求"}], {"pending_call": {"id": "c"}} if pending else {})
    store.update(status)
    value = store.inspect()
    value["updated_at"] = time.time() - 30 * 86400
    atomic_json(store.directory / "run.json", value)
    return store


def test_cleanup_preview_protects_live_pinned_uncertain_and_evidence(owner: object, tmp_path: Path) -> None:
    eligible = _run(tmp_path)
    live = _run(tmp_path, "running")
    pending = _run(tmp_path, pending=True)
    evidence = _run(tmp_path)
    evidence.update("completed", protected_evidence=True)
    pinned = _run(tmp_path)
    set_retention(pinned.root, pinned.execution_id, pinned.scope, {"pinned": True})
    preview = preview_cleanup(owner, eligible.root, "task", "project", 7)
    assert [r["execution_id"] for r in preview["candidates"]] == [eligible.execution_id]
    reasons = {r["execution_id"]: r["reason"] for r in preview["protected"]}
    assert reasons[live.execution_id] == "execution_not_terminal"
    assert reasons[pending.execution_id] == "tool_outcome_unreviewed"
    assert reasons[evidence.execution_id] == "acceptance_evidence"
    assert reasons[pinned.execution_id] == "pinned"
    assert eligible.directory.exists()
    with pytest.raises(ValueError):
        execute_cleanup(owner, eligible.root, preview["preview_id"], False)
    result = execute_cleanup(owner, eligible.root, preview["preview_id"], True)
    assert result["deleted"] == [eligible.execution_id] and not eligible.directory.exists()
    assert all(r.directory.exists() for r in [live, pending, evidence, pinned])


def test_cleanup_rechecks_pin_and_live_task_after_preview(owner: object, tmp_path: Path) -> None:
    first = _run(tmp_path)
    plan = preview_cleanup(owner, first.root, "task", "project", 7)
    set_retention(first.root, first.execution_id, first.scope, {"pinned": True})
    assert execute_cleanup(owner, first.root, plan["preview_id"], True)["skipped"][0]["reason"] == "pinned"
    set_retention(first.root, first.execution_id, first.scope, {"pinned": False})
    plan = preview_cleanup(owner, first.root, "task", "project", 7)
    owner.db.update_task_status("task", "running")
    assert execute_cleanup(owner, first.root, plan["preview_id"], True)["skipped"][0]["reason"] == "task_not_terminal"
    assert first.directory.exists()


def test_unknown_task_requires_explicit_review_and_new_writes_invalidate_it(tmp_path: Path) -> None:
    owner = SimpleNamespace()
    run = _run(tmp_path)
    assert list_runs(owner, run.root, "task", "project")["items"][0]["retention_protection"] == "task_state_unavailable"
    with pytest.raises(ValueError):
        set_retention(run.root, run.execution_id, run.scope, {"pinned": False, "task_terminal_confirmed": True})
    set_retention(
        run.root,
        run.execution_id,
        run.scope,
        {"pinned": False, "task_terminal_confirmed": True, "note": "已核对主节点任务完成和工具产物"},
    )
    assert preview_cleanup(owner, run.root, "task", "project", 7)["candidates"]
    run.update("completed")
    assert list_runs(owner, run.root, "task", "project")["items"][0]["retention_protection"] == "task_state_unavailable"


def test_changed_artifact_and_wrong_scope_cannot_be_cleaned(owner: object, tmp_path: Path) -> None:
    run = _run(tmp_path)
    plan = preview_cleanup(owner, run.root, "task", "project", 7)
    run.save("extra", {"observation": "清理预览之后新增"})
    assert execute_cleanup(owner, run.root, plan["preview_id"], True)["skipped"][0]["reason"] == "changed_since_preview"
    assert not list_runs(owner, run.root, "task", "other-project")["items"]
    with pytest.raises(PermissionError):
        set_retention(run.root, run.execution_id, {**run.scope, "project_id": "other"}, {"pinned": False})
    assert run.directory.exists()


def test_acceptance_sources_and_bound_tool_records_are_protected(owner: object, tmp_path: Path) -> None:
    run = _run(tmp_path)
    record = run.save("tool_result", dict(observation="验收观察"))
    actor = SimpleNamespace(shared_folder=str(tmp_path), agent_id="reviewer")
    ref = dict(id=record["artifact_id"], execution_id=run.execution_id, scope=run.scope)
    try:
        call_component(
            actor,
            AcceptanceEvidenceService,
            "bind",
            task={"task_id": "task", "project_id": "project"},
            criteria=["必须有来源"],
            deliverable="报告",
            review={"checks": [{"met": True, "evidence_ids": [ref["id"]]}]},
            catalog=[ref],
        )
        assert run.inspect()["protected_evidence"]
        assert all(
            item["retention_protection"] == "acceptance_evidence"
            for item in list_runs(owner, run.root, "task", "project")["items"]
        )
    finally:
        actor.services.close()


def test_artifact_replica_preserves_identity_and_rejects_overwrite(tmp_path: Path) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    run = _run(source)
    bundle_store = PrivateBundleStore(tmp_path / "bundles")
    exported = export_artifacts(run.root, bundle_store, run.execution_id, run.scope)
    bid = exported["bundle_id"]
    root = artifact_root(str(target))
    result = restore_artifacts(root, bundle_store, bid)
    replica = RuntimeArtifacts(root, run.execution_id, run.scope)
    assert result["read_only"] and replica.inspect()["source_status"] == "completed"
    checkpoint = replica.inspect()["latest_checkpoint"]
    assert replica.read(checkpoint, view="record")["content"] == run.read(checkpoint, view="record")["content"]
    with pytest.raises(PermissionError):
        replica.save("tool_result", dict(observation="不可继续旧工具"))
    with pytest.raises(PermissionError):
        replica.update("running")
    assert restore_artifacts(root, bundle_store, bid)["already_restored"]
    with pytest.raises(ValueError, match="拒绝覆盖"):
        restore_artifacts(run.root, bundle_store, bid)
    with pytest.raises(PermissionError):
        RuntimeArtifacts(root, run.execution_id, {**run.scope, "attempt_id": "other"}).inspect()


@pytest.mark.parametrize("phase", ["before_record", "after_record", "after_manifest"])
def test_sigkill_during_atomic_checkpoint_keeps_last_published_state(tmp_path: Path, phase: str) -> None:
    run = _run(tmp_path, "running")
    old = run.inspect()["latest_checkpoint"]
    code = f"""
import os,signal
from pathlib import Path
from lan_mesh.runtime_artifacts import RuntimeArtifacts
run=RuntimeArtifacts(Path({str(run.root)!r}),{run.execution_id!r},{run.scope!r})
replace=os.replace
phase={phase!r}
def crash():
 if hasattr(signal,'SIGKILL'):os.kill(os.getpid(),signal.SIGKILL)
 os._exit(77)
def intercepted(src,dst):
 record=Path(dst).name!='run.json'
 if phase=='before_record' and record:crash()
 replace(src,dst)
 if (phase=='after_record' and record) or (phase=='after_manifest' and not record):crash()
os.replace=intercepted
run.checkpoint([{{'role':'user','content':'崩溃前的新要求'}}],{{'pending_call':{{'id':'uncertain'}}}},phase='tool_started')
"""
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=20)
    assert result.returncode in (-getattr(signal, "SIGKILL", 9), 77), result.stderr
    restored = RuntimeArtifacts(run.root, run.execution_id, run.scope)
    latest = restored.inspect()["latest_checkpoint"]
    assert (latest != old) == (phase == "after_manifest")
    record = json.loads(restored.read(latest, view="record")["content"])
    assert record["kind"] == "checkpoint"
    assert restored.inspect()["status"] == "running"  # Never mark uncertain execution complete.
    for path in run.directory.glob("*.json"):
        if path.name != "run.json":
            restored.read(path.stem, view="record")


def test_cleanup_restarts_after_process_exit_between_rename_and_delete(owner: object, tmp_path: Path) -> None:
    run = _run(tmp_path)
    preview = preview_cleanup(owner, run.root, "task", "project", 7)
    code = f"""
import os
from pathlib import Path
from types import SimpleNamespace
from lan_mesh.database import Database
import lan_mesh.artifact_retention as module
Database.backup=lambda *a,**k:''
owner=SimpleNamespace(db=Database({str(owner.db.path)!r}))
module.shutil.rmtree=lambda *a,**k:os._exit(77)
module.execute_cleanup(owner,Path({str(run.root)!r}),{preview["preview_id"]!r},True)
"""
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=20)
    assert result.returncode == 77, result.stderr
    assert not run.directory.exists() and list((run.root / ".trash").iterdir())
    assert execute_cleanup(owner, run.root, preview["preview_id"], True)["deleted"] == [run.execution_id]
    assert not list((run.root / ".trash").iterdir())


def test_worker_replication_to_configured_secretary_and_api_restore(owner: object, tmp_path: Path) -> None:
    app = FastAPI()
    app.include_router(build_private_backup_routes(owner))
    app.include_router(build_artifact_management_routes(owner))
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
    thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.started
    worker_folder = tmp_path / "worker"
    run = _run(worker_folder)
    worker = SimpleNamespace(
        state=SimpleNamespace(
            shared_folder=SharedFolderManager(str(worker_folder)),
            device_id="worker",
            secretary_ip="127.0.0.1",
            secretary_port=port,
        )
    )
    try:
        exported = call_component(
            worker, ArtifactManagementService, "export", execution_id=run.execution_id, payload={"scope": run.scope}
        )
        bid = exported["bundle_id"]
        assert (
            call_component(worker, PrivateBackupService, "push", bundle_id=bid, payload={"peer_id": "secretary"})[
                "state"
            ]
            == "replicated"
        )
        with TestClient(app) as client:
            route = f"/api/runtime/artifact-replicas/{bid}/restore"
            assert client.post(route, json={}).status_code == 422
            assert client.post(route, json={"confirmed": True}).json()["read_only"]
            result = client.get("/api/runtime/artifact-runs?task_id=task&project_id=project").json()
            assert result["items"][0]["execution_id"] == run.execution_id
            assert result["items"][0]["retention_protection"] == "pinned"
    finally:
        server.should_exit = True
        thread.join(5)
        worker.services.close()


def test_legacy_delivery_reference_protects_unpinned_evidence(owner: object, tmp_path: Path) -> None:
    run = _run(tmp_path)
    task = owner.db.get_task("task")
    task.output_data = {
        "_delivery": {
            "acceptance_review": {"checks": [{"source_refs": [{"execution_id": run.execution_id, "scope": run.scope}]}]}
        }
    }
    owner.db.save_task(task)
    plan = preview_cleanup(owner, run.root, "task", "project", 7)
    assert not plan["candidates"] and plan["protected"][0]["reason"] == "acceptance_evidence"


def test_artifact_management_write_roles_and_scope(
    owner: object, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lan_mesh import station_routes_common as common

    run = _run(tmp_path)
    monkeypatch.setattr(common, "_mesh_auth_enabled", True)
    monkeypatch.setattr(common, "_mesh_auth_token", "mesh")
    monkeypatch.setattr(common, "_API_KEY", "")
    monkeypatch.setattr(common, "_users", {"configured": {}})
    monkeypatch.setattr(common, "_user_tokens", {"configured": {}})
    monkeypatch.setattr(common, "_rate_limiter", SimpleNamespace(is_allowed=lambda *a, **k: True))
    monkeypatch.setattr(
        common, "resolve_role", lambda token: {"role": token} if token in {"boss", "operator", "viewer"} else None
    )
    app = FastAPI()
    app.middleware("http")(common.api_guard_middleware)
    app.include_router(build_artifact_management_routes(owner))
    with TestClient(app) as client:
        endpoint = "/api/runtime/artifact-cleanup/preview"
        payload = {"task_id": "task", "project_id": "project", "older_than_days": 7}
        assert client.post(endpoint, json=payload).status_code == 401
        for role in ["operator", "viewer"]:
            assert client.post(endpoint, json=payload, headers={"Authorization": "Bearer " + role}).status_code == 403
        client.headers["Authorization"] = "Bearer boss"
        assert client.get("/api/runtime/artifact-runs?task_id=task&project_id=wrong").json()["items"] == []
        plan = client.post(endpoint, json=payload).json()
        assert len(plan["candidates"]) == 1
        response = client.post(
            "/api/runtime/artifact-cleanup/execute", json={"preview_id": plan["preview_id"], "confirmed": True}
        )
        assert response.json()["deleted"] == [run.execution_id]


def test_malformed_replica_record_is_rejected_before_publication(tmp_path: Path) -> None:
    import hashlib

    source = tmp_path / "source"
    source.mkdir()
    eid = "a" * 32
    scope = dict(agent_id="a", task_id="t", project_id="p", attempt_id="at")
    run_file = source / "run.json"
    run_file.write_text(json.dumps(dict(execution_id=eid, scope=scope, status="completed")))
    raw = b"[]"
    digest = hashlib.sha256(raw).hexdigest()
    record = source / (digest + ".json")
    record.write_bytes(raw)
    bundles = PrivateBundleStore(tmp_path / "bundles")
    bid = bundles.create(
        "artifacts",
        dict(execution_id=eid, execution_scope=scope),
        {"run.json": run_file, "records/" + record.name: record},
    )["bundle_id"]
    root = artifact_root(str(tmp_path / "target"))
    with pytest.raises(ValueError, match="身份或作用域"):
        restore_artifacts(root, bundles, bid)
    assert not (root / eid).exists()
