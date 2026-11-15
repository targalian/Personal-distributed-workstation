"""数据库升级安全：写前快照、原子回滚、版本守卫。"""
import os
import sqlite3
from contextlib import closing

import pytest

import lan_mesh.database as database
import lan_mesh.schema_upgrade as schema_upgrade
from lan_mesh.database import Database
from lan_mesh.protocol import Task


@pytest.fixture
def legacy(tmp_path, monkeypatch):
    state = tmp_path / "state"
    snapshots = tmp_path / "snapshots"
    monkeypatch.setattr(schema_upgrade, "backup_root", lambda: snapshots)
    monkeypatch.setattr(Database, "backup", lambda self: "")
    path = state / "secretary.sqlite3"
    db = Database(str(path))
    db.save_task(Task(task_id="accepted-before-upgrade", name="Reviewed delivery",
                      status="accepted", input_data={"k": 1},
                      output_data={"deliverable": "saved artifact"}))
    conn = db._get_conn()
    # 退回到 v4，并删掉 v5 才创建的表
    conn.execute("UPDATE schema_meta SET value='4' WHERE key='version'")
    conn.execute("DROP TABLE IF EXISTS llm_call_log")
    conn.commit()
    conn.close()
    return path


def dump(path) -> str:
    with closing(sqlite3.connect(str(path))) as conn:
        return "\n".join(conn.iterdump())


def assert_old(path) -> None:
    with closing(sqlite3.connect(str(path))) as conn:
        version = conn.execute(
            "SELECT value FROM schema_meta WHERE key='version'").fetchone()[0]
        assert version == "4"
        assert not conn.execute(
            "SELECT name FROM sqlite_master WHERE name='llm_call_log'").fetchall()
        task = conn.execute(
            "SELECT status,input_data,output_data FROM tasks WHERE task_id='accepted-before-upgrade'"
        ).fetchone()
        assert task[0] == "accepted" and task[1] == '{"k": 1}'
        assert conn.execute("PRAGMA integrity_check").fetchall() == [("ok",)]


def test_upgrade_snapshot_precedes_all_schema_writes(legacy):
    before = dump(legacy)
    db = Database(str(legacy))
    snapshot = db.upgrade_backup_path
    assert snapshot and dump(snapshot) == before
    assert_old(snapshot)
    assert db.get_task("accepted-before-upgrade").status == "accepted"
    assert db._get_schema_version(db._get_conn()) == database.SCHEMA_VERSION
    db._get_conn().close()

    again = Database(str(legacy))
    assert again.upgrade_backup_path is None
    again._get_conn().close()
    assert snapshot.exists()
    if os.name == 'posix':
        assert snapshot.stat().st_mode & 0o777 == 0o600


def test_migration_failure_rolls_back_ddl_data_and_version(legacy, monkeypatch):
    before = dump(legacy)

    def fail(conn):
        conn.execute("CREATE TABLE partially_created (value TEXT)")
        conn.execute("UPDATE tasks SET status='failed'")
        raise sqlite3.OperationalError("injected migration write failure")

    monkeypatch.setitem(database._MIGRATIONS, 5, fail)
    with pytest.raises(sqlite3.OperationalError, match="injected"):
        Database(str(legacy))
    assert dump(legacy) == before
    assert_old(legacy)
    snapshots = legacy.parent.parent / "snapshots"
    assert len(list(snapshots.glob("*.sqlite3"))) == 1


def test_missing_migration_cannot_advance_version(legacy, monkeypatch):
    before = dump(legacy)
    monkeypatch.delitem(database._MIGRATIONS, 5)
    with pytest.raises(ValueError, match="Missing schema migration"):
        Database(str(legacy))
    assert dump(legacy) == before


def test_future_schema_version_is_rejected(legacy):
    conn = sqlite3.connect(str(legacy))
    conn.execute("UPDATE schema_meta SET value='999' WHERE key='version'")
    conn.commit()
    conn.close()
    before = dump(legacy)
    with pytest.raises(ValueError, match="Unsupported database schema v999"):
        Database(str(legacy))
    assert dump(legacy) == before
    snapshots = legacy.parent.parent / "snapshots"
    assert not snapshots.exists() or not list(snapshots.glob("*.sqlite3"))


def test_failed_pre_upgrade_snapshot_prevents_any_schema_write(legacy, monkeypatch):
    before = dump(legacy)

    def fail(*args):
        raise OSError("backup disk full")

    monkeypatch.setattr(database, "backup_before_upgrade", fail)
    with pytest.raises(OSError, match="backup disk full"):
        Database(str(legacy))
    assert dump(legacy) == before


def test_fresh_database_has_no_upgrade_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr(schema_upgrade, "backup_root", lambda: tmp_path / "snapshots")
    monkeypatch.setattr(Database, "backup", lambda self: "")
    db = Database(str(tmp_path / "fresh.sqlite3"))
    assert db.upgrade_backup_path is None
    assert db._get_schema_version(db._get_conn()) == database.SCHEMA_VERSION
    db._get_conn().close()


def test_execute_schema_rejects_incomplete_statement():
    with closing(sqlite3.connect(":memory:")) as conn:
        with pytest.raises(ValueError, match="incomplete statement"):
            schema_upgrade.execute_schema(conn, "CREATE TABLE half (id INTEGER")
