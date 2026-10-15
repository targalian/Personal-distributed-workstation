"""Restore real task, brief and receipt stores; reject corrupt or unsafe backup archives."""
import json
import os
import sqlite3
import stat
import subprocess
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from lan_mesh.backup import create_backup, digest, restore_backup, verify_backup
from lan_mesh.chat_handler import ChatHandler
from lan_mesh.database import Database
from lan_mesh.execution_receipts import ExecutionReceipts
from lan_mesh.pm_checkpoint import PMCheckpoints, read_checkpoint
from lan_mesh.pm_start import start_pm_once
from lan_mesh.pm_state import PMState
from lan_mesh.protocol import Task
from lan_mesh.task_resubmission import prepare_resubmission
from tests.test_project_brief import document


@pytest.fixture
def dataset(tmp_path, monkeypatch):
    monkeypatch.setattr(Database, "backup", lambda self: "")
    state, shared, data = (tmp_path / name for name in ("state", "shared", "db"))
    for path in (state, shared, data):
        path.mkdir()
    db = Database(str(data / "main.sqlite3"))
    controller = SimpleNamespace(db=db, submit_task_from_chat=Mock())
    chat = ChatHandler(Mock(), controller, db=db, shared_folder=SimpleNamespace(path=shared))
    cid = chat._active_conv_id
    chat.briefs.save(cid, document(), 0)
    chat.briefs.save(cid, {**document(), "title": "保存的版本"}, 1)
    chat.briefs.propose(cid, {**document(), "title": "待审阅建议"}, 2)
    def submit(**kw):
        task = Task(task_id=kw["task_id"], name=kw["name"], input_data=kw["input_data"], pm_agent_id="pm-backup", status="running")
        db.save_task(task)
        return task.to_dict()
    controller.submit_task_from_chat.side_effect = submit
    handoff = chat.brief_handoffs.dispatch(cid, 2)
    db.record_task_delivery(handoff["task_id"], "pm-backup", {"deliverable": "交付产物", "summary": "摘要"})
    db.review_task_delivery(handoff["task_id"], True)
    payload = {"subtask_id": "done", "name": "A", "input_data": {}}
    receipts = ExecutionReceipts(data / "main-executions.sqlite3", owner="old-process")
    receipts.claim(payload)
    receipts.finish("done", {"status": "completed", "output": {"summary": "原输出"}})
    receipts.claim({**payload, "subtask_id": "pending"})
    pm_payload = {"subtask_id": "pm-restore", "pm_id": "pm-restore", "parent_task_id": "t", "name": "PM startup",
                  "input_data": {"task": {"task_id": "t", "pm_agent_id": "pm-restore", "status": "running"},
                                 "secretary_url": "http://secretary"}}
    starts = ExecutionReceipts(data / "main-pm-starts.sqlite3", owner="old-process")
    starts.claim(pm_payload)
    starts.finish("pm-restore", {"status": "completed", "output": {"ok": True, "process_owner": "old-process"}})
    for name in ("worker-executions.sqlite3", "worker-pm-starts.sqlite3"):
        store = ExecutionReceipts(state / name, owner="old-process")
        store.claim(payload)
    (state / ".env").write_text("BACKUP_TEST_VALUE=fixture-only\n")
    (state / "station.lock").write_text("stale lock")
    (state / "backups").mkdir()
    (state / "backups" / "old.txt").write_text("excluded")
    (shared / "deliverables").mkdir()
    (shared / "deliverables" / "项目描述.md").write_text(chat.briefs.get(cid)["markdown"], encoding="utf-8")
    return SimpleNamespace(roots={"state": state, "shared": shared, "database_dir": data},
                           archive=tmp_path / "backup.zip", db=db, cid=cid, handoff=handoff, payload=payload,
                           pm_payload=pm_payload)


def test_restore_preserves_pm_execution_snapshot_in_startup_database(dataset, tmp_path):
    path = dataset.roots["database_dir"] / "main-pm-starts.sqlite3"
    st = PMState()
    st.task = {"task_id": "t"}
    st.plan = {"decomposition": [{"name": "A"}, {"name": "B"}]}
    st.subtask_outputs = {"A": "saved output"}
    st.execution_attempts = {"B": {"subtask_id": "execution-B", "phase": "active"}}
    snapshot = st.snapshot()
    PMCheckpoints(path, owner="old-process").save("pm-restore", "t", "d", snapshot, 0)
    create_backup(dataset.roots, dataset.archive, offline_confirmed=True)
    destination = tmp_path / "restored"
    restore_backup(dataset.archive, destination)
    restored = destination / "database_dir/main-pm-starts.sqlite3"
    assert read_checkpoint(restored, "pm-restore", "t", "d")["snapshot"] == snapshot
    with pytest.raises(ValueError):
        PMCheckpoints(restored, owner="new-process").save("pm-restore", "t", "d", snapshot, 1)


def test_restore_preserves_resubmission_tombstone_and_frozen_source(dataset, tmp_path):
    source = Task(task_id="retry-source", status="failed", input_data={"_brief": {"revision": 2}})
    dataset.db.save_task(source)
    replacement, _ = prepare_resubmission(SimpleNamespace(db=dataset.db), source.task_id)
    dataset.db.delete_task(replacement.task_id)
    create_backup(dataset.roots, dataset.archive, offline_confirmed=True)
    destination = tmp_path / "restored"
    restore_backup(dataset.archive, destination)
    db = Database(str(destination / "database_dir/main.sqlite3"))
    with pytest.raises(ValueError, match="已删除"):
        prepare_resubmission(SimpleNamespace(db=db), source.task_id)
    record = db._get_conn().execute("SELECT * FROM task_resubmissions").fetchone()
    assert record["replacement_task_id"] == replacement.task_id
    assert json.loads(record["source_snapshot"])["input_data"] == source.input_data


def test_roundtrip_preserves_reviews_history_handoffs_and_execution_dedup(dataset, tmp_path, monkeypatch):
    manifest = create_backup(dataset.roots, dataset.archive, offline_confirmed=True)
    assert not any("station.lock" in item["path"] or "backups/" in item["path"] for item in manifest["files"])
    destination = tmp_path / "restored"
    restore_backup(dataset.archive, destination)
    db = Database(str(destination / "database_dir/main.sqlite3"))
    controller = SimpleNamespace(db=db, submit_task_from_chat=Mock())
    chat = ChatHandler(Mock(), controller, db=db, shared_folder=SimpleNamespace(path=destination / "shared"))
    brief = chat.briefs.get(dataset.cid)
    assert brief["revision"] == 2 and brief["document"]["title"] == "保存的版本"
    assert brief["proposal"] is not None
    assert len(chat.briefs.history(dataset.cid)["revisions"]) == 2
    assert chat.briefs.revision(dataset.cid, 1)["document"]["title"] == document()["title"]
    replay = chat.brief_handoffs.dispatch(dataset.cid, 2)
    assert replay["task_id"] == dataset.handoff["task_id"]
    assert replay["task_status"] == "accepted"
    controller.submit_task_from_chat.assert_not_called()
    assert db.get_task(replay["task_id"]).output_data["_delivery"]["accepted"] is True
    receipts = ExecutionReceipts(destination / "database_dir/main-executions.sqlite3")
    claimed, receipt = receipts.claim(dataset.payload)
    assert not claimed and receipt["output"] == {"summary": "原输出"}
    assert receipts.get("pending")["status"] == "unknown"
    factory = Mock()
    monkeypatch.setattr("lan_mesh.pm_start.ProjectManagerAgent", factory)
    result = start_pm_once(pm_id="pm-restore", task_id="t", secretary_url="http://secretary",
                           task_data=dataset.pm_payload["input_data"]["task"], runtime=Mock(), device_id="d", device_name="d",
                           receipt_path=destination / "database_dir/main-pm-starts.sqlite3",
                           current=lambda: None, assign=Mock(), lookup=Mock())
    assert result["uncertain"] and not result["ok"]
    factory.assert_not_called()
    assert (destination / "state/.env").read_text() == "BACKUP_TEST_VALUE=fixture-only\n"
    assert not (destination / "RESTORE_INCOMPLETE").exists()
    assert (destination / "恢复说明.md").exists()
    assert stat.S_IMODE(dataset.archive.stat().st_mode) == 0o600
    assert stat.S_IMODE((destination / "state/.env").stat().st_mode) == 0o600


@pytest.mark.parametrize("file_root", [False, True])
def test_wal_database_snapshot_contains_committed_pages(tmp_path, file_root):
    source = tmp_path / "source"
    source.mkdir()
    path = source / "data.sqlite3"
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE sample (value TEXT)")
        conn.execute("INSERT INTO sample VALUES ('committed-in-wal')")
        conn.commit()
        archive = tmp_path / "wal.zip"
        manifest = create_backup({"state": path if file_root else source}, archive, offline_confirmed=True)
        assert [item["path"] for item in manifest["files"]] == ["state/data.sqlite3"]
        restore_backup(archive, tmp_path / "restore")
        with sqlite3.connect(tmp_path / "restore/state/data.sqlite3") as restored:
            assert restored.execute("SELECT value FROM sample").fetchone()[0] == "committed-in-wal"
    finally:
        conn.close()


def test_explicit_database_root_detects_wal_changes(tmp_path, monkeypatch):
    import lan_mesh.backup as backup
    path = tmp_path / "active.sqlite3"
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE sample (value TEXT)")
        connection.commit()
        actual_check = backup._database_check
        def change_source_after_snapshot(snapshot):
            actual_check(snapshot)
            connection.execute("INSERT INTO sample VALUES ('concurrent')")
            connection.commit()
        monkeypatch.setattr(backup, "_database_check", change_source_after_snapshot)
        archive = tmp_path / "changed.zip"
        with pytest.raises(ValueError, match="变化"):
            create_backup({"database": path}, archive, offline_confirmed=True)
        assert not archive.exists()
    finally:
        connection.close()


def rewrite_archive(source, target, transform):
    with zipfile.ZipFile(source) as original, zipfile.ZipFile(target, "w") as changed:
        for info in original.infolist():
            transform(changed, info, original.read(info.filename))


@pytest.mark.parametrize("mode", ["changed", "missing", "extra", "duplicate", "traversal", "symlink", "bad_sqlite"])
def test_corrupt_or_unsafe_archive_never_creates_restore_destination(dataset, tmp_path, mode):
    create_backup(dataset.roots, dataset.archive, offline_confirmed=True)
    bad = tmp_path / "bad.zip"
    def transform(bundle, info, data):
        selected = info.filename == "state/.env"
        if mode == "missing" and selected:
            return
        if mode == "changed" and selected:
            data += b"changed"
        if mode == "bad_sqlite" and info.filename == "database_dir/main.sqlite3":
            data = b"not sqlite"
        if mode == "bad_sqlite" and info.filename == "manifest.json":
            import hashlib
            manifest = json.loads(data)
            item = next(item for item in manifest["files"] if item["path"] == "database_dir/main.sqlite3")
            item.update(size=len(b"not sqlite"), sha256=hashlib.sha256(b"not sqlite").hexdigest())
            data = json.dumps(manifest).encode()
        if mode == "symlink" and selected:
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
        bundle.writestr(info, data)
        if selected and mode in ("extra", "duplicate", "traversal"):
            bundle.writestr({"extra": "extra.txt", "duplicate": "state/.env", "traversal": "../outside"}[mode], b"bad")
    rewrite_archive(dataset.archive, bad, transform)
    destination = tmp_path / "failed-restore"
    with pytest.raises((ValueError, sqlite3.DatabaseError)):
        restore_backup(bad, destination)
    assert not destination.exists() and not (tmp_path / "outside").exists()


def test_restore_refuses_existing_data_and_preserves_originals(dataset, tmp_path):
    create_backup(dataset.roots, dataset.archive, offline_confirmed=True)
    original_hash = digest(dataset.archive)
    with pytest.raises(FileExistsError):
        create_backup(dataset.roots, dataset.archive, offline_confirmed=True)
    assert digest(dataset.archive) == original_hash
    dest = tmp_path / "existing"
    dest.mkdir()
    (dest / "keep").write_text("original")
    with pytest.raises(FileExistsError):
        restore_backup(dataset.archive, dest)
    assert (dest / "keep").read_text() == "original"


def test_source_mutation_aborts_without_publishing_archive(dataset, monkeypatch):
    from lan_mesh import backup
    actual = backup.shutil.copyfile
    mutated = False
    def copy(source, target):
        nonlocal mutated
        result = actual(source, target)
        if not mutated:
            mutated = True
            (dataset.roots["shared"] / "new-during-backup.txt").write_text("concurrent writer")
        return result
    monkeypatch.setattr(backup.shutil, "copyfile", copy)
    with pytest.raises(ValueError, match="变化"):
        create_backup(dataset.roots, dataset.archive, offline_confirmed=True)
    assert not dataset.archive.exists()


def test_source_link_is_rejected(dataset, tmp_path):
    (dataset.roots["shared"] / "link").symlink_to(tmp_path)
    with pytest.raises(ValueError, match="符号链接"):
        create_backup(dataset.roots, dataset.archive, offline_confirmed=True)


def test_create_requires_offline_confirmation_and_rejects_live_station(dataset, monkeypatch):
    with pytest.raises(ValueError, match="offline-confirmed"):
        create_backup(dataset.roots, dataset.archive)
    monkeypatch.setattr("lan_mesh.backup.psutil.process_iter", lambda *args: [
        SimpleNamespace(pid=1234, info={"cmdline": ["python", "main.py", "station"]})])
    with pytest.raises(ValueError, match="仍在运行"):
        create_backup(dataset.roots, dataset.archive, offline_confirmed=True)
    assert not dataset.archive.exists()


def test_archive_inside_source_is_rejected(dataset):
    with pytest.raises(ValueError, match="源内部"):
        create_backup(dataset.roots, dataset.roots["shared"] / "bundle.zip", offline_confirmed=True)


def test_missing_committed_brief_revision_cannot_be_verified(dataset):
    folder = dataset.roots["shared"] / "conversations/project_briefs" / dataset.cid
    raw = json.loads((folder / "brief.json").read_text())
    (folder / "revisions" / raw["_history"][0]["file"]).unlink()
    with pytest.raises(ValueError, match="缺少已提交历史版本"):
        create_backup(dataset.roots, dataset.archive, offline_confirmed=True)
    assert not dataset.archive.exists()


def test_empty_directories_and_executable_artifacts_survive(dataset, tmp_path):
    empty = dataset.roots["shared"] / "empty/nested"
    empty.mkdir(parents=True)
    script = dataset.roots["shared"] / "deliverables/run.sh"
    script.write_text("#!/bin/sh\nexit 0\n")
    script.chmod(0o755)
    create_backup(dataset.roots, dataset.archive, offline_confirmed=True)
    restored = tmp_path / "restored"
    restore_backup(dataset.archive, restored)
    assert (restored / "shared/empty/nested").is_dir()
    assert stat.S_IMODE((restored / "shared/deliverables/run.sh").stat().st_mode) == 0o700


def test_missing_source_and_corrupt_database_abort(dataset):
    with pytest.raises(ValueError, match="不存在"):
        create_backup({"missing": dataset.archive.parent / "missing"}, dataset.archive, offline_confirmed=True)
    (dataset.roots["state"] / "broken.sqlite3").write_bytes(b"corrupt database")
    with pytest.raises(sqlite3.DatabaseError):
        create_backup(dataset.roots, dataset.archive, offline_confirmed=True)
    assert not dataset.archive.exists()


def test_restore_rejects_symlink_destination(dataset, tmp_path):
    create_backup(dataset.roots, dataset.archive, offline_confirmed=True)
    actual = tmp_path / "actual"
    actual.mkdir()
    link = tmp_path / "link"
    link.symlink_to(actual, target_is_directory=True)
    with pytest.raises(ValueError, match="符号链接"):
        restore_backup(dataset.archive, link / "restore")
    assert not list(actual.iterdir())


def test_failed_restore_retains_incomplete_marker(dataset, tmp_path, monkeypatch):
    from lan_mesh import backup
    create_backup(dataset.roots, dataset.archive, offline_confirmed=True)
    actual = backup.shutil.copyfileobj
    def fail(source, target, *args, **kwargs):
        if "restore-target" in str(getattr(target, "name", "")):
            raise OSError("disk full")
        return actual(source, target, *args, **kwargs)
    monkeypatch.setattr(backup.shutil, "copyfileobj", fail)
    destination = tmp_path / "restore-target"
    with pytest.raises(OSError, match="disk full"):
        restore_backup(dataset.archive, destination)
    assert (destination / "RESTORE_INCOMPLETE").exists()


def test_cli_discovers_companion_receipts_and_runs_restore(dataset, tmp_path):
    script = Path(__file__).resolve().parents[1] / "scripts/backup_workstation.py"
    archive = tmp_path / "cli.zip"
    base = [sys.executable, str(script)]
    create = [*base, "create", str(archive), "--state-dir", str(dataset.roots["state"]),
              "--db", str(dataset.roots["database_dir"] / "main.sqlite3"), "--shared", str(dataset.roots["shared"])]
    env = {key: value for key, value in os.environ.items() if key not in ("PYTHONPATH", "PYTHONHOME", "LAN_MESH_CONFIG")}
    planned = subprocess.run([*create, "--plan"], env=env, capture_output=True, text=True, timeout=10)
    assert planned.returncode == 0 and "pm_start_receipts" in planned.stdout
    assert not archive.exists()
    for command in ([*create, "--offline-confirmed"], [*base, "verify", str(archive)],
                    [*base, "restore", str(archive), str(tmp_path / "cli-restore")]):
        run = subprocess.run(command, env=env, capture_output=True, text=True, timeout=20)
        assert run.returncode == 0, run.stderr
        assert "fixture-only" not in run.stdout
    manifest = verify_backup(archive)
    assert "execution_receipts" in manifest["roots"] and "pm_start_receipts" in manifest["roots"]


def test_custom_config_and_explicit_files_override_state_exclusions(dataset, tmp_path, monkeypatch):
    from scripts import backup_workstation as cli
    config = tmp_path / "config.yaml"
    config.write_text(json.dumps({"secretary": {"db_path": str(dataset.roots["database_dir"] / "main.sqlite3"),
                                                "shared_folder": str(dataset.roots["shared"])},
                                  "worker": {"shared_folder": str(dataset.roots["shared"])}}))
    extra = dataset.roots["state"] / "backups" / "old.txt"
    args = SimpleNamespace(state_dir=str(dataset.roots["state"]), config=str(config), db=None,
                           shared=None, worker_shared=None, extra_file=[str(extra)])
    roots = cli.backup_roots(args)
    assert roots["database"] == dataset.roots["database_dir"] / "main.sqlite3"
    assert extra in roots.values()
    assert "worker_shared" not in roots
    args.extra_file = [str(tmp_path / "missing.json")]
    with pytest.raises(ValueError, match="不存在"):
        cli.backup_roots(args)


def test_archive_size_limit_is_checked_before_restore(dataset, tmp_path, monkeypatch):
    create_backup(dataset.roots, dataset.archive, offline_confirmed=True)
    monkeypatch.setattr("lan_mesh.backup.MAX_BYTES", 1)
    with pytest.raises(ValueError, match="体积超过"):
        restore_backup(dataset.archive, tmp_path / "restore")
    assert not (tmp_path / "restore").exists()
