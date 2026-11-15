"""整机备份：在线快照、归档校验、恢复到新目录。"""
import os
import sqlite3
import zipfile
from types import SimpleNamespace

import pytest

from lan_mesh.backup import create_backup, restore_backup, verify_backup
from lan_mesh.database import Database
from lan_mesh.protocol import Task


@pytest.fixture
def dataset(tmp_path, monkeypatch):
    monkeypatch.setattr(Database, "backup", lambda self: "")
    db_path = tmp_path / "db" / "main.sqlite3"
    db_path.parent.mkdir()
    shared = tmp_path / "shared"
    (shared / "deliverables").mkdir(parents=True)
    (shared / "deliverables" / "交付物.md").write_text("交付内容", encoding="utf-8")
    config = tmp_path / "config.yaml"
    config.write_text("secretary:\n  db_path: ~/.lan_mesh/secretary.sqlite3\n", encoding="utf-8")
    db = Database(str(db_path))
    db.save_task(Task(task_id="t-1", name="备份任务", status="accepted",
                      input_data={"k": 1}, output_data={"deliverable": "交付产物"}))
    roots = {"database": db_path, "shared": shared, "config_0": config}
    return SimpleNamespace(roots=roots, archive=tmp_path / "backup.zip", db=db,
                           db_path=db_path, shared=shared, config=config)


def test_roundtrip_restores_database_and_shared_files(dataset, tmp_path):
    manifest = create_backup(dataset.roots, dataset.archive, offline_confirmed=True)
    names = {item["path"] for item in manifest["files"]}
    assert "database/main.sqlite3" in names
    assert "shared/deliverables/交付物.md" in names
    assert "config_0/config.yaml" in names
    assert manifest["format"] == 1

    verify_backup(dataset.archive)
    destination = tmp_path / "restored"
    restore_backup(dataset.archive, destination)

    restored = Database(str(destination / "database" / "main.sqlite3"))
    assert restored.get_task("t-1").status == "accepted"
    restored._get_conn().close()
    assert (destination / "shared" / "deliverables" / "交付物.md").read_text(encoding="utf-8") == "交付内容"
    assert (destination / "manifest.json").is_file()
    assert not (destination / "RESTORE_INCOMPLETE").exists()
    assert (destination / "恢复说明.md").is_file()


def test_wal_snapshot_captures_committed_rows(tmp_path, monkeypatch):
    monkeypatch.setattr(Database, "backup", lambda self: "")
    source = tmp_path / "wal"
    source.mkdir()
    path = source / "data.sqlite3"
    writer = sqlite3.connect(path)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("CREATE TABLE notes (value TEXT)")
        writer.execute("INSERT INTO notes VALUES ('committed in wal')")
        writer.commit()
        manifest = create_backup({"database": path}, tmp_path / "wal.zip", offline_confirmed=True)
        assert manifest["files"][0]["kind"] == "sqlite"
    finally:
        writer.close()
    destination = tmp_path / "wal-restored"
    restore_backup(tmp_path / "wal.zip", destination)
    with sqlite3.connect(destination / "database" / "data.sqlite3") as conn:
        assert conn.execute("SELECT value FROM notes").fetchone()[0] == "committed in wal"


def test_create_requires_offline_confirmation(dataset):
    with pytest.raises(ValueError, match="offline-confirmed"):
        create_backup(dataset.roots, dataset.archive)


def test_archive_must_not_live_inside_a_backup_source(dataset):
    with pytest.raises(ValueError, match="不能位于备份源内部"):
        create_backup(dataset.roots, dataset.shared / "backup.zip", offline_confirmed=True)


def test_verify_detects_tampered_member(dataset):
    create_backup(dataset.roots, dataset.archive, offline_confirmed=True)
    with zipfile.ZipFile(dataset.archive) as bundle:
        members = {info.filename: bundle.read(info.filename) for info in bundle.infolist()}
    members["shared/deliverables/交付物.md"] = "被篡改".encode("utf-8")
    with zipfile.ZipFile(dataset.archive, "w") as bundle:
        for name, content in members.items():
            bundle.writestr(name, content)
    with pytest.raises(ValueError, match="文件校验失败"):
        verify_backup(dataset.archive)


def test_restore_refuses_existing_destination(dataset, tmp_path):
    create_backup(dataset.roots, dataset.archive, offline_confirmed=True)
    destination = tmp_path / "occupied"
    destination.mkdir()
    with pytest.raises(FileExistsError):
        restore_backup(dataset.archive, destination)


@pytest.mark.skipif(os.name == "nt", reason="Windows 创建符号链接需要额外权限")
def test_symlinked_source_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(Database, "backup", lambda self: "")
    real = tmp_path / "real"
    real.mkdir()
    (real / "file.txt").write_text("x", encoding="utf-8")
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(ValueError, match="符号链接"):
        create_backup({"shared": link}, tmp_path / "s.zip", offline_confirmed=True)


def test_manifest_records_roots_and_directories(dataset):
    manifest = create_backup(dataset.roots, dataset.archive, offline_confirmed=True)
    assert set(manifest["roots"]) == {"database", "shared", "config_0"}
    assert manifest["root_types"]["config_0"] == "file"
    assert "shared/deliverables" in manifest["directories"]
