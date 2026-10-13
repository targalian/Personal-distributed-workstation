"""Launch the shell wrapper in temporary projects to verify errors, arguments and cleanup."""
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from scripts import check_environment

ROOT = Path(__file__).resolve().parent.parent
BASH = shutil.which("bash")
pytestmark = pytest.mark.skipif(BASH is None or os.name == "nt", reason="Unix launcher requires Bash")


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project with spaces"
    (root / "scripts").mkdir(parents=True)
    shutil.copy2(ROOT / "scripts/start_workstation.sh", root / "scripts/start_workstation.sh")
    (root / "requirements.txt").write_text("fastapi>=0.104\n")
    (root / "requirements-dev.txt").write_text("-r requirements.txt\npytest>=7.4\n")
    (root / ".venv/bin").mkdir(parents=True)
    (root / ".venv/pyvenv.cfg").write_text("home = /unused\n")
    # A controlled stand-in for the Python application: no network or real Station.
    executable = root / ".venv/bin/python"
    executable.write_text(f"#!{sys.executable}\n" + '''
import json, os, sys, time, signal
from pathlib import Path
root = Path(__file__).resolve().parents[2]
args = sys.argv[1:]
assert 'PYTHONPATH' not in os.environ
assert 'PYTHONHOME' not in os.environ
if args == ['--version']:
    print('Python 3.10.12')
elif args[:2] == ['-m', 'pip']:
    if 'install' in args:
        if (root / 'install-fails').exists(): sys.exit(7)
        (root / 'dependencies-ready').touch()
elif args and args[0].endswith('check_environment.py'):
    sys.exit(0 if (root / 'dependencies-ready').exists() else 1)
elif args and args[0].endswith('main.py'):
    role = args[1]
    (root / (role + '.json')).write_text(json.dumps({'args':args, 'pid':os.getpid()}))
    def stopped(*_):
        (root / (role + '.stopped')).touch()
        sys.exit(0)
    signal.signal(signal.SIGTERM, stopped)
    if role == 'station' and not (root / 'station-waits').exists():
        time.sleep(.15)
        sys.exit(23 if (root / 'station-fails').exists() else 0)
    if role == 'worker' and (root / 'worker-fails').exists(): sys.exit(9)
    while True: time.sleep(.05)
''', encoding="utf-8")
    executable.chmod(0o755)
    return root


def command(root, *args):
    return [BASH, str(root / "scripts/start_workstation.sh"), *args]


def run(root, *args, cwd=None):
    env = os.environ.copy()
    env["PYTHONPATH"] = "/nonexistent/ros-path"
    return subprocess.run(command(root, *args), cwd=cwd or root, env=env,
                          text=True, capture_output=True, timeout=10)


@pytest.mark.parametrize('args', [
    ('--port',), ('--port', 'abc'), ('--port', '0'), ('--port', '65536'),
    ('--name', '--check'), ('--unknown',), ('--config', 'absent.yaml'),
    ('--with-worker', '--worker-port', '45470'), ('--with-worker', '--dev'),
])
def test_invalid_arguments_fail_before_environment_changes(project, args):
    result = run(project, *args)
    assert result.returncode != 0
    assert '[ERROR]' in result.stderr
    assert 'unbound variable' not in result.stderr
    assert not (project / 'dependencies-ready').exists()
    assert not (project / 'station.json').exists()


def test_help_has_no_environment_side_effects(project):
    result = run(project, '--help')
    assert result.returncode == 0
    assert '--check' in result.stdout
    assert not (project / 'dependencies-ready').exists()


def test_incompatible_environment_is_preserved(project):
    (project / '.venv/bin/python').rename(project / '.venv/bin/saved-python')
    result = run(project, '--check')
    assert result.returncode == 1
    assert 'preserved' in result.stderr
    assert (project / '.venv/bin/saved-python').exists()


def test_install_failure_has_actionable_error_and_does_not_launch(project):
    (project / 'install-fails').touch()
    result = run(project)
    assert result.returncode == 1
    assert 'Dependency installation failed' in result.stderr
    assert 'Manual repair:' in result.stderr
    assert not (project / 'station.json').exists()


def test_check_only_repairs_partial_dependencies_and_remains_offline_when_ready(project):
    result = run(project, '--install-dev', '--check')
    assert result.returncode == 0
    assert (project / 'dependencies-ready').exists()
    assert not (project / 'station.json').exists()
    (project / 'install-fails').touch()
    assert run(project, '--check').returncode == 0


def test_paths_arguments_exit_code_and_worker_cleanup(project, tmp_path):
    config = tmp_path / 'custom config.yaml'
    config.write_text('discovery: {}\n')
    (project / 'station-fails').touch()
    result = run(project, '--with-worker', '--name', '测试 Boss', '--config', config.name,
                 '--shared', 'shared folder', '--port', '04547', cwd=tmp_path)
    assert result.returncode == 23
    for role in ['station', 'worker']:
        args = json.loads((project / f'{role}.json').read_text())['args']
        assert args[args.index('--config')+1] == str(config)
        assert args[args.index('--shared')+1] == str(tmp_path / 'shared folder')
        assert args[args.index('--name')+1] == ('测试 Boss' if role == 'station' else '测试 Boss-Worker')
    assert (project / 'worker.stopped').exists()


def test_worker_failure_stops_owned_station(project):
    (project / 'station-waits').touch()
    (project / 'worker-fails').touch()
    result = run(project, '--with-worker')
    assert result.returncode == 1
    assert 'Local Worker exited' in result.stderr
    assert (project / 'station.stopped').exists()


def test_termination_cleans_up_both_owned_processes(project):
    (project / 'station-waits').touch()
    proc = subprocess.Popen(command(project, '--with-worker'), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 5
        while not ((project / 'station.json').exists() and (project / 'worker.json').exists()):
            assert time.monotonic() < deadline
            time.sleep(.02)
        proc.send_signal(signal.SIGTERM)
        proc.communicate(timeout=5)
        assert proc.returncode == 143
        assert (project / 'station.stopped').exists()
        assert (project / 'worker.stopped').exists()
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.communicate(timeout=5)


def test_dependency_check_detects_other_missing_and_wrong_version(monkeypatch):
    from importlib.metadata import PackageNotFoundError

    monkeypatch.setattr(check_environment, 'requirements', lambda _: ['fastapi>=0.104', 'pydantic>=2', 'cryptography>=41'])
    def version(name):
        if name == 'cryptography':
            raise PackageNotFoundError(name)
        return {'fastapi': '0.104', 'pydantic': '1.10'}[name]
    monkeypatch.setattr(check_environment.importlib.metadata, 'version', version)
    errors = check_environment.check_environment()
    assert len(errors) == 2
    assert any('pydantic' in e for e in errors)
    assert any('cryptography' in e for e in errors)
