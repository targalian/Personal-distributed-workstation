"""Exercise process death, atomic recovery and original execution reuse through delivery."""
import json
import sqlite3
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from lan_mesh.pm_agent import ProjectManagerAgent
from lan_mesh.pm_checkpoint import PMCheckpoints, read_checkpoint
from lan_mesh.pm_lease import lease_identity
from lan_mesh.pm_resume import resume_pm_once
from lan_mesh.pm_state import PMState
from lan_mesh.protocol import PMAgent, Task
from lan_mesh.task_resume import prepare_resume, request_task_resume, resume_authorization
from tests.test_pm_start import client_for, live_server
from tests.test_pm_start import controller as _controller_fixture

controller = _controller_fixture


@pytest.fixture
def recovery(controller, monkeypatch):
    db = controller.db
    task = Task(task_id='task-resume', name='resume', description='recover original requirement', status='running',
                pm_agent_id='pm-resume', input_data={'_brief': {'revision': 7}, 'requirement': 'original'})
    db.save_task(task)
    db.upsert_pm_agent(PMAgent(pm_id='pm-resume', task_id=task.task_id, device_id='self', ip='127.0.0.1', api_port=12345))
    path = db.path.with_name(db.path.stem + '-pm-starts.sqlite3')
    state = PMState()
    state.task = task.to_dict()
    state.plan = {'pattern': 'orchestrator', 'decomposition': [{'name': 'A'}, {'name': 'B', 'depends_on': ['A']}]}
    state.subtask_outputs = {'A': {'summary': 'saved A'}}
    state.phase = 'monitoring'
    store = PMCheckpoints(path, owner='exited-process', lease=lease_identity(path))
    revision = [0]
    def save():
        revision[0] = store.save('pm-resume', task.task_id, 'self', {**state.snapshot(), 'secretary_url': 'http://original'}, revision[0])
    save()
    client = client_for(controller)
    def post(url, **kwargs):
        return client.post('/api/' + url.split('/api/', 1)[1], json=kwargs.get('json', {}))
    monkeypatch.setattr('lan_mesh.pm_agent.http_post', post)
    original = ProjectManagerAgent.__init__
    created = []
    def init(self, *args, **kwargs):
        original(self, *args, **kwargs)
        created.append(self)
        self._planner.load_skill = Mock()
        self._dispatcher.get_available_stations = lambda: []
        self._distribute_artifacts = Mock()
        self._record_task_memory = Mock()
        self._monitor.poll_interval = 0.01
    monkeypatch.setattr(ProjectManagerAgent, '__init__', init)
    controller.chat_runtime.execute.return_value = {'status': 'completed', 'output': {'summary': 'B done'}}
    controller.chat_runtime._call_llm_with_routing.return_value = {'content': 'recovered artifact'}
    data = SimpleNamespace(controller=controller, path=path, state=state, store=store, save=save,
                           revision=revision, client=client, created=created)
    yield data
    for pm in created:
        pm.stop()
        for thread in (pm._thread, pm._progress_thread):
            if thread:
                thread.join(timeout=4)
                assert not thread.is_alive()


def finish(recovery):
    pm = recovery.controller._local_pm_agent
    assert pm is not None
    pm._thread.join(timeout=4)
    pm._progress_thread.join(timeout=4)
    assert not pm._thread.is_alive() and not pm._progress_thread.is_alive()
    return pm


def resume(recovery):
    return request_task_resume(recovery.controller, 'task-resume', recovery.revision[0])


def test_saved_output_reused_original_task_delivered_and_owner_fenced(recovery):
    before = recovery.state.start_time
    assert resume(recovery)['ok']
    pm = finish(recovery)
    assert [c.args[0]['name'] for c in pm.runtime.execute.call_args_list] == ['B']
    assert pm.runtime.execute.call_args.args[0]['input_data']['_dependency_outputs']['A'] == {'summary': 'saved A'}
    assert pm._state.start_time == before and pm._state.task['input_data']['_brief']['revision'] == 7
    task = recovery.controller.db.get_task('task-resume')
    assert task.status == 'completed' and task.output_data['_delivery']['deliverable'] == 'recovered artifact'
    latest = read_checkpoint(recovery.path, 'pm-resume', 'task-resume', 'self')
    assert latest['snapshot']['phase'] == 'completed'
    with pytest.raises(ValueError):
        recovery.store.save('pm-resume', 'task-resume', 'self', recovery.state.snapshot(), latest['revision'])


def test_concurrent_recovery_and_lost_response_retry_do_not_reexecute(recovery):
    entered, release = threading.Event(), threading.Event()
    def execute(payload):
        entered.set()
        assert release.wait(5)
        return {'status': 'completed', 'output': {'summary': 'once'}}
    recovery.controller.chat_runtime.execute.side_effect = execute
    try:
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(lambda _: resume(recovery), range(6)))
        assert entered.wait(2)
        assert all(r['ok'] for r in results)
        assert len({r['request_id'] for r in results}) == 1
        assert len(recovery.created) == 1
        assert resume(recovery)['duplicate']
    finally:
        release.set()
    finish(recovery)
    assert recovery.controller.chat_runtime.execute.call_count == 1


@pytest.mark.parametrize('kind', ['cancelled', 'paused', 'completed', 'delivery', 'control', 'replacement', 'budget'])
def test_authorization_rejects_business_conflicts(recovery, kind):
    ctl = recovery.controller
    task = ctl.db.get_task('task-resume')
    if kind in ('cancelled', 'paused', 'completed'):
        task.status = kind
    elif kind == 'delivery':
        task.output_data['_delivery'] = {'deliverable': 'already delivered'}
    elif kind == 'control':
        task.output_data['_pm_control'] = {'state': 'unknown'}
    elif kind == 'replacement':
        with ctl.db._get_conn() as conn:
            conn.execute('INSERT INTO task_resubmissions VALUES (?,?,?,?,?)', ('task-resume', 'replacement', time.time(), '{}', 1))
    else:
        task.project_id = 'project'
        ctl.project_manager = Mock(check_budget=Mock(return_value=False))
    ctl.db.save_task(task)
    with pytest.raises(ValueError):
        resume(recovery)
    assert not recovery.created


@pytest.mark.parametrize('kind', ['stopped', 'deadline', 'local_unknown', 'direct_unknown', 'legacy', 'moved', 'failed', 'clarification', 'stale'])
def test_unsafe_checkpoint_never_starts_or_changes_owner(recovery, kind):
    st = recovery.state
    if kind == 'stopped':
        st.stop_event.set()
    elif kind == 'deadline':
        st.start_time = 1
    elif kind == 'local_unknown':
        st.execution_attempts['B'] = {'name': 'B', 'subtask_id': 'old-B', 'pm_id': 'pm-resume',
                                      'parent_task_id': 'task-resume', 'reporter_id': 'worker', 'phase': 'active', 'station': {}}
    elif kind == 'direct_unknown':
        st.plan['pattern'] = 'single'
        st.direct_execution = {'state': 'active', 'execution_id': 'old-direct'}
    elif kind == 'failed':
        st.phase = 'failed'
    elif kind == 'clarification':
        st.phase = 'awaiting_input'
    elif kind == 'legacy':
        recovery.store.lease = None
    elif kind == 'moved':
        recovery.store.lease = {**lease_identity(recovery.path), 'path': '/other/location'}
    recovery.save()
    if kind == 'stale':
        recovery.revision[0] -= 1
    before = recovery.path.read_bytes()
    result = resume(recovery)
    assert not result['ok'] and not recovery.created
    assert recovery.path.read_bytes() == before


def test_cancel_after_claim_before_execution_stops_new_work(recovery, monkeypatch):
    original = PMCheckpoints.takeover
    def takeover(self, *args):
        result = original(self, *args)
        recovery.controller.db.begin_task_control('task-resume', 'cancel')
        return result
    monkeypatch.setattr(PMCheckpoints, 'takeover', takeover)
    assert not resume(recovery)['ok']
    recovery.controller.chat_runtime.execute.assert_not_called()
    assert recovery.controller.db.get_task('task-resume').status == 'cancelled'


def test_stored_unreviewed_result_passes_quality_gate_before_dependency(recovery):
    st = recovery.state
    st.subtask_outputs = {}
    report = {'subtask_id': 'old-A', 'pm_id': 'pm-resume', 'parent_task_id': 'task-resume',
              'reporter_id': 'worker', 'task_name': 'A', 'status': 'completed', 'output': {'summary': 'saved raw A'}}
    st.execution_attempts['A'] = {**report, 'name': 'A', 'phase': 'processing', 'received_result': report, 'station': {}}
    recovery.save()
    assert resume(recovery)['ok']
    pm = finish(recovery)
    assert [c.args[0]['name'] for c in pm.runtime.execute.call_args_list] == ['B']
    assert pm._state.execution_attempts['A']['subtask_id'] == 'old-A'
    assert pm._state.subtask_outputs['A'] == report['output']


def test_saved_aggregate_and_delivery_payload_are_reused(recovery):
    st = recovery.state
    st.subtask_outputs.update(B={'summary': 'saved B'}, _aggregated='exact original artifact')
    st.delivery = {'state': 'sending', 'payload': {'task_id': 'task-resume', 'pm_id': 'pm-resume',
                       'task_name': 'resume', 'deliverable': 'exact original artifact', 'summary': 'original',
                       'subtask_stats': {'total': 2, 'completed': 2, 'failed': 0}, 'delivered_at': 123}}
    recovery.save()
    assert resume(recovery)['ok']
    pm = finish(recovery)
    pm.runtime.execute.assert_not_called()
    pm.runtime._call_llm_with_routing.assert_not_called()
    delivery = recovery.controller.db.get_task('task-resume').output_data['_delivery']
    assert delivery['delivered_at'] == 123 and delivery['summary'] == 'original'


def test_returned_direct_execution_is_delivered_without_repeat(recovery):
    st = recovery.state
    st.plan = {'pattern': 'single', 'decomposition': [{'name': 'A'}]}
    st.subtask_outputs = {}
    st.direct_execution = {'execution_id': 'old-direct', 'state': 'returned',
                           'result': {'status': 'completed', 'summary': 'direct artifact'}}
    recovery.save()
    assert resume(recovery)['ok']
    pm = finish(recovery)
    pm.runtime.execute.assert_not_called()
    assert recovery.controller.db.get_task('task-resume').output_data['_delivery']['deliverable'] == 'direct artifact'


def test_wrong_revision_request_does_not_revoke_existing_authorization(recovery):
    old = prepare_resume(recovery.controller, 'task-resume', 1)
    prepare_resume(recovery.controller, 'task-resume', 500)
    assert resume_authorization(recovery.controller, 'task-resume', old['request_id'])['authorization']['expected_revision'] == 1
    stale = recovery.controller.db.get_task('task-resume')
    latest = prepare_resume(recovery.controller, 'task-resume', 2)
    recovery.controller.db.save_task(stale)
    assert recovery.controller.db.get_task('task-resume').output_data['_pm_resume']['request_id'] == latest['request_id']


def test_live_original_process_lock_prevents_takeover(recovery):
    code = """
import sys
from pathlib import Path
from lan_mesh.pm_lease import acquire_pm_lease
acquire_pm_lease(Path(sys.argv[1]))
print('LOCKED', flush=True)
sys.stdin.read(1)
"""
    proc = subprocess.Popen([sys.executable, '-c', code, str(recovery.path)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert proc.stdout.readline().strip() == 'LOCKED'
        assert not resume(recovery)['ok']
        assert not recovery.created
    finally:
        proc.communicate('x', timeout=5)
    assert proc.returncode == 0
    assert resume(recovery)['ok']
    finish(recovery)


def test_actual_pm_process_crash_after_A_resumes_B_once(recovery, tmp_path):
    # Use a new original path because process locks intentionally live until exit.
    path = tmp_path / 'original-process.sqlite3'
    trace = tmp_path / 'effects.txt'
    task_data = recovery.state.task
    code = """
import json, os, sys, time
from pathlib import Path
from unittest.mock import Mock
from lan_mesh.pm_agent import ProjectManagerAgent
from lan_mesh.pm_start import start_pm_once
import lan_mesh.pm_agent as agent_module
agent_module.http_post = Mock()
original = ProjectManagerAgent.__init__
def init(self, *args):
    original(self, *args)
    self._planner.load_skill = Mock()
    self._planner.refine_requirements = lambda t: t
    self._planner.analyze_with_skill = lambda t: {'pattern': 'orchestrator', 'decomposition': [{'name':'A'}, {'name':'B','depends_on':['A']}]}
    self._dispatcher.get_available_stations = lambda: []
    dispatch = self._dispatcher.try_dispatch_pending
    def crash_after_A():
        if 'A' in self._state.subtask_outputs:
            os._exit(23)
        dispatch()
    self._dispatcher.try_dispatch_pending = crash_after_A
ProjectManagerAgent.__init__ = init
runtime = Mock()
def execute(task):
    with open(sys.argv[2], 'a') as f:
        f.write(task['name'] + '\\n')
    return {'status':'completed','output':{'summary':'durable A'}}
runtime.execute = execute
task=json.loads(sys.argv[3])
start_pm_once(pm_id='pm-resume',task_id='task-resume',secretary_url='http://original',task_data=task,
    runtime=runtime,device_id='self',device_name='node',receipt_path=Path(sys.argv[1]),
    current=lambda:None,assign=lambda pm:None,lookup=lambda _:task)
time.sleep(10)
"""
    proc = subprocess.run([sys.executable, '-c', code, str(path), str(trace), json.dumps(task_data)], capture_output=True, timeout=15)
    assert proc.returncode == 23, proc.stderr.decode()
    record = read_checkpoint(path, 'pm-resume', 'task-resume', 'self')
    assert trace.read_text() == 'A\n' and 'A' in record['snapshot']['subtask_outputs']
    auth = prepare_resume(recovery.controller, 'task-resume', record['revision'])
    result = resume_pm_once(payload=auth, receipt_path=path, runtime=recovery.controller.chat_runtime,
                            device_id='self', device_name='node', current=lambda: recovery.controller._local_pm_agent,
                            assign=lambda pm: setattr(recovery.controller, '_local_pm_agent', pm),
                            authorization=lambda tid, rid: resume_authorization(recovery.controller, tid, rid))
    assert result['ok'], result
    pm = finish(recovery)
    assert [c.args[0]['name'] for c in pm.runtime.execute.call_args_list] == ['B']
    assert pm._state.start_time == record['snapshot']['start_time']
    assert pm._state.subtask_outputs['A'] == {'summary': 'durable A'}
    assert recovery.controller.db.get_task('task-resume').status == 'completed'


@pytest.mark.parametrize('mode', ['station', 'worker'])
def test_remote_resume_via_real_http_and_original_receipt(recovery, monkeypatch, mode):
    import requests
    from fastapi import FastAPI

    from lan_mesh.execution_receipts import ExecutionReceipts
    from lan_mesh.execution_routes import build_execution_routes
    from lan_mesh.station_controller import StationController
    from lan_mesh.station_routes_pm import build_pm_routes as secretary_routes
    from lan_mesh.station_routes_tasks import build_task_routes
    from lan_mesh.station_routes_worker import build_worker_routes
    from lan_mesh.worker import WorkerAgent
    from lan_mesh.worker_routes_pm import build_pm_routes as worker_routes
    ctl = recovery.controller
    executor_path = recovery.path.with_name('remote-executions.sqlite3')
    store = ExecutionReceipts(executor_path)
    identity = {'subtask_id': 'original-A', 'parent_task_id': 'task-resume', 'pm_id': 'pm-resume',
                'reporter_id': 'remote-agent', 'name': 'A', 'input_data': {}}
    store.claim(identity)
    executor = FastAPI()
    execute = Mock(side_effect=AssertionError('must only query original execution'))
    executor.include_router(build_execution_routes(execute, executor_path))
    @executor.get('/info')
    def info():
        return {'device_id': 'executor'}
    secretary = FastAPI()
    secretary.include_router(build_task_routes(ctl))
    secretary.include_router(secretary_routes(ctl))
    from lan_mesh.http_retry import http_post
    monkeypatch.setattr('lan_mesh.pm_agent.http_post', http_post)
    with live_server(secretary) as secretary_port, live_server(executor) as executor_port:
        node_app = FastAPI()
        if mode == 'station':
            node = StationController.__new__(StationController)
            node.db = SimpleNamespace(path=recovery.path.with_name('remote.sqlite3'))
            node.state = SimpleNamespace(device_id='remote-node', device_name='remote')
            node.discovery = Mock()
            node.secretary_active = False
            node.chat_runtime = ctl.chat_runtime
            node._local_pm_agent = None
            path = node.db.path.with_name('remote-pm-starts.sqlite3')
            def current():
                return node._local_pm_agent
            node_app.include_router(build_worker_routes(node))
        else:
            node = WorkerAgent.__new__(WorkerAgent)
            node.data_dir = recovery.path.parent
            node.state = SimpleNamespace(device_id='remote-node', device_name='remote', pm_agent=None, agent_runtime=ctl.chat_runtime)
            path = node.data_dir / 'worker-pm-starts.sqlite3'
            def current():
                return node.state.pm_agent
            node_app.include_router(worker_routes(node))
        state = recovery.state
        state.subtask_outputs = {}
        state.execution_attempts = {'A': {**identity, 'phase': 'active', 'station': {
            'device_id': 'executor', 'ip': '127.0.0.1', 'api_port': executor_port}}}
        state.dispatched = {'A'}
        PMCheckpoints(path, owner='original-process', lease=lease_identity(path)).save('pm-resume', 'task-resume', 'remote-node',
            {**state.snapshot(), 'secretary_url': f'http://127.0.0.1:{secretary_port}'}, 0)
        with live_server(node_app) as node_port:
            ctl.db.upsert_pm_agent(PMAgent(pm_id='pm-resume', task_id='task-resume', device_id='remote-node', ip='127.0.0.1', api_port=node_port))
            response = requests.post(f'http://127.0.0.1:{secretary_port}/api/tasks/task-resume/resume', json={'expected_revision': 1}, timeout=10)
            assert response.status_code == 200 and response.json()['ok'], response.text
            pm = current()
            pm._thread.join(timeout=3)
            assert pm.running
            ctl.chat_runtime.execute.assert_not_called()  # A still executing, B must wait.
            assert pm._state.execution_attempts['A']['subtask_id'] == 'original-A'
            store.finish('original-A', {'status': 'completed', 'output': {'summary': 'remote A saved'}})
            pm._progress_thread.join(timeout=5)
            assert not pm.running and not pm._progress_thread.is_alive()
            assert [c.args[0]['name'] for c in ctl.chat_runtime.execute.call_args_list] == ['B']
            assert ctl.db.get_task('task-resume').status == 'completed'
            assert pm._state.subtask_outputs['A'] == {'summary': 'remote A saved'}
    execute.assert_not_called()


def test_recovered_unknown_receipt_never_assigns_new_execution(recovery, monkeypatch):
    st = recovery.state
    st.subtask_outputs = {}
    st.execution_attempts = {'A': {'name': 'A', 'pm_id': 'pm-resume', 'parent_task_id': 'task-resume',
        'subtask_id': 'unknown-A', 'reporter_id': 'worker', 'phase': 'active',
        'station': {'device_id': 'executor', 'ip': '127.0.0.1', 'api_port': 9}}}
    st.dispatched = {'A'}
    recovery.save()
    checked = threading.Event()
    def get(url, **kwargs):
        if url.endswith('/info'):
            return Mock(status_code=200, json=lambda: {'device_id': 'executor'})
        checked.set()
        return Mock(status_code=404)
    monkeypatch.setattr('lan_mesh.pm_dispatcher.requests.get', get)
    assert resume(recovery)['ok']
    pm = recovery.controller._local_pm_agent
    assert checked.wait(2)
    assert pm.running and pm._state.execution_attempts['A']['subtask_id'] == 'unknown-A'
    pm.runtime.execute.assert_not_called()
    assert not pm._state.subtask_outputs
    pm.stop()
    finish(recovery)


def test_recovery_quality_review_finishes_before_B_runs(recovery, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    st = recovery.state
    st.subtask_outputs = {}
    report = {'subtask_id': 'old-A', 'pm_id': 'pm-resume', 'parent_task_id': 'task-resume',
              'reporter_id': 'worker', 'task_name': 'A', 'status': 'completed', 'output': {'summary': 'unreviewed'}}
    st.execution_attempts['A'] = {**report, 'name': 'A', 'phase': 'processing', 'received_result': report, 'station': {}}
    recovery.save()
    def verify(self, name, output):
        if name == 'A':
            entered.set()
            assert release.wait(4)
        return {'accepted': True}
    monkeypatch.setattr('lan_mesh.pm_monitor.PMMonitor._verify_output_quality', verify)
    try:
        assert resume(recovery)['ok']
        assert entered.wait(2)
        recovery.controller.chat_runtime.execute.assert_not_called()
        assert 'A' not in recovery.controller._local_pm_agent._state.subtask_outputs
    finally:
        release.set()
    finish(recovery)
    assert recovery.controller.chat_runtime.execute.call_count == 1


def test_takeover_write_failure_leaves_original_owner(recovery, monkeypatch):
    with sqlite3.connect(recovery.path) as conn:
        conn.execute("CREATE TRIGGER deny_takeover BEFORE UPDATE ON pm_checkpoints BEGIN SELECT RAISE(ABORT, 'write failed'); END")
    before = recovery.path.read_bytes()
    assert not resume(recovery)['ok']
    assert recovery.path.read_bytes() == before and not recovery.created


def test_new_recovery_request_cannot_take_over_same_process(recovery):
    entered, release = threading.Event(), threading.Event()
    def execute(payload):
        entered.set()
        assert release.wait(4)
        return {'status': 'completed', 'output': {'summary': 'done'}}
    recovery.controller.chat_runtime.execute.side_effect = execute
    try:
        assert resume(recovery)['ok']
        assert entered.wait(2)
        latest = read_checkpoint(recovery.path, 'pm-resume', 'task-resume', 'self')
        assert not request_task_resume(recovery.controller, 'task-resume', latest['revision'])['ok']
        assert len(recovery.created) == 1
    finally:
        release.set()
    finish(recovery)
    assert recovery.controller.db.get_task('task-resume').status == 'completed'


def test_api_requires_active_secretary_and_explicit_revision(recovery):
    client = recovery.client
    assert client.post('/api/tasks/task-resume/resume', json={}).status_code == 409
    assert client.get('/api/tasks/task-resume/resume-authorization?request_id=missing').status_code == 409
    assert client.post('/api/tasks/missing/resume', json={'expected_revision': 1}).status_code == 404
    recovery.controller.secretary_active = False
    assert client.post('/api/tasks/task-resume/resume', json={'expected_revision': 1}).status_code == 503


@pytest.mark.parametrize('phase', ['starting', 'planning', 'planned'])
def test_resume_before_execution_keeps_deadline_and_can_finish(recovery, phase):
    from lan_mesh.pm_recovery import assess_pm_recovery
    st = recovery.state
    st.phase = phase
    st.plan = {'pattern': 'single'} if phase == 'planned' else {}
    st.subtask_outputs = {}
    recovery.save()
    # Configure the normal planning path; planned single tasks must not replan.
    original = ProjectManagerAgent.__init__
    def init(self, *args, **kwargs):
        original(self, *args, **kwargs)
        self._planner.refine_requirements = lambda task: task
        self._planner.analyze_with_skill = Mock(return_value={'pattern': 'single'})
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(ProjectManagerAgent, '__init__', init)
        evidence = assess_pm_recovery(recovery.path, 'pm-resume', 'task-resume', 'self', lambda: {})
        assert evidence['resume_supported']
        assert resume(recovery)['ok']
        pm = finish(recovery)
        if phase == 'planned':
            pm._planner.analyze_with_skill.assert_not_called()
    assert pm.runtime.execute.call_count == 1 and pm._state.start_time == st.start_time
    assert recovery.controller.db.get_task('task-resume').status == 'completed'


def test_thread_start_failure_stops_assigned_recovery_instance(recovery, monkeypatch):
    monkeypatch.setattr('lan_mesh.pm_resume.threading.Thread.start', Mock(side_effect=RuntimeError('cannot start thread')))
    assert not resume(recovery)['ok']
    pm = recovery.controller._local_pm_agent
    assert not pm.running and pm._state.stop_event.is_set()
    pm.runtime.execute.assert_not_called()
    # These threads never started and cannot be joined by fixture cleanup.
    pm._thread = pm._progress_thread = None
