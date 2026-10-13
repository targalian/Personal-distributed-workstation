"""Verify local-loop failure propagation, environment isolation and offline reports."""
import json
import os
import subprocess
import sys
from pathlib import Path

from test_bug import local_checks


def test_syntax_check_reports_invalid_file(tmp_path):
    (tmp_path / 'main.py').write_text('def broken(:\n')
    result = local_checks.check_syntax(tmp_path)
    assert result['status'] == 'failed'
    assert 'main.py' in result['output']


def test_loop_state_rejects_broken_history(tmp_path):
    path = tmp_path / 'loop_status.json'
    path.write_text('{"notes": "route "/" was added"}')
    assert local_checks.check_loop_state(tmp_path)['status'] == 'failed'
    path.write_text(json.dumps({'notes': 'route "/" was added'}))
    assert local_checks.check_loop_state(tmp_path)['status'] == 'passed'


def test_command_preserves_exit_status_and_sanitizes_python_environment(tmp_path, monkeypatch):
    monkeypatch.setenv('PYTHONPATH', '/unrelated/ros')
    monkeypatch.setenv('PYTHONHOME', '/invalid')
    code = "import os,sys; assert 'PYTHONPATH' not in os.environ; assert 'PYTHONHOME' not in os.environ; print('child ran'); sys.exit(7)"
    result = local_checks.check_command('failing', [sys.executable, '-c', code], tmp_path, 10)
    assert result['status'] == 'failed'
    assert result['returncode'] == 7
    assert 'child ran' in result['output']
    assert os.environ['PYTHONPATH'] == '/unrelated/ros'


def test_timeout_is_failure(tmp_path):
    result = local_checks.check_command('slow', [sys.executable, '-c', 'import time; time.sleep(10)'], tmp_path, .1)
    assert result['status'] == 'failed'
    assert 'exceeded' in result['output']


def test_local_cycle_has_no_api_access_and_does_not_report_failures_as_passed(tmp_path, monkeypatch):
    (tmp_path / 'main.py').write_text('pass\n')
    (tmp_path / 'loop_status.json').write_text('{"iteration_count": 99}')
    calls = []
    def check(name, cmd, root, timeout):
        calls.append(cmd)
        return {'name': name, 'status': 'failed' if name == 'unit_tests' else 'passed', 'output': 'result'}
    monkeypatch.setattr(local_checks, 'check_command', check)
    monkeypatch.setattr(local_checks.shutil, 'which', lambda _: None)
    assert not local_checks.run_local_checks(root=tmp_path)
    report = json.loads((tmp_path / '.lan_mesh/local-checks.json').read_text())
    assert report['status'] == 'failed'
    assert len([c for c in report['checks'] if c['status'] == 'skipped']) == 2
    assert all('api_tests' not in ' '.join(c) for c in calls)
    assert json.loads((tmp_path / 'loop_status.json').read_text()) == {'iteration_count': 99}


def test_dev_loop_rejects_local_online_combination():
    root = Path(__file__).resolve().parent.parent
    env = os.environ.copy()
    env.pop('PYTHONPATH', None)
    env.pop('PYTHONHOME', None)
    result = subprocess.run([sys.executable, 'test_bug/dev_loop.py', '--local', '--plan'],
                            cwd=root, env=env, text=True, capture_output=True, timeout=10)
    assert result.returncode == 2
    assert '--local' in result.stderr
