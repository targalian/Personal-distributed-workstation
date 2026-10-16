"""Reject unsafe upgrade inputs and preserve evidence when a real drill cannot finish."""
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from scripts import upgrade_drill as drill


@pytest.fixture
def repository(tmp_path, monkeypatch):
    root = tmp_path / 'repository'
    root.mkdir()
    (root / 'main.py').write_text('print("fixture")')
    app = root / 'lan_mesh'
    app.mkdir()
    (app / 'data_paths.py').write_text('# supports isolated data')
    (app / 'module.py').write_text('VALUE = 1')
    for name in ('model_pool.yaml', 'resources.yaml', '.env'):
        (app / name).write_text('private fixture contents')
    subprocess.run(['git', 'init', '-q', str(root)], check=True)
    subprocess.run(['git', 'add', '.'], cwd=root, check=True)
    subprocess.run(['git', '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                    'commit', '-qm', 'fixture'], cwd=root, check=True)
    monkeypatch.setattr(drill, 'ROOT', root)
    monkeypatch.setattr(drill, 'SOURCE_PATHS', ['lan_mesh', 'main.py'])
    return root


def test_export_does_not_copy_private_runtime_configuration(repository, tmp_path):
    destination = tmp_path / 'export'
    commit = drill.export_baseline('HEAD', destination)
    assert len(commit) == 40
    assert (destination / 'lan_mesh/module.py').read_text() == 'VALUE = 1'
    assert not any((destination / 'lan_mesh' / name).exists() for name in ('model_pool.yaml', 'resources.yaml', '.env'))


def test_baseline_symbolic_link_is_rejected(repository, tmp_path):
    (repository / 'lan_mesh/external.py').symlink_to('/etc/passwd')
    subprocess.run(['git', 'add', 'lan_mesh/external.py'], cwd=repository, check=True)
    subprocess.run(['git', '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                    'commit', '-qm', 'link'], cwd=repository, check=True)
    with pytest.raises(ValueError, match='unsafe'):
        drill.export_baseline('HEAD', tmp_path / 'export')
    assert not (tmp_path / 'export/lan_mesh/external.py').exists()


def test_candidate_link_is_not_followed(repository, tmp_path):
    (repository / 'lan_mesh/external.py').symlink_to('/etc/passwd')
    with pytest.raises(ValueError, match='symbolic link'):
        drill.copy_candidate(tmp_path / 'candidate')
    assert not (tmp_path / 'candidate/lan_mesh/external.py').exists()


def test_child_environment_drops_ambient_keys_resources_and_config(tmp_path, monkeypatch):
    for key in ('OPENAI_API_KEY', 'LAN_MESH_RESOURCES', 'LAN_MESH_API_KEY', 'LAN_MESH_CONFIG',
                'PYTHONPATH', 'PYTHONHOME', 'CLOUD_STORAGE_ACCESS_KEY'):
        monkeypatch.setenv(key, 'fixture-value')
    env = drill.isolated_env(tmp_path / 'state', tmp_path / 'service.log')
    result = subprocess.run([sys.executable, '-c',
        "import os,json; print(json.dumps(dict(os.environ)))"], env=env,
        capture_output=True, text=True, check=True, timeout=10)
    actual = json.loads(result.stdout)
    assert actual['LAN_MESH_DATA_DIR'] == str(tmp_path / 'state')
    assert not any(value == 'fixture-value' for value in actual.values())


def test_missing_baseline_is_failure_before_any_launcher(repository, tmp_path):
    output = tmp_path / 'evidence'
    with pytest.raises(subprocess.CalledProcessError):
        drill.run_drill(output, 'missing-commit', Path(sys.prefix), Path(sys.prefix))
    report = json.loads((output / 'upgrade.json').read_text())
    assert report['status'] == 'failed' and report['stage'] == 'sources_and_runtimes'
    assert not report['steps'] and not list(output.glob('*launcher.log'))


@pytest.mark.parametrize('interrupted', [False, True])
def test_runtime_failure_or_interruption_cannot_pass(repository, tmp_path, monkeypatch, interrupted):
    def fail(*args):
        if interrupted:
            raise KeyboardInterrupt()
        raise ValueError('runtime lock mismatch')
    monkeypatch.setattr(drill, 'check_runtime', fail)
    output = tmp_path / 'evidence'
    with pytest.raises(KeyboardInterrupt if interrupted else ValueError):
        drill.run_drill(output, 'HEAD', Path(sys.prefix), Path(sys.prefix))
    report = json.loads((output / 'upgrade.json').read_text())
    assert report['status'] == ('interrupted' if interrupted else 'failed')
    assert not report['steps'] and report['finished_at']


def test_existing_evidence_is_not_overwritten(tmp_path):
    report = tmp_path / 'upgrade.json'
    report.write_text('original')
    with pytest.raises(FileExistsError):
        drill.run_drill(tmp_path, 'HEAD', Path(sys.prefix), Path(sys.prefix))
    assert report.read_text() == 'original'


def test_hung_shutdown_is_failure_even_when_forced_cleanup_succeeds(tmp_path):
    ready = tmp_path / 'ready'
    code = ("import signal,sys,time; from pathlib import Path; "
            "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
            "Path(sys.argv[1]).touch(); time.sleep(30)")
    proc = subprocess.Popen([sys.executable, '-c', code, str(ready)], start_new_session=True)
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        assert ready.exists()
        with pytest.raises(RuntimeError, match='shutdown timed out'):
            drill.stop(proc, timeout=.1)
        assert proc.poll() is not None
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
