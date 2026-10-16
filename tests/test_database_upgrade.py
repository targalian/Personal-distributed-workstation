"""Verify schema upgrade rollback, pre-write snapshots and recovery across real processes."""
import json
import os
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

import pytest

import lan_mesh.database as database
from lan_mesh.backup import create_backup, restore_backup
from lan_mesh.database import Database
from lan_mesh.protocol import Task
from lan_mesh.schema_upgrade import add_column
from scripts.startup_smoke import ROOT, free_port


@pytest.fixture
def legacy(tmp_path, monkeypatch):
    state = tmp_path / 'state'
    monkeypatch.setenv('LAN_MESH_DATA_DIR', str(state))
    monkeypatch.setattr(Database, 'backup', lambda self: '')
    path = state / 'secretary.sqlite3'
    db = Database(str(path))
    db.save_task(Task(task_id='accepted-before-upgrade', name='Reviewed delivery', status='accepted',
                      input_data={'_brief': {'revision': 2}},
                      output_data={'_delivery': {'accepted': True}, 'deliverable': 'saved artifact'}))
    conn = db._get_conn()
    conn.execute('DROP TABLE task_resubmissions')
    conn.execute("UPDATE schema_meta SET value='4' WHERE key='version'")
    conn.commit()
    conn.close()
    return path


def dump(path):
    with closing(sqlite3.connect(path)) as conn:
        return '\n'.join(conn.iterdump())


def assert_old(path):
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("SELECT value FROM schema_meta WHERE key='version'").fetchone()[0] == '4'
        assert not conn.execute("SELECT name FROM sqlite_master WHERE name='task_resubmissions'").fetchall()
        task = conn.execute("SELECT status,input_data,output_data FROM tasks WHERE task_id='accepted-before-upgrade'").fetchone()
        assert task[0] == 'accepted' and json.loads(task[1])['_brief']['revision'] == 2
        assert json.loads(task[2])['_delivery']['accepted'] is True
        assert conn.execute('PRAGMA integrity_check').fetchall() == [('ok',)]


def test_upgrade_snapshot_precedes_all_schema_writes_and_is_not_pruned(legacy):
    before = dump(legacy)
    db = Database(str(legacy))
    snapshot = db.upgrade_backup_path
    assert snapshot and dump(snapshot) == before
    assert_old(snapshot)
    assert db.get_task('accepted-before-upgrade').status == 'accepted'
    assert db._get_schema_version(db._get_conn()) == database.SCHEMA_VERSION
    db._get_conn().close()
    again = Database(str(legacy))
    assert again.upgrade_backup_path is None
    again._get_conn().close()
    assert snapshot.exists() and snapshot.stat().st_mode & 0o777 == 0o600


def test_migration_failure_rolls_back_ddl_data_and_version(legacy, monkeypatch):
    before = dump(legacy)
    def fail(conn):
        conn.execute('CREATE TABLE partially_created (value TEXT)')
        conn.execute("UPDATE tasks SET status='failed'")
        raise sqlite3.OperationalError('injected migration write failure')
    monkeypatch.setitem(database._MIGRATIONS, 5, fail)
    with pytest.raises(sqlite3.OperationalError, match='injected'):
        Database(str(legacy))
    assert dump(legacy) == before
    assert_old(legacy)
    assert len(list((legacy.parent / 'backups/upgrades').glob('*.sqlite3'))) == 1


def test_missing_migration_cannot_advance_version(legacy, monkeypatch):
    before = dump(legacy)
    monkeypatch.delitem(database._MIGRATIONS, 5)
    with pytest.raises(ValueError, match='Missing schema migration'):
        Database(str(legacy))
    assert dump(legacy) == before


def test_failed_pre_upgrade_snapshot_prevents_any_schema_write(legacy, monkeypatch):
    before = dump(legacy)
    def fail(*args):
        raise OSError('backup disk full')
    monkeypatch.setattr(database, 'backup_before_upgrade', fail)
    with pytest.raises(OSError, match='backup disk full'):
        Database(str(legacy))
    assert dump(legacy) == before


def test_snapshot_sync_failure_publishes_no_backup_and_preserves_source(legacy, monkeypatch):
    before = dump(legacy)
    def fail(_):
        raise OSError('snapshot fsync failed')
    monkeypatch.setattr('lan_mesh.schema_upgrade.os.fsync', fail)
    with pytest.raises(OSError, match='snapshot fsync failed'):
        Database(str(legacy))
    assert dump(legacy) == before
    assert not list((legacy.parent / 'backups/upgrades').iterdir())


def test_crash_before_snapshot_publication_leaves_only_pending_file(legacy):
    before = dump(legacy)
    code = '''
import os,sys
import lan_mesh.schema_upgrade as upgrade
from lan_mesh.database import Database
upgrade.os.fsync = lambda _: os._exit(24)
Database(sys.argv[1])
'''
    env = os.environ.copy()
    env.pop('PYTHONPATH', None)
    env.pop('PYTHONHOME', None)
    result = subprocess.run([sys.executable, '-c', code, str(legacy)], env=env,
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 24, result.stderr
    assert dump(legacy) == before
    folder = legacy.parent / 'backups/upgrades'
    assert list(folder.glob('*.pending')) and not list(folder.glob('*.sqlite3'))
    db = Database(str(legacy))
    assert db.upgrade_backup_path.is_file()
    assert_old(db.upgrade_backup_path)
    db._get_conn().close()


@pytest.mark.parametrize('version', ['999', '-1', 'invalid'])
def test_unsupported_schema_is_rejected_without_changing_database(legacy, version):
    with closing(sqlite3.connect(legacy)) as conn:
        conn.execute("UPDATE schema_meta SET value=? WHERE key='version'", (version,))
        conn.commit()
    before = dump(legacy)
    with pytest.raises(ValueError):
        Database(str(legacy))
    assert dump(legacy) == before
    assert not (legacy.parent / 'backups/upgrades').exists()


def test_add_column_only_ignores_a_column_that_already_exists(tmp_path):
    with closing(sqlite3.connect(tmp_path / 'schema.sqlite3')) as conn:
        conn.execute('CREATE TABLE sample (value TEXT)')
        add_column(conn, 'sample', 'value', 'TEXT')
        with pytest.raises(sqlite3.OperationalError, match='no such table'):
            add_column(conn, 'missing', 'value', 'TEXT')
        conn.execute('CREATE VIEW sample_view AS SELECT value FROM sample')
        with pytest.raises(sqlite3.OperationalError):
            add_column(conn, 'sample_view', 'new_value', 'TEXT')


def test_snapshot_includes_committed_wal_and_concurrent_upgrade_runs_once(legacy):
    keeper = sqlite3.connect(legacy)
    try:
        keeper.execute('PRAGMA journal_mode=WAL')
        keeper.execute("UPDATE tasks SET name='committed WAL content'")
        keeper.commit()
        def upgrade(_):
            db = Database(str(legacy))
            snapshot = db.upgrade_backup_path
            db._get_conn().close()
            return snapshot
        with ThreadPoolExecutor(max_workers=2) as pool:
            snapshots = [p for p in pool.map(upgrade, range(2)) if p]
        assert len(snapshots) == 1
        assert_old(snapshots[0])
        with closing(sqlite3.connect(snapshots[0])) as conn:
            assert conn.execute('SELECT name FROM tasks').fetchone()[0] == 'committed WAL content'
    finally:
        keeper.close()


@pytest.mark.parametrize('journal_mode', ['DELETE', 'WAL'])
def test_process_death_during_schema_change_leaves_original_database(legacy, journal_mode):
    with closing(sqlite3.connect(legacy)) as conn:
        conn.execute(f'PRAGMA journal_mode={journal_mode}')
    before = dump(legacy)
    code = '''
import os,sys
import lan_mesh.database as module
def interrupted(conn):
    conn.execute('CREATE TABLE interrupted_upgrade (value TEXT)')
    conn.execute("UPDATE tasks SET status='failed'")
    os._exit(23)
module._MIGRATIONS[5] = interrupted
module.Database(sys.argv[1])
'''
    env = os.environ.copy()
    env.pop('PYTHONPATH', None)
    env.pop('PYTHONHOME', None)
    result = subprocess.run([sys.executable, '-c', code, str(legacy)], env=env,
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 23, result.stderr
    assert dump(legacy) == before
    assert_old(legacy)
    recovered = Database(str(legacy))
    assert recovered._get_schema_version(recovered._get_conn()) == database.SCHEMA_VERSION
    recovered._get_conn().close()


@pytest.mark.parametrize('fail_late', [False, True])
def test_legacy_without_metadata_upgrades_all_columns_or_rolls_back_every_step(legacy, monkeypatch, fail_late):
    with closing(sqlite3.connect(legacy)) as conn:
        for index in ('idx_resource_usage_uid', 'idx_resource_usage_task'):
            conn.execute(f'DROP INDEX {index}')
        for table, columns in (
            ('tasks', ('project_id', 'pm_agent_id')),
            ('hosts', ('rating_tier', 'rating_score', 'rating_summary', 'code_version', 'version_ts')),
            ('resource_usage_log', ('usage_id', 'reported', 'task_id', 'project_id')),
        ):
            for column in columns:
                conn.execute(f'ALTER TABLE {table} DROP COLUMN {column}')
        conn.execute('DROP TABLE schema_meta')
        conn.execute('DROP TABLE graph_checkpoints')
        conn.commit()
    before = dump(legacy)
    if fail_late:
        def fail(conn):
            raise sqlite3.OperationalError('last migration failed')
        monkeypatch.setitem(database._MIGRATIONS, 5, fail)
        with pytest.raises(sqlite3.OperationalError, match='last migration failed'):
            Database(str(legacy))
        assert dump(legacy) == before
    else:
        db = Database(str(legacy))
        assert db.get_task('accepted-before-upgrade').status == 'accepted'
        assert db._get_schema_version(db._get_conn()) == database.SCHEMA_VERSION
        assert dump(db.upgrade_backup_path) == before
        conn = db._get_conn()
        conn.execute("INSERT INTO resource_usage_log (usage_id,task_id,project_id) VALUES ('once','t','p')")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO resource_usage_log (usage_id) VALUES ('once')")
        conn.rollback()
        conn.close()


def request(port, path, payload=None, method=None):
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(f'http://127.0.0.1:{port}{path}', data=data, method=method,
                                 headers={'Content-Type': 'application/json'})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=5) as response:
        return json.load(response)


def start_station(project, state, log_path):
    port = free_port()
    config = project / 'drill-config.json'
    config.write_text(json.dumps({'auto_upgrade': False, 'discovery': {'port': free_port(socket.SOCK_DGRAM)},
                                  'security': {'auth_enabled': False}}))
    env = os.environ.copy()
    for name in ('PYTHONPATH', 'PYTHONHOME', 'LAN_MESH_CONFIG', 'LAN_MESH_MODEL_POOL', 'LAN_MESH_RESOURCES'):
        env.pop(name, None)
    env.update(LAN_MESH_DATA_DIR=str(state), LAN_MESH_LOG_FILE=str(log_path.with_suffix('.service.log')))
    with log_path.open('w') as log:
        proc = subprocess.Popen([sys.executable, str(project / 'main.py'), 'station', '--port', str(port),
                                 '--config', str(config)], cwd=project, env=env, stdout=log,
                                stderr=subprocess.STDOUT, start_new_session=True)
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise AssertionError(log_path.read_text())
            try:
                health = request(port, '/api/health')
                assert request(port, '/api/station/activate-secretary', {})['ok']
                return proc, port, health['device_id']
            except OSError:
                time.sleep(.1)
        raise AssertionError('Restored Station did not become ready')
    except BaseException:
        stop_station(proc)
        raise


def stop_station(proc):
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        proc.wait(timeout=10)
    finally:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait(timeout=5)


def test_real_station_reopens_offline_restored_data_at_original_path(legacy, tmp_path):
    project = tmp_path / 'project'
    project.mkdir()
    shutil.copytree(ROOT / 'lan_mesh', project / 'lan_mesh', ignore=shutil.ignore_patterns(
        '__pycache__', '*.pyc', '.env*', 'model_pool.yaml', 'resources.yaml', '*.sqlite3', '*.log'))
    shutil.copy2(ROOT / 'main.py', project / 'main.py')
    proc, port, identity = start_station(project, legacy.parent, tmp_path / 'before.log')
    try:
        assert request(port, '/api/tasks/accepted-before-upgrade')['status'] == 'accepted'
        cid = request(port, '/api/conversations', {'title': 'Restore drill'})['id']
        path = f'/api/conversations/{cid}/brief'
        request(port, path, {'expected_revision': 0, 'document': {'title': 'Before restore'}}, 'PUT')
        request(port, path, {'expected_revision': 1, 'document': {'title': 'Reviewed revision'}}, 'PUT')
    finally:
        stop_station(proc)
    state = legacy.parent
    archive = tmp_path / 'offline.zip'
    create_backup({'state': state}, archive, offline_confirmed=True)
    # The original is retained, and a fully verified offline snapshot takes its exact path.
    original = tmp_path / 'retained-original'
    state.rename(original)
    restored = tmp_path / 'restored'
    restore_backup(archive, restored)
    (restored / 'state').rename(state)
    proc, port, restored_identity = start_station(project, state, tmp_path / 'restored.log')
    try:
        assert restored_identity == identity
        assert request(port, '/api/tasks/accepted-before-upgrade')['output_data']['_delivery']['accepted']
        brief = request(port, path)
        assert brief['revision'] == 2 and brief['document']['title'] == 'Reviewed revision'
        assert len(request(port, path + '/history')['revisions']) == 2
        request(port, path, {'expected_revision': 2, 'document': {'title': 'After switch'}}, 'PUT')
    finally:
        stop_station(proc)
    # Revert a trial switch without mixing files: retain it and restore the original root.
    state.rename(tmp_path / 'retained-trial')
    original.rename(state)
    proc, port, rollback_identity = start_station(project, state, tmp_path / 'rollback.log')
    try:
        assert rollback_identity == identity
        assert request(port, path)['revision'] == 2
        assert request(port, path)['document']['title'] == 'Reviewed revision'
        assert request(port, '/api/tasks/accepted-before-upgrade')['status'] == 'accepted'
    finally:
        stop_station(proc)
