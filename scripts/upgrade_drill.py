"""Exercise a pinned older checkout, current source and offline rollback in disposable storage."""
import argparse
import hashlib
import io
import json
import os
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from scripts.check_environment import requirements  # noqa: E402
from scripts.startup_smoke import free_port  # noqa: E402

BASELINE = '2141a62b1d54d068981ca76988e683f82f07313b'  # v0.34.18, immutable acceptance baseline
SOURCE_PATHS = ['lan_mesh', 'main.py', 'VERSION.json', 'requirements.txt', 'requirements-dev.txt',
                'requirements-linux-py310.lock', 'scripts/start_workstation.sh',
                'scripts/check_environment.py', 'scripts/backup_workstation.py']
PRIVATE_NAMES = {'model_pool.yaml', 'resources.yaml'}
SEED = r'''
import hashlib,json,sys
from pathlib import Path
from types import SimpleNamespace
from lan_mesh.database import Database
from lan_mesh.chat_handler import ChatHandler
from lan_mesh.protocol import Task
from lan_mesh.execution_receipts import ExecutionReceipts
state=Path(sys.argv[1])
db=Database(str(state/'secretary.sqlite3'))
controller=SimpleNamespace(db=db)
chat=ChatHandler(None,controller,db=db,shared_folder=SimpleNamespace(path=state/'shared'))
cid=chat._active_conv_id
doc={'title':'Upgrade acceptance','sections':{'background':'Preserve reviewed requirements'},
     'nodes':[{'id':'module','title':'Module'}, {'id':'feature','title':'Feature','parent_id':'module'},
              {'id':'detail','title':'Detail','parent_id':'feature'}], 'edges':[]}
chat.briefs.save(cid,doc,0)
chat.briefs.save(cid,{**doc,'title':'Reviewed baseline'},1)
chat.briefs.propose(cid,{**doc,'title':'Unreviewed proposal'},2)
def submit(**kw):
    task=Task(task_id=kw['task_id'],name=kw['name'],description=kw['description'],
              input_data=kw['input_data'],pm_agent_id='drill-pm',status='running')
    db.save_task(task)
    return task.to_dict()
controller.submit_task_from_chat=submit
handoff=chat.brief_handoffs.dispatch(cid,2)
db.record_task_delivery(handoff['task_id'],'drill-pm',{'deliverable':'Reviewed artifact','summary':'Fixture only'})
db.review_task_delivery(handoff['task_id'],True)
payload={'subtask_id':'drill-done','name':'Stored execution','input_data':{}}
receipts=ExecutionReceipts(state/'secretary-executions.sqlite3',owner='fixture-process')
receipts.claim(payload)
receipts.finish('drill-done',{'status':'completed','output':{'summary':'Original output'}})
receipts.claim({**payload,'subtask_id':'drill-unknown'})
artifacts=state/'shared/deliverables'
artifacts.mkdir(parents=True,exist_ok=True)
(artifacts/'项目描述.md').write_text(chat.briefs.get(cid)['markdown'])
(artifacts/'交付.md').write_text('Reviewed fixture artifact; no model was called.\n')
hashes={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in artifacts.iterdir()}
Path(sys.argv[2]).write_text(json.dumps({'cid':cid,'task_id':handoff['task_id'],'artifacts':hashes}))
db._get_conn().close()
'''
VERIFY_RECEIPTS = '''
import sys
from pathlib import Path
from lan_mesh.execution_receipts import ExecutionReceipts
store=ExecutionReceipts(Path(sys.argv[1])/'secretary-executions.sqlite3')
for execution_id,status in [('drill-done','completed'),('drill-unknown','unknown')]:
    claimed,result=store.claim({'subtask_id':execution_id,'name':'Stored execution','input_data':{}})
    assert not claimed and result['status']==status, 'Duplicate execution became eligible to run'
'''


def isolated_env(state: Path, log: Path) -> dict:
    """Pass only OS basics; do not inherit model keys, resource paths or cloud/Bot settings."""
    env = {key: os.environ[key] for key in ('LANG', 'LC_ALL', 'TZ', 'SYSTEMROOT') if key in os.environ}
    env.update(PATH=os.defpath, LAN_MESH_DATA_DIR=str(state), LAN_MESH_LOG_FILE=str(log),
               PYTHONNOUSERSITE='1', PYTHONUNBUFFERED='1')
    return env


def private(path: PurePosixPath) -> bool:
    return any(p.startswith('.env') or p in PRIVATE_NAMES or p == '__pycache__' or
               p.endswith(('.pyc', '.sqlite3', '.log')) for p in path.parts)


def export_baseline(ref: str, destination: Path) -> str:
    """Export only application files from a local commit, rejecting links and unsafe members."""
    commit = subprocess.check_output(['git', 'rev-parse', '--verify', '--end-of-options', ref + '^{commit}'],
                                     cwd=ROOT, text=True).strip()
    subprocess.run(['git', 'merge-base', '--is-ancestor', commit, 'HEAD'], cwd=ROOT, check=True)
    archive = subprocess.check_output(['git', 'archive', '--format=tar', commit, '--', *SOURCE_PATHS], cwd=ROOT)
    destination.mkdir()
    with tarfile.open(fileobj=io.BytesIO(archive)) as bundle:
        for member in bundle:
            path = PurePosixPath(member.name)
            if path.is_absolute() or '..' in path.parts or not (member.isdir() or member.isfile()):
                raise ValueError('Baseline archive contains an unsafe path or file type')
            if private(path):
                continue
            target = destination.joinpath(*path.parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with bundle.extractfile(member) as source, target.open('xb') as output:
                    shutil.copyfileobj(source, output)
    return commit


def copy_candidate(destination: Path) -> None:
    """Copy the current worktree application while excluding personal runtime files."""
    def ignore(directory, names):
        excluded = []
        for name in names:
            if private(PurePosixPath(name)):
                excluded.append(name)
            elif (Path(directory) / name).is_symlink():
                raise ValueError('Candidate source contains a symbolic link')
        return excluded
    destination.mkdir()
    for name in SOURCE_PATHS:
        source, target = ROOT / name, destination / name
        if source.is_symlink():
            raise ValueError('Candidate source contains a symbolic link')
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, target, ignore=ignore)
        else:
            shutil.copy2(source, target)


def check_runtime(project: Path, venv: Path, env: dict) -> dict:
    """Require the selected interpreter to satisfy this source revision's exact runtime lock."""
    if not (project / 'lan_mesh/data_paths.py').is_file():
        raise ValueError('Baseline must support LAN_MESH_DATA_DIR; HOME overrides are not supported')
    locked = requirements(project / 'requirements-linux-py310.lock')
    code = '''
import importlib.metadata,json,sys
from pip._vendor.packaging.requirements import Requirement
assert sys.prefix != sys.base_prefix, 'Use a virtual environment'
assert sys.version_info[:2] == (3,10), 'Runtime lock requires Python 3.10'
for raw in json.loads(sys.argv[1]):
    req=Requirement(raw)
    version=importlib.metadata.version(req.name)
    assert req.specifier.contains(version, prereleases=True), str(req)
print(json.dumps({'python':sys.version.split()[0], 'prefix':sys.prefix}))
'''
    python = str(venv / 'bin/python')
    subprocess.run([python, '-m', 'pip', 'check'], cwd=project, env=env,
                   capture_output=True, check=True, timeout=30)
    subprocess.run([python, 'scripts/check_environment.py'], cwd=project, env=env,
                   capture_output=True, check=True, timeout=30)
    result = json.loads(subprocess.check_output([python, '-c', code, json.dumps(locked)],
                                               cwd=project, env=env, text=True, timeout=30))
    result['lock_sha256'] = hashlib.sha256((project / 'requirements-linux-py310.lock').read_bytes()).hexdigest()
    return result


def request(port: int, path: str, payload=None, method=None):
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(f'http://127.0.0.1:{port}{path}', data=data, method=method,
                                 headers={'Content-Type': 'application/json'})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=5) as response:
        return json.load(response)


def stop(proc: subprocess.Popen, timeout: float = 10) -> None:
    """Stop this invocation's process group, including a launcher that already exited."""
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    expired = False
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        expired = True
    finally:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait(timeout=5)
    if expired:
        raise RuntimeError('Station shutdown timed out; process group was forcibly cleaned up')


@contextmanager
def station(project: Path, venv: Path, state: Path, output: Path, label: str, reject: bool = False):
    """Use the real source launcher; require readiness or a specific schema refusal."""
    port = free_port()
    env = isolated_env(state, output / f'{label}-service.log')
    log_path = output / f'{label}-launcher.log'
    command = ['bash', str(project / 'scripts/start_workstation.sh'), '--venv', str(venv),
               '--port', str(port), '--config', str(state / 'config.yaml')]
    with log_path.open('w') as log:
        proc = subprocess.Popen(command, cwd=project, env=env, stdout=log, stderr=subprocess.STDOUT,
                                start_new_session=True)
    try:
        if reject:
            code = proc.wait(timeout=30)
            if code == 0 or 'Unsupported database schema v999' not in log_path.read_text():
                raise ValueError('Expected explicit refusal of unsupported schema v999')
            yield {'rejected': True, 'returncode': code}
        else:
            deadline = time.monotonic() + 40
            while True:
                if proc.poll() is not None:
                    raise RuntimeError(f'{label} launcher exited; see {log_path.name}')
                try:
                    health = request(port, '/api/health')
                    if not request(port, '/api/station/activate-secretary', {})['ok']:
                        raise ValueError('Secretary activation failed')
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise RuntimeError(f'{label} readiness timeout')
                    time.sleep(.1)
            expected = json.loads((project / 'VERSION.json').read_text())
            version = request(port, '/api/version')
            if any(version[key] != expected[key] for key in ('version', 'commit')):
                raise ValueError('HTTP version does not match the selected source')
            yield {'port': port, 'device_id': health['device_id'], 'version': version['version'],
                   'version_commit': version['commit']}
            if proc.poll() is not None:
                raise RuntimeError(f'{label} exited before controlled shutdown')
    finally:
        stop(proc)
        with socket.socket() as sock:
            if sock.connect_ex(('127.0.0.1', port)) == 0:
                raise RuntimeError(f'{label} left its HTTP port open')


def inspect(port: int, fixture: dict, state: Path) -> dict:
    """Read actual APIs for requirements, pending review, accepted delivery and execution evidence."""
    path = f'/api/conversations/{fixture["cid"]}/brief'
    brief = request(port, path)
    if brief['revision'] != 2 or brief['document']['title'] != 'Reviewed baseline' or not brief['proposal']:
        raise ValueError('Requirement revision or pending AI proposal was lost')
    if len(request(port, path + '/history')['revisions']) != 2 or len(brief['document']['nodes']) != 3:
        raise ValueError('Requirement history or nested graph was lost')
    handoff = request(port, path + '/handoffs/2')
    task = request(port, '/api/tasks/' + fixture['task_id'])
    if handoff['task_id'] != fixture['task_id'] or task['status'] != 'accepted' or not task['output_data']['_delivery']['accepted']:
        raise ValueError('Confirmed requirement/task/delivery association was lost')
    if task['input_data']['_brief']['revision'] != 2:
        raise ValueError('Task requirement snapshot changed')
    for execution_id, status in (('drill-done', 'completed'), ('drill-unknown', 'unknown')):
        receipt = request(port, '/tasks/executions/' + execution_id)
        if receipt['status'] != status:
            raise ValueError('Execution receipt changed across restart')
    for name, expected in fixture['artifacts'].items():
        if hashlib.sha256((state / 'shared/deliverables' / name).read_bytes()).hexdigest() != expected:
            raise ValueError('Delivered file content changed across upgrade or rollback')
    return brief['document']


def database_digest(state: Path) -> str:
    """Compare logical SQLite content across a refused startup."""
    with closing(sqlite3.connect(state / 'secretary.sqlite3')) as conn:
        return hashlib.sha256('\n'.join(conn.iterdump()).encode()).hexdigest()


def command(project: Path, venv: Path, state: Path, output: Path, name: str, args: list[str]) -> None:
    with (output / f'{name}.log').open('w') as log:
        try:
            subprocess.run([str(venv / 'bin/python'), *args], cwd=project,
                           env=isolated_env(state, output / f'{name}-service.log'),
                           stdout=log, stderr=subprocess.STDOUT, check=True, timeout=60)
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(f'{name} exited {exc.returncode}; see {name}.log') from None


def run_drill(output: Path, baseline_ref: str, venv: Path, baseline_venv: Path) -> bool:
    """Run the upgrade and rollback sequence, preserving failure evidence in a new directory."""
    output.mkdir(parents=True, exist_ok=False)
    report = {'status': 'running', 'started_at': datetime.now(timezone.utc).isoformat(), 'steps': [],
              'scope': 'Linux source upgrade; fixture data; no live models, physical multi-host or production switch'}
    def save():
        (output / 'upgrade.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    def passed(name, **details):
        report['steps'].append({'name': name, 'status': 'passed', **details})
        save()
        print(f'[Upgrade] PASS {name}', flush=True)
    def begin(name):
        report['stage'] = name
        save()
    save()
    try:
        if sys.platform != 'linux':
            raise ValueError('The upgrade drill currently requires Linux')
        report['candidate_commit'] = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
        report['candidate_dirty'] = bool(subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT, text=True).strip())
        with tempfile.TemporaryDirectory(prefix='workstation-upgrade-') as folder:
            begin('sources_and_runtimes')
            root = Path(folder)
            baseline, candidate, state = root / 'baseline', root / 'candidate', root / 'state'
            report['baseline_commit'] = export_baseline(baseline_ref, baseline)
            copy_candidate(candidate)
            state.mkdir()
            for source in (baseline, candidate):
                (source / 'lan_mesh/model_pool.yaml').write_text('models: []\n')
            env = isolated_env(state, output / 'prepare-service.log')
            passed('sources_and_runtimes', baseline_runtime=check_runtime(baseline, baseline_venv, env),
                   candidate_runtime=check_runtime(candidate, venv, env), shared_venv=venv == baseline_venv)
            (state / 'config.yaml').write_text(json.dumps({'auto_upgrade': False,
                'discovery': {'port': free_port(socket.SOCK_DGRAM)}, 'security': {'auth_enabled': False}}))
            fixture_file = output / 'fixture.json'
            begin('seed')
            command(baseline, baseline_venv, state, output, 'seed', ['-c', SEED, str(state), str(fixture_file)])
            fixture = json.loads(fixture_file.read_text())
            begin('baseline')
            with station(baseline, baseline_venv, state, output, 'baseline') as before:
                inspect(before['port'], fixture, state)
            command(baseline, baseline_venv, state, output, 'baseline-receipts', ['-c', VERIFY_RECEIPTS, str(state)])
            passed('baseline', **before)
            begin('offline_backup')
            archive = output / 'before-upgrade.zip'
            command(baseline, baseline_venv, state, output, 'backup', ['scripts/backup_workstation.py',
                    'create', str(archive), '--config', str(state / 'config.yaml'), '--offline-confirmed'])
            command(baseline, baseline_venv, state, output, 'verify', ['scripts/backup_workstation.py', 'verify', str(archive)])
            passed('offline_backup', archive=archive.name)
            begin('upgrade_and_edit')
            with station(candidate, venv, state, output, 'upgraded') as upgraded:
                document = inspect(upgraded['port'], fixture, state)
                if upgraded['device_id'] != before['device_id']:
                    raise ValueError('Upgrade changed the node identity')
                reply = request(upgraded['port'], f'/api/conversations/{fixture["cid"]}/brief',
                                {'expected_revision': 2, 'document': {**document, 'title': 'Trial edit after upgrade'}}, 'PUT')
                if reply['revision'] != 3:
                    raise ValueError('Upgraded application could not save a new revision')
            command(candidate, venv, state, output, 'upgraded-receipts', ['-c', VERIFY_RECEIPTS, str(state)])
            passed('upgrade_and_edit', **upgraded)
            begin('unsupported_schema_refused')
            with closing(sqlite3.connect(state / 'secretary.sqlite3')) as conn:
                conn.execute("UPDATE schema_meta SET value='999' WHERE key='version'")
                conn.commit()
            before_refusal = database_digest(state)
            with station(candidate, venv, state, output, 'refused', reject=True) as refusal:
                if database_digest(state) != before_refusal:
                    raise ValueError('Refused startup changed the database')
            passed('unsupported_schema_refused', **refusal)
            begin('restore')
            restored = root / 'restored'
            command(candidate, venv, state, output, 'restore', ['scripts/backup_workstation.py',
                    'restore', str(archive), str(restored)])
            state.rename(root / 'retained-trial')
            (restored / 'state').rename(state)
            begin('rollback_code_and_data')
            with station(baseline, baseline_venv, state, output, 'rollback') as rollback:
                inspect(rollback['port'], fixture, state)
                if rollback['device_id'] != before['device_id']:
                    raise ValueError('Rollback changed the node identity')
            command(baseline, baseline_venv, state, output, 'rollback-receipts', ['-c', VERIFY_RECEIPTS, str(state)])
            passed('rollback_code_and_data', **rollback)
            report['status'] = 'passed'
            return True
    except BaseException as exc:
        report['status'] = 'interrupted' if isinstance(exc, (KeyboardInterrupt, SystemExit)) else 'failed'
        report['error'] = f'{type(exc).__name__}: {exc}'
        raise
    finally:
        report['finished_at'] = datetime.now(timezone.utc).isoformat()
        save()
        print(f'[Upgrade] {report["status"]}: {output / "upgrade.json"}', flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True, help='New directory for reports and fixture backup')
    parser.add_argument('--baseline-ref', default=BASELINE, help='Trusted local ancestor commit supporting isolated data')
    parser.add_argument('--venv', type=Path, default=Path(sys.prefix))
    parser.add_argument('--baseline-venv', type=Path, help='Defaults to candidate venv; must satisfy baseline lock')
    args = parser.parse_args()
    return 0 if run_drill(args.output.resolve(), args.baseline_ref, args.venv.resolve(),
                         (args.baseline_venv or args.venv).resolve()) else 1


if __name__ == '__main__':
    raise SystemExit(main())
