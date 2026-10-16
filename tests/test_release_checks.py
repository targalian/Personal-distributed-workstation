"""Verify isolated data defaults and release failure evidence without calling models."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from lan_mesh.data_paths import data_directory, default_shared_directory
from scripts import release_check


def test_data_directory_keeps_legacy_default_and_rejects_ambiguous_paths(monkeypatch):
    monkeypatch.delenv('LAN_MESH_DATA_DIR', raising=False)
    assert data_directory() == Path.home() / '.lan_mesh'
    assert default_shared_directory() == '~/lan_mesh_shared'
    for invalid in ('', ' ', './relative'):
        monkeypatch.setenv('LAN_MESH_DATA_DIR', invalid)
        with pytest.raises(ValueError):
            data_directory()


def test_isolated_root_routes_identity_token_logs_config_and_database(tmp_path):
    isolated = tmp_path / 'separate data'
    env = os.environ.copy()
    env.pop('PYTHONPATH', None)
    env.pop('PYTHONHOME', None)
    env.pop('LAN_MESH_LOG_FILE', None)
    env['LAN_MESH_DATA_DIR'] = str(isolated)
    code = '''
import json
from lan_mesh.auth import _TOKEN_FILE
from lan_mesh.config import AppConfig, get_db_path, get_shared_folder
from lan_mesh.logger import _get_log_file
from lan_mesh.singleton import _lock_path
from lan_mesh.data_paths import data_directory
cfg=AppConfig()
root=data_directory()
for path in (_TOKEN_FILE, _get_log_file(), _lock_path(), get_db_path(cfg), get_shared_folder(cfg,'secretary'), get_shared_folder(cfg,'worker')):
    assert path.is_relative_to(root), path
print(json.dumps({'root':str(root)}))
'''
    result = subprocess.run([sys.executable, '-c', code], env=env, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert str(isolated) in result.stdout


def test_explicit_config_paths_override_isolated_defaults(tmp_path, monkeypatch):
    from lan_mesh.config import AppConfig, get_db_path, get_shared_folder
    monkeypatch.setenv('LAN_MESH_DATA_DIR', str(tmp_path / 'root'))
    cfg = AppConfig(secretary={'db_path': str(tmp_path / 'custom.db'), 'shared_folder': str(tmp_path / 'custom-shared')})
    assert get_db_path(cfg) == tmp_path / 'custom.db'
    assert get_shared_folder(cfg, 'secretary') == tmp_path / 'custom-shared'


def test_first_run_config_stays_in_selected_data_directory(tmp_path, monkeypatch):
    from lan_mesh.config import get_db_path, get_shared_folder, load_config
    from lan_mesh.preflight import _check_config_file
    isolated = tmp_path / 'data'
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv('LAN_MESH_CONFIG', raising=False)
    monkeypatch.setenv('LAN_MESH_DATA_DIR', str(isolated))
    result = _check_config_file()
    assert result.passed and result.auto_fixed
    assert (isolated / 'config.yaml').is_file()
    assert not (tmp_path / 'config.yaml').exists()
    cfg = load_config()
    assert get_db_path(cfg).is_relative_to(isolated)
    assert get_shared_folder(cfg, 'secretary').is_relative_to(isolated)
    assert get_shared_folder(cfg, 'worker').is_relative_to(isolated)


def test_backup_cli_defaults_to_selected_data_directory(tmp_path):
    isolated = tmp_path / 'data'
    isolated.mkdir()
    config = isolated / 'config.yaml'
    config.write_text('{}')
    env = os.environ.copy()
    env.pop('PYTHONPATH', None)
    env.pop('PYTHONHOME', None)
    env['LAN_MESH_DATA_DIR'] = str(isolated)
    result = subprocess.run([sys.executable, 'scripts/backup_workstation.py', 'create',
        str(tmp_path / 'unused.zip'), '--config', str(config), '--plan'],
        env=env, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert f'state: {isolated}' in result.stdout
    assert not (tmp_path / 'unused.zip').exists()


@pytest.mark.parametrize('parent_wait', [False, True])
def test_release_cleans_descendants_after_exit_or_timeout(tmp_path, parent_wait):
    pid_file = tmp_path / 'child.pid'
    child = "import signal,time; signal.signal(signal.SIGINT,signal.SIG_IGN); time.sleep(30)"
    parent = (f"import subprocess,sys,time; from pathlib import Path; "
              f"p=subprocess.Popen([sys.executable,'-c',{child!r}]); "
              f"Path({str(pid_file)!r}).write_text(str(p.pid)); "
              f"time.sleep({30 if parent_wait else 0})")
    result = release_check.run_check('descendants', [sys.executable, '-c', parent],
                                     tmp_path, os.environ.copy(), 1)
    assert result['status'] == ('timed_out' if parent_wait else 'passed')
    pid = int(pid_file.read_text())
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        status = Path(f'/proc/{pid}/stat')
        if not status.exists() or status.read_text().split()[2] == 'Z':
            break
        time.sleep(.02)
    else:
        pytest.fail(f'Gate left running child {pid}')


def test_release_timeout_is_failure_and_preserves_log(tmp_path):
    result = release_check.run_check('timeout', [sys.executable, '-c',
        "import time; print('entered',flush=True); time.sleep(30)"], tmp_path, os.environ.copy(), .2)
    assert result['status'] == 'timed_out' and result['returncode'] is None
    assert 'entered' in (tmp_path / 'timeout.log').read_text()


def test_release_records_nonzero_child_exit(tmp_path):
    result = release_check.run_check('broken', [sys.executable, '-c', "raise SystemExit(7)"],
                                     tmp_path, os.environ.copy(), 10)
    assert result['status'] == 'failed' and result['returncode'] == 7


def test_missing_release_dependencies_prevent_any_gate(tmp_path, monkeypatch):
    monkeypatch.setattr(release_check, 'locked_environment_errors', lambda: ['Missing locked dependency: playwright'])
    monkeypatch.setattr(release_check, 'run_check', lambda *args: pytest.fail('must not run a gate'))
    output = tmp_path / 'result'
    assert not release_check.run_release_checks(output)
    report = json.loads((output / 'release.json').read_text())
    assert report['status'] == 'failed' and not report['checks'] and report['errors']


@pytest.mark.parametrize('failure', ['dependencies', 'local', 'browser', 'startup', 'skipped_local'])
def test_release_never_passes_after_failed_or_skipped_gate(tmp_path, monkeypatch, failure):
    monkeypatch.setattr(release_check, 'locked_environment_errors', lambda: [])
    seen = []
    def check(name, command, output, env, timeout):
        seen.append(name)
        assert Path(env['LAN_MESH_DATA_DIR']).is_dir()
        assert 'PYTHONHOME' not in env and 'PYTHONPATH' not in env
        if name == 'local':
            (output / 'local-checks.json').write_text(json.dumps({'status': 'passed', 'checks': [
                {'name': 'javascript_syntax', 'status': 'skipped' if failure == 'skipped_local' else 'passed'}]}))
        return {'name': name, 'status': 'failed' if failure == name else 'passed'}
    monkeypatch.setattr(release_check, 'run_check', check)
    output = tmp_path / 'result'
    assert not release_check.run_release_checks(output)
    report = json.loads((output / 'release.json').read_text())
    assert report['status'] == 'failed'
    assert seen[-1] == ('local' if failure == 'skipped_local' else failure)


def test_release_interruption_is_not_success(tmp_path, monkeypatch):
    monkeypatch.setattr(release_check, 'locked_environment_errors', lambda: [])
    def interrupt(*args):
        raise KeyboardInterrupt()
    monkeypatch.setattr(release_check, 'run_check', interrupt)
    with pytest.raises(KeyboardInterrupt):
        release_check.run_release_checks(tmp_path / 'result')
    assert json.loads((tmp_path / 'result/release.json').read_text())['status'] == 'interrupted'


def test_existing_release_evidence_is_never_overwritten(tmp_path):
    original = tmp_path / 'release.json'
    original.write_text('previous evidence')
    with pytest.raises(FileExistsError):
        release_check.run_release_checks(tmp_path)
    assert original.read_text() == 'previous evidence'
