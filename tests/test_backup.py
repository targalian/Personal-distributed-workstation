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


def test_backup_command_uses_the_home_config(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from scripts.backup_workstation import backup_roots

    home = tmp_path / "home"
    (home / ".lan_mesh").mkdir(parents=True)
    db = tmp_path / "secretary.sqlite3"
    db.write_bytes(b"")
    shared = tmp_path / "shared"
    shared.mkdir()
    (home / ".lan_mesh" / "config.yaml").write_text(
        "secretary:\n"
        f"  db_path: {db.as_posix()}\n"
        f"  shared_folder: {shared.as_posix()}\n",
        encoding="utf-8")
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(tmp_path)
    roots = backup_roots(SimpleNamespace(config=None, db=None, shared=None, extra_file=[]))
    assert roots["database"] == db.absolute()
    assert roots["shared"] == shared.absolute()
    assert any(path == (home / ".lan_mesh" / "config.yaml").absolute() for path in roots.values())


@pytest.fixture
def isolated_backup_command(tmp_path, monkeypatch):
    from pathlib import Path

    from scripts import backup_workstation

    project = tmp_path / "project"
    (project / "lan_mesh").mkdir(parents=True)
    home = tmp_path / "home"
    home.mkdir()
    db = tmp_path / "main.sqlite3"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE notes (value TEXT)")
    monkeypatch.setattr(backup_workstation, "ROOT", project)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv("LAN_MESH_CONFIG", raising=False)
    monkeypatch.chdir(project)
    args = SimpleNamespace(config=None, db=str(db), shared=None, extra_file=[])
    return SimpleNamespace(command=backup_workstation, project=project, home=home,
                           args=args, archive=tmp_path / "command.zip")


def test_backup_command_includes_resources_yaml(isolated_backup_command, tmp_path, monkeypatch, capsys):
    setup = isolated_backup_command
    resources = setup.project / "lan_mesh" / "resources.yaml"
    resources.write_text("resources: []\n", encoding="utf-8")
    roots = setup.command.backup_roots(setup.args)
    label = next(label for label, path in roots.items() if path == resources)
    assert label.startswith("config_")
    manifest = create_backup(roots, setup.archive, offline_confirmed=True)
    assert f"{label}/resources.yaml" in {item["path"] for item in manifest["files"]}
    destination = tmp_path / "resources-restored"
    restore_backup(setup.archive, destination)
    assert (destination / label / "resources.yaml").read_bytes() == resources.read_bytes()
    monkeypatch.setattr("sys.argv", ["backup_workstation.py", "create", str(tmp_path / "plan.zip"), "--plan"])
    assert setup.command.main() == 0
    assert f"{label}: {resources}" in capsys.readouterr().out
    assert not (tmp_path / "plan.zip").exists()


def test_backup_command_state_preserves_identity_and_excludes_runtime(isolated_backup_command, tmp_path):
    setup = isolated_backup_command
    state = setup.home / ".lan_mesh"
    state.mkdir()
    for name in ("mesh_token", "device_id_station", "device_id_worker", "mcp_servers.yaml", "station.lock"):
        (state / name).write_text(f"fixture-{name}", encoding="utf-8")
    for name in ("logs", "backups"):
        (state / name / "nested").mkdir(parents=True)
        (state / name / "nested" / "ignored.txt").write_text("ignored", encoding="utf-8")
    roots = setup.command.backup_roots(setup.args)
    assert roots["state"] == state
    manifest = create_backup(roots, setup.archive, offline_confirmed=True)
    expected = {f"state/{name}" for name in
                ("mesh_token", "device_id_station", "device_id_worker", "mcp_servers.yaml")}
    assert {item["path"] for item in manifest["files"] if item["path"].startswith("state/")} == expected
    assert not any(name.startswith(("state/logs", "state/backups")) for name in manifest["directories"])
    destination = tmp_path / "state-restored"
    restore_backup(setup.archive, destination)
    for name in expected:
        assert (destination / name).read_bytes() == (state / name.split("/")[1]).read_bytes()
    assert not any((destination / "state" / name).exists() for name in ("logs", "backups", "station.lock"))


def test_backup_command_skips_missing_state(isolated_backup_command):
    setup = isolated_backup_command
    roots = setup.command.backup_roots(setup.args)
    assert "state" not in roots
    manifest = create_backup(roots, setup.archive, offline_confirmed=True)
    assert "state" not in manifest["roots"]
