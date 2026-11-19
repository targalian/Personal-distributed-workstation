#!/usr/bin/env python3
"""Standard-library read-only environment diagnosis; never starts services or CLIs."""
import argparse
import ast
import importlib.metadata
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def git(*args: str) -> str:
    """Read local Git metadata without contacting remotes."""
    try:
        return subprocess.run(['git', '-C', str(ROOT), *args], capture_output=True,
                              text=True, timeout=10, check=True).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ''


def check_constraint(requirement: str, version: str | None) -> str:
    """Validate the numeric release constraints declared by this repository."""
    if version is None:
        return 'missing'
    if not re.fullmatch(r'\d+(?:\.\d+)*', version):
        return 'unknown'
    actual = tuple(map(int, version.split('.')))
    rule = r'(?:>=|<=|==|!=|>|<)\s*\d+(?:\.\d+)*'
    if not re.fullmatch(rf'(?:[A-Za-z0-9_.-]+(?:\[[A-Za-z0-9_,.-]+\])?)?\s*{rule}(?:\s*,\s*{rule})*', requirement.strip()):
        return 'unknown'
    specs = re.findall(r'(>=|<=|==|!=|>|<)\s*([0-9.]+)', requirement)
    if not specs:
        return 'unknown'
    for operator, bound in specs:
        expected = tuple(map(int, bound.split('.')))
        width = max(len(actual), len(expected))
        left = actual + (0,) * (width - len(actual))
        right = expected + (0,) * (width - len(expected))
        valid = {'>=': left >= right, '<=': left <= right, '==': left == right,
                 '!=': left != right, '>': left > right, '<': left < right}[operator]
        if not valid:
            return 'incompatible'
    return 'compatible'


def diagnose(profile: str, isolated_dir: str | None = None) -> dict:
    """Return observations without reading configuration or user data."""
    try:
        source = (ROOT / 'pyproject.toml').read_text(encoding='utf-8')
        project = re.search(r'^\[project\]$(.*?)(?=^\[|\Z)', source, re.M | re.S)
        if not project:
            raise ValueError('Missing project declaration')
        required = ast.literal_eval(re.search(r'^requires-python\s*=\s*(.+)$', project[1], re.M)[1])
        requirements = ast.literal_eval(re.search(r'^dependencies\s*=\s*(\[.*?^\])', project[1], re.M | re.S)[1])
        if not isinstance(required, str) or not isinstance(requirements, list) or not all(isinstance(r, str) for r in requirements):
            raise ValueError('Invalid project declarations')
        metadata = json.loads((ROOT / 'VERSION.json').read_text(encoding='utf-8-sig'))
        if not isinstance(metadata, dict):
            raise ValueError('Invalid VERSION object')
    except (OSError, ValueError, SyntaxError, TypeError) as exc:
        return {'errors': [f'Cannot read declarations: {exc}'], 'ready_for_further_checks': False}
    dependencies = []
    for requirement in requirements:
        name = re.split(r'[\[<>=!~; ]', requirement)[0]
        try:
            version = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            version = None
        dependencies.append({'requirement': requirement, 'installed_version': version,
                             'constraint_validation': check_constraint(requirement, version)})
    commit = str(metadata.get('commit', ''))
    head = git('rev-parse', 'HEAD')
    recorded = git('rev-parse', '--verify', commit + '^{commit}') if commit else ''
    changed = git('diff', '--name-only', recorded, head) if recorded and head else ''
    relation = 'unknown'
    if recorded and head:
        relation = 'exact' if recorded == head else 'different'
        if (recorded != head and changed == 'VERSION.json'
                and git('show', '-s', '--format=%P', head) == recorded):
            relation = 'expected_sync'
    disabled = {v.strip().lower() for v in os.environ.get('CLI_AGENT_DISABLED_BACKENDS', '').split(',')}
    cli = {name: {'path': shutil.which(name), 'disabled': name in disabled,
                  'login': 'unverified'} for name in ('codex', 'claude', 'aider')}
    node = shutil.which('node')
    node_version = None
    if node:
        try:
            node_version = subprocess.run([node, '--version'], capture_output=True,
                                          text=True, timeout=10, check=True).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
    path = Path(isolated_dir).absolute() if isolated_dir else None
    isolation = {'declared': path is not None, 'path': str(path) if path else None,
                 'exists': path.exists() if path else None,
                 'writable': os.access(path, os.W_OK) if path else None}
    isolation['available'] = bool(path and path.is_dir() and isolation['writable'])
    isolation['status'] = ('not_declared' if path is None else
                           'passed' if isolation['available'] else 'failed')
    python_version = '.'.join(map(str, sys.version_info[:3]))
    python_status = check_constraint(required, python_version)
    node_status = check_constraint('>=20', node_version.removeprefix('v') if node_version else None)
    if node and node_status == 'missing':
        node_status = 'unknown'
    ready = python_status == 'compatible' and all(d['constraint_validation'] == 'compatible' for d in dependencies)
    if profile == 'frontend':
        ready = ready and node_status == 'compatible'
    return {'python': {'executable': sys.executable, 'version': sys.version.split()[0],
                       'required': required, 'compatible': python_status == 'compatible'},
            'dependencies': dependencies, 'node': {'path': node, 'version': node_version,
            'required': profile == 'frontend', 'constraint_validation': node_status}, 'cli': cli, 'isolation': isolation,
            'release': {'version': metadata.get('version'), 'commit': commit,
                        'head': head, 'relation': relation}, 'profile': profile,
            'environment_runnable': bool(ready),
            'ready_for_further_checks': bool(ready and (path is None or isolation['available'])),
            'limitations': ['Only numeric release constraints are supported; other versions are unknown.',
                            'CLI authentication and physical nodes are unverified.']}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile', choices=['backend', 'frontend'], default='backend')
    parser.add_argument('--json', action='store_true')
    parser.add_argument('--isolated-dir')
    args = parser.parse_args()
    report = diagnose(args.profile, args.isolated_dir)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report['ready_for_further_checks'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
