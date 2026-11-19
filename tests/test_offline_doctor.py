"""Offline diagnosis regression checks, without production configuration access."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace

from lan_mesh import preflight

spec = importlib.util.spec_from_file_location('doctor', Path(__file__).parents[1] / 'scripts/offline_doctor.py')
doctor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(doctor)


def test_declared_dependency_constraints():
    assert doctor.check_constraint('fastapi==0.128.8', '0.128.8') == 'compatible'
    assert doctor.check_constraint('fastapi==0.128.8', '0.129.0') == 'incompatible'
    assert doctor.check_constraint('uvicorn[standard]>=0.24.0,<1.0', '1.0') == 'incompatible'
    assert doctor.check_constraint('foo>=1', None) == 'missing'
    assert doctor.check_constraint('foo>=1', '1.0rc1') == 'unknown'


def test_backend_does_not_require_node(monkeypatch):
    monkeypatch.setattr(doctor.shutil, 'which', lambda name: None)
    monkeypatch.setattr(doctor.importlib.metadata, 'version', lambda name: {
        'fastapi': '0.128.8', 'uvicorn': '0.30', 'websockets': '13',
        'psutil': '6', 'pydantic': '2.10', 'PyYAML': '6', 'requests': '2.32',
        'python-multipart': '0.0.9', 'python-dotenv': '1', 'boto3': '1.30',
        'cryptography': '42', 'readerwriterlock': '1.0.9'}[name])
    assert doctor.diagnose('backend')['ready_for_further_checks']
    assert not doctor.diagnose('frontend')['ready_for_further_checks']


def test_python310_rejected(monkeypatch):
    class Version(tuple):
        major, minor, micro = 3, 10, 12
    monkeypatch.setattr(preflight.sys, 'version_info', Version((3, 10, 12)))
    result = preflight._check_python()
    assert not result.passed


import json
import pytest


@pytest.mark.parametrize('requirement', [
    'foo>=1; python_version>"3"', 'foo~=1.2', 'foo>=1,~=2',
    'foo>=1 trailing', 'foo==1.*', 'foo>=1..2', 'foo===1',
])
def test_unknown_syntax(requirement):
    assert doctor.check_constraint(requirement, '2') == 'unknown'


def test_cli_normalization(monkeypatch):
    monkeypatch.setenv('CLI_AGENT_DISABLED_BACKENDS', ' Claude , AIDER ')
    result = doctor.diagnose('backend')['cli']
    assert set(result) == {'codex', 'claude', 'aider'}
    assert result['claude']['disabled'] and result['aider']['disabled']
    assert all(item['login'] == 'unverified' for item in result.values())


@pytest.mark.parametrize('output', ['unknown', '', 'v20.x', 'v19.0.0'])
def test_node_output_json(monkeypatch, capsys, output):
    monkeypatch.setattr(doctor.shutil, 'which', lambda name: '/node' if name == 'node' else None)
    monkeypatch.setattr(doctor, 'git', lambda *args: '')
    monkeypatch.setattr(doctor.subprocess, 'run', lambda *a, **k: SimpleNamespace(stdout=output))
    monkeypatch.setattr(doctor.sys, 'argv', ['doctor', '--profile', 'frontend', '--json'])
    assert doctor.main() == 1
    report = json.loads(capsys.readouterr().out)
    assert report['node']['constraint_validation'] in {'unknown', 'incompatible'}


def test_declaration_error_json(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(doctor, 'ROOT', tmp_path)
    monkeypatch.setattr(doctor.sys, 'argv', ['doctor', '--json'])
    assert doctor.main() == 1
    assert json.loads(capsys.readouterr().out)['errors']


def test_python_requirement_from_declaration(monkeypatch, tmp_path):
    (tmp_path / 'pyproject.toml').write_text('[project]\nrequires-python = ">=99"\ndependencies = [\n]\n')
    (tmp_path / 'VERSION.json').write_text('{}')
    monkeypatch.setattr(doctor, 'ROOT', tmp_path)
    report = doctor.diagnose('backend')
    assert report['python']['required'] == '>=99'
    assert not report['python']['compatible']


@pytest.mark.parametrize('kind', ['missing', 'file', 'unwritable', 'directory'])
def test_isolation(monkeypatch, tmp_path, kind):
    monkeypatch.setattr(doctor, 'check_constraint', lambda *a: 'compatible')
    path = tmp_path / 'isolated'
    if kind == 'file':
        path.touch()
    elif kind in {'unwritable', 'directory'}:
        path.mkdir()
    if kind == 'unwritable':
        monkeypatch.setattr(doctor.os, 'access', lambda *a: False)
    report = doctor.diagnose('backend', str(path))
    assert report['environment_runnable']
    assert report['isolation']['available'] == (kind == 'directory')
    assert report['ready_for_further_checks'] == (kind == 'directory')


@pytest.mark.parametrize('parent,expected', [('recorded', 'expected_sync'),
                                                   ('intermediate', 'different'),
                                                   ('recorded other', 'different')])
def test_version_direct_parent_only(monkeypatch, parent, expected):
    def fake_git(*args):
        if args == ('rev-parse', 'HEAD'):
            return 'head'
        if args[0] == 'rev-parse':
            return 'recorded'
        if args[0] == 'diff':
            return 'VERSION.json'
        if args[0] == 'show':
            return parent
        return ''
    monkeypatch.setattr(doctor, 'git', fake_git)
    assert doctor.diagnose('backend')['release']['relation'] == expected
