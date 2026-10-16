"""Verify real launcher HTTP readiness and shutdown using disposable source and data."""
import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def free_port(kind: int = socket.SOCK_STREAM) -> int:
    with socket.socket(socket.AF_INET, kind) as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def run_smoke(venv: Path, output: Path) -> None:
    """Start only disposable processes; preserve service logs outside the temporary tree."""
    if sys.platform != 'linux':
        raise ValueError('This release smoke check requires Linux')
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='workstation smoke ') as folder:
        directory = Path(folder)
        project = directory / 'project'
        project.mkdir()
        shutil.copytree(ROOT / 'lan_mesh', project / 'lan_mesh', ignore=shutil.ignore_patterns(
            '__pycache__', '*.pyc', '.env*', 'model_pool.yaml', 'resources.yaml', '*.sqlite3', '*.log'))
        (project / 'scripts').mkdir()
        for name in ('start_workstation.sh', 'check_environment.py'):
            shutil.copy2(ROOT / 'scripts' / name, project / 'scripts' / name)
        for name in ('main.py', 'requirements.txt', 'requirements-dev.txt', 'VERSION.json'):
            shutil.copy2(ROOT / name, project / name)
        station_port, worker_port = free_port(), free_port()
        while station_port == worker_port:
            worker_port = free_port()
        config = directory / 'config.json'
        config.write_text(json.dumps({'auto_upgrade': False,
            'discovery': {'port': free_port(socket.SOCK_DGRAM)},
            'secretary': {'db_path': str(directory / 'data' / 'station.sqlite3')},
            'security': {'auth_enabled': False}}))
        env = os.environ.copy()
        for name in ('PYTHONPATH', 'PYTHONHOME', 'LAN_MESH_CONFIG', 'LAN_MESH_MODEL_POOL',
                     'LAN_MESH_RESOURCES', 'LAN_MESH_API_KEY'):
            env.pop(name, None)
        env.update(LAN_MESH_DATA_DIR=str(directory / 'data'),
                   LAN_MESH_LOG_FILE=str(output / 'service.log'), PYTHONNOUSERSITE='1')
        command = ['bash', str(project / 'scripts/start_workstation.sh'), '--venv', str(venv),
                   '--with-worker', '--port', str(station_port), '--worker-port', str(worker_port),
                   '--config', str(config), '--shared', str(directory / 'shared'), '--name', 'ReleaseSmoke']
        with (output / 'launcher.log').open('w') as log:
            proc = subprocess.Popen(command, cwd=project, env=env, stdout=log, stderr=subprocess.STDOUT,
                                    start_new_session=True)
            try:
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                for port, path in ((station_port, '/api/health'), (station_port, '/'), (worker_port, '/info')):
                    deadline = time.monotonic() + 40
                    while True:
                        if proc.poll() is not None:
                            raise RuntimeError(f'Launcher exited with {proc.returncode}; see launcher.log')
                        try:
                            with opener.open(f'http://127.0.0.1:{port}{path}', timeout=1) as response:
                                if response.status != 200 or (path == '/' and b'<html' not in response.read().lower()):
                                    raise RuntimeError(f'Unexpected HTTP content: {path}')
                            break
                        except OSError:
                            if time.monotonic() >= deadline:
                                raise RuntimeError(f'HTTP readiness timeout: {path}')
                            time.sleep(.2)
                    print(f'[Smoke] PASS HTTP 200 {path}', flush=True)
                proc.send_signal(signal.SIGTERM)
                proc.wait(timeout=15)
                if proc.returncode != 143:
                    raise RuntimeError(f'Unexpected launcher exit: {proc.returncode}')
                for port in (station_port, worker_port):
                    with socket.socket() as sock:
                        if sock.connect_ex(('127.0.0.1', port)) == 0:
                            raise RuntimeError(f'Port remains open: {port}')
                print('[Smoke] PASS TERM 143 and both ports released', flush=True)
            finally:
                # The group belongs to this invocation, including children of a failed launcher.
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait(timeout=5)
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                logs = project / '.lan_mesh' / 'logs'
                if logs.exists():
                    shutil.copytree(logs, output / 'worker-logs', dirs_exist_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--venv', type=Path, default=Path(sys.prefix))
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    run_smoke(args.venv.resolve(), args.output.resolve())
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
