"""Regression tests for Windows batch control flow and upgraded runtime dependencies."""

import os
from pathlib import Path
import subprocess

import pytest

from scripts import check_runtime_dependencies as preflight


ROOT = Path(__file__).resolve().parents[1]
WINDOWS_ONLY = pytest.mark.skipif(os.name != 'nt', reason='Requires Windows cmd.exe')


def test_preflight_detects_new_dependencies_even_when_fastapi_exists(tmp_path, monkeypatch):
    requirements = tmp_path / 'requirements.txt'
    requirements.write_text('fastapi==0.128.8\nreaderwriterlock==1.0.9\n', encoding='utf-8')

    def installed(name):
        if name == 'fastapi':
            return '0.138.1'
        raise preflight.PackageNotFoundError(name)

    monkeypatch.setattr(preflight, 'version', installed)
    problems = preflight.check_requirements(requirements)
    assert any('readerwriterlock' in problem and 'not installed' in problem for problem in problems)
    assert any('fastapi' in problem and '0.128.8' in problem for problem in problems)


def test_preflight_accepts_compatible_versions_and_skips_inactive_markers(tmp_path, monkeypatch):
    requirements = tmp_path / 'requirements.txt'
    requirements.write_text('# generated runtime requirements\nfastapi==0.128.8\n'
                            'readerwriterlock==1.0.9\nnever-installed; python_version < "0"\n',
                            encoding='utf-8')
    versions = {'fastapi': '0.128.8', 'readerwriterlock': '1.0.9'}
    monkeypatch.setattr(preflight, 'version', lambda name: versions[name])
    assert preflight.check_requirements(requirements) == []


@WINDOWS_ONLY
@pytest.mark.parametrize('probe_exit', [0, 1])
def test_npm_cmd_probes_return_to_startup_before_station_launch(tmp_path, probe_exit):
    source = (ROOT / 'scripts/start_workstation.bat').read_text(encoding='utf-8')
    section = source[source.index('REM -- CLI Agent probes:'):source.index('REM -- Step 6:')]
    npm = tmp_path / 'npm'
    npm.mkdir()
    for name in ('claude', 'codex'):
        (npm / f'{name}.cmd').write_text(
            f'@echo off\necho {name}>>"%STARTUP_PROBE_MARKER%"\nexit /b {probe_exit}\n',
            encoding='ascii')
    script = tmp_path / 'probe.bat'
    script.write_text('@echo off\n' + section + '\necho REACHED_STATION_LAUNCH\n', encoding='utf-8')
    marker = tmp_path / 'called.txt'
    env = {**os.environ, 'APPDATA': str(tmp_path), 'STARTUP_PROBE_MARKER': str(marker)}
    result = subprocess.run(['cmd.exe', '/d', '/c', str(script)], env=env,
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'REACHED_STATION_LAUNCH' in result.stdout
    assert marker.read_text().splitlines() == ['claude', 'codex']


@WINDOWS_ONLY
@pytest.mark.parametrize('body', ['exit /b 7', 'if ('])
def test_outer_console_guard_preserves_child_errors(tmp_path, body):
    source = (ROOT / 'scripts/start_workstation.bat').read_text(encoding='utf-8')
    wrapper = source[:source.index('\n:run\n')]
    directory = tmp_path / 'path with spaces (startup)'
    directory.mkdir()
    script = directory / 'start test.bat'
    script.write_text(wrapper + '\n:run\n' + body + '\n', encoding='utf-8')
    env = {**os.environ, 'LANMESH_NO_PAUSE': '1'}
    result = subprocess.run(['cmd.exe', '/d', '/c', str(script)], env=env,
                            capture_output=True, text=True, timeout=15)
    assert result.returncode != 0
    assert '[ERROR] Workstation startup failed' in result.stdout
    if body == 'exit /b 7':
        assert result.returncode == 7
