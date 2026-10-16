"""Run the Linux release gates and preserve evidence without invoking real models."""
import argparse
import importlib.metadata
import json
import os
import platform
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.check_environment import requirements  # noqa: E402


def locked_environment_errors() -> list[str]:
    """Require the platform and exact package versions covered by this release lock."""
    from pip._vendor.packaging.requirements import Requirement
    errors = []
    if sys.platform != 'linux' or platform.machine() != 'x86_64' or sys.version_info[:2] != (3, 10):
        errors.append('Release lock requires Linux x86_64 and CPython 3.10')
    if sys.prefix == sys.base_prefix:
        errors.append('Run release checks from an isolated virtual environment')
    if not shutil.which('node'):
        errors.append('Node.js is required; JavaScript checks cannot be skipped')
    for raw in requirements(ROOT / 'requirements-release-linux-py310.lock'):
        req = Requirement(raw)
        try:
            installed = importlib.metadata.version(req.name)
        except importlib.metadata.PackageNotFoundError:
            errors.append(f'Missing locked dependency: {req.name}')
            continue
        if not req.specifier.contains(installed, prereleases=True):
            errors.append(f'Lock mismatch: {req.name} {installed}; expected {req.specifier}')
    return errors


def run_check(name: str, command: list[str], output: Path, env: dict, timeout: float) -> dict:
    """Capture logs and terminate the owned process group on timeout or interruption."""
    start = time.monotonic()
    log_path = output / f'{name}.log'
    with log_path.open('w') as log:
        process = subprocess.Popen(command, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
                                   start_new_session=True)
        try:
            code = process.wait(timeout=timeout)
            status = 'passed' if code == 0 else 'failed'
        except subprocess.TimeoutExpired:
            status, code = 'timed_out', None
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGINT)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=5)
            # A gate can exit while descendants remain alive in its process group.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    return dict(name=name, status=status, returncode=code,
                seconds=round(time.monotonic() - start, 3), log=log_path.name)


def run_release_checks(output: Path) -> bool:
    """Stop on the first failing gate; a skipped or unfinished gate cannot pass release."""
    output.mkdir(parents=True, exist_ok=False)
    report = {'format': 1, 'status': 'running', 'started_at': datetime.now(timezone.utc).isoformat(),
              'scope': 'Linux source installation; simulated models; no physical multi-host or 72-hour acceptance',
              'python': platform.python_version(), 'checks': [], 'errors': []}
    def save():
        (output / 'release.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    save()
    try:
        report['commit'] = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
        report['dirty'] = bool(subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT, text=True).strip())
        report['errors'] = locked_environment_errors()
        if report['errors']:
            report['status'] = 'failed'
            return False
        with tempfile.TemporaryDirectory(prefix='workstation-release-data-') as directory:
            env = os.environ.copy()
            for name in ('PYTHONPATH', 'PYTHONHOME', 'LAN_MESH_LOG_FILE'):
                env.pop(name, None)
            env.update(LAN_MESH_DATA_DIR=directory, PYTHONNOUSERSITE='1', PYTEST_DISABLE_PLUGIN_AUTOLOAD='1',
                       BRIEF_BROWSER_OUTPUT=str(output / 'screenshots'))
            commands = [
                ('dependencies', [sys.executable, '-m', 'pip', 'check'], 30),
                ('imports', [sys.executable, 'scripts/check_environment.py', '--dev'], 30),
                ('local', [sys.executable, 'test_bug/local_checks.py', '--timeout', '600',
                           '--report', str(output / 'local-checks.json')], 700),
                ('browser', [sys.executable, 'test_bug/project_brief_browser.py'], 240),
                ('startup', [sys.executable, 'scripts/startup_smoke.py', '--venv', sys.prefix,
                             '--output', str(output / 'startup')], 150),
            ]
            for name, command, timeout in commands:
                print(f'[Release] Running {name}...', flush=True)
                result = run_check(name, command, output, env, timeout)
                report['checks'].append(result)
                save()
                if result['status'] != 'passed':
                    report['status'] = 'failed'
                    return False
                if name == 'local':
                    local = json.loads((output / 'local-checks.json').read_text())
                    if local.get('status') != 'passed' or any(c.get('status') != 'passed' for c in local['checks']):
                        report['errors'].append('Local checks contain failed or skipped gates')
                        report['status'] = 'failed'
                        return False
            report['status'] = 'passed'
            return True
    except BaseException as exc:
        report['status'] = 'interrupted' if isinstance(exc, (KeyboardInterrupt, SystemExit)) else 'failed'
        report['errors'].append(f'{type(exc).__name__}: {exc}')
        raise
    finally:
        report['finished_at'] = datetime.now(timezone.utc).isoformat()
        save()
        print(f'[Release] {report["status"]}: {output / "release.json"}', flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True, help='New output directory; existing reports are preserved')
    args = parser.parse_args()
    return 0 if run_release_checks(args.output.resolve()) else 1


if __name__ == '__main__':
    raise SystemExit(main())
