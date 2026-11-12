"""Durable delivery acceptance: retries, crash recovery, identities and real HTTP."""

import json
import socket
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
import requests
import uvicorn
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lan_mesh.application_lifecycle import stop_signal, join_threads, close_component_runtime
from lan_mesh.database import Database
from lan_mesh.delivery_protocol import build_result, seal_payload, validate_payload
from lan_mesh.delivery_routes import build_delivery_routes
from lan_mesh.mesh_delivery_service import call_delivery, delivery_repository, bind_pm_delivery
from lan_mesh.pm_agent import ProjectManagerAgent
from lan_mesh.pm_monitor import PMMonitor
from lan_mesh.pm_state import PMState


class Owner:
    """Isolated application owner with a real DB and an observable runtime."""

    def __init__(self, path):
        self.db = Database(str(path))
        self.state = SimpleNamespace(api_port=0, pm_agent=None, sub_agents={})
        self._threads = []
        self.calls = []

    def create_subagent(self, **spec):
        runtime = SimpleNamespace(execute=self.execute, cancel=lambda: None)
        self.state.sub_agents[spec['preferred_agent_id']] = {'runtime': runtime, 'status': 'idle'}

    def execute(self, payload):
        self.calls.append(payload)
        return {'status': 'completed', 'output': {'text': '长结果边界✓' * 50000},
                'usage': {'input_tokens': 12, 'output_tokens': 34}}

    def close(self):
        stop_signal(self).set()
        assert not join_threads(list(self._threads), 10)
        close_component_runtime(self)


@pytest.fixture
def owner(tmp_path, monkeypatch):
    monkeypatch.setattr(Database, 'backup', lambda self, keep=3: '')
    instance = Owner(tmp_path / 'delivery.db')
    yield instance
    instance.close()


def task(**changes):
    payload = {'pm_id': 'pm-a', 'parent_task_id': 'task-a', 'subtask_id': 'subtask-a',
               'attempt_id': 'attempt-a', 'name': '分析', 'reporter_id': 'agent-a',
               'required_skill': 'code_generation', 'input_data': {'requirement': '分析'},
               'reply_url': 'http://127.0.0.1:1', 'agent_spec': {}}
    payload.update(changes)
    return seal_payload(payload)


def wait_until(predicate, seconds=8):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.02)
    assert predicate(), 'delivery condition did not become true'


def make_pm(owner, envelope):
    pm = ProjectManagerAgent('pm-a', SimpleNamespace(), 'http://127.0.0.1:1', 'origin')
    bind_pm_delivery(owner, pm)
    pm._state.task = {'task_id': 'task-a'}
    pm._state.active_attempts[envelope['name']] = envelope
    pm._running = True
    seen = []

    def receive(report):
        seen.append(report)
        pm._state.subtask_outputs[report['task_name']] = report['output']

    pm._monitor = SimpleNamespace(receive_progress_report=receive)
    pm._persist_snapshot = lambda phase: pm.persist_delivery_state()
    owner.state.pm_agent = pm
    pm.persist_delivery_state()
    return pm, seen


def test_outbox_reopens_with_complete_unicode_and_conflict_guard(owner):
    repo = delivery_repository(owner)
    report = build_result(task(), owner.execute(task()))
    assert repo.enqueue(report['result_id'], 'http://peer/result', report)
    assert not repo.enqueue(report['result_id'], 'http://peer/result', report)
    reopened = Database(str(owner.db.path)).repositories.delivery
    assert json.loads(reopened.list_outbox()[0]['payload_json']) == report
    with pytest.raises(ValueError, match='different message'):
        repo.enqueue(report['result_id'], 'http://other/result', report)
    validate_payload(report, 'result')


@pytest.mark.parametrize('kind', ['task', 'result'])
def test_modified_payload_is_rejected_before_acceptance(owner, kind):
    body = task() if kind == 'task' else build_result(task(), {'status': 'completed', 'output': {}})
    body['input_data' if kind == 'task' else 'output'] = {'changed': True}
    with pytest.raises(ValueError, match='checksum'):
        call_delivery(owner, f'accept_{kind}', payload=body)
    assert call_delivery(owner, 'inspect')['executions'] == {}


def test_protocol_routes_keep_mesh_auth_and_reject_invalid_messages(owner, monkeypatch):
    import lan_mesh.station_routes_common as common
    token = uuid.uuid4().hex
    monkeypatch.setattr(common, '_mesh_auth_enabled', True)
    monkeypatch.setattr(common, '_mesh_auth_token', token)
    monkeypatch.setattr(common, '_API_KEY', '')
    monkeypatch.setattr(common, '_user_tokens', {})
    app = FastAPI(); app.middleware('http')(common.api_guard_middleware)
    app.include_router(build_delivery_routes(owner))
    headers = {'Authorization': f'Bearer {token}'}
    with TestClient(app) as client:
        for endpoint in ('task', 'result', 'cancel'):
            assert client.post(f'/mesh-delivery/{endpoint}', json=task()).status_code == 401
        assert client.get('/mesh-delivery/status').status_code == 401
        response = client.post('/mesh-delivery/task', json=task(), headers=headers)
        assert response.status_code == 200 and response.json()['ack_id'] == 'task:attempt-a'
        altered = {**task(), 'name': 'altered after hashing'}
        assert client.post('/mesh-delivery/task', json=altered, headers=headers).status_code == 400
        assert client.get('/mesh-delivery/status', headers=headers).json()['executions'] == {'queued': 1}


def test_duplicate_task_is_receipted_concurrently_once(owner):
    body = task()
    with ThreadPoolExecutor(8) as pool:
        receipts = list(pool.map(lambda _: call_delivery(owner, 'accept_task', payload=body), range(24)))
    assert all(item['ack_id'] == 'task:attempt-a' for item in receipts)
    assert len(delivery_repository(owner).executions('queued')) == 1
    assert not owner.calls  # transport acceptance never runs tools inline
    altered = task(input_data={'requirement': 'different'})
    with pytest.raises(ValueError, match='reused'):
        call_delivery(owner, 'accept_task', payload=altered)


def test_execution_and_result_outbox_commit_together(owner):
    repo = delivery_repository(owner)
    body = task()
    call_delivery(owner, 'accept_task', payload=body)
    assert repo.claim_execution(body['attempt_id'])
    assert not repo.claim_execution(body['attempt_id'])
    result = owner.execute(body)
    report = build_result(body, result)
    repo.finish_execution(body['attempt_id'], 'completed', result, 'http://peer/result', report)
    assert json.loads(repo.executions('completed')[0]['result_json']) == result
    assert json.loads(repo.list_outbox()[0]['payload_json'])['execution_result'] == result
    receipt = call_delivery(owner, 'accept_task', payload=body)
    assert receipt['receipt_state'] == 'completed'
    assert len(owner.calls) == 1


def test_result_commit_rolls_back_if_outbox_write_fails(owner):
    body = task(); repo = delivery_repository(owner)
    call_delivery(owner, 'accept_task', payload=body)
    repo.claim_execution(body['attempt_id'])
    conn = owner.db._get_conn()
    conn.execute("""CREATE TRIGGER fail_result_outbox BEFORE INSERT ON mesh_delivery_outbox
                    BEGIN SELECT RAISE(ABORT, 'disk write rejected'); END""")
    conn.commit()
    result = {'status': 'completed', 'output': {'text': 'must survive'}}
    with pytest.raises(Exception, match='disk write rejected'):
        repo.finish_execution(body['attempt_id'], 'completed', result,
                              'http://peer/result', build_result(body, result))
    assert repo.execution_status(body['attempt_id']) == 'started'
    assert not repo.list_outbox()


def test_retries_do_not_run_same_runtime_concurrently(owner):
    first = task(); second = task(attempt_id='attempt-b')
    repo = delivery_repository(owner)
    for envelope in (first, second):
        call_delivery(owner, 'accept_task', payload=envelope)
    assert repo.claim_execution('attempt-a')
    assert not repo.claim_execution('attempt-b')
    repo.finish_execution('attempt-a', 'completed', {'status': 'completed'})
    assert repo.claim_execution('attempt-b')


def test_cancel_fence_prevents_completed_result_commit(owner):
    envelope = task(); repo = delivery_repository(owner)
    call_delivery(owner, 'accept_task', payload=envelope)
    repo.claim_execution('attempt-a')
    repo.cancel_execution('attempt-a')
    result = {'status': 'completed', 'output': {'late': True}}
    assert not repo.finish_execution('attempt-a', 'completed', result,
                                     'http://peer/result', build_result(envelope, result))
    assert not repo.list_outbox()
    assert repo.execution_status('attempt-a') == 'cancel_requested'


def test_terminal_execution_receipt_cannot_be_overwritten(owner):
    envelope = task(); repo = delivery_repository(owner)
    call_delivery(owner, 'accept_task', payload=envelope)
    repo.finish_execution('attempt-a', 'cancelled', {'status': 'cancelled'})
    assert not repo.finish_execution('attempt-a', 'completed', {'status': 'completed'})
    assert repo.execution_status('attempt-a') == 'cancelled'


def test_concurrent_senders_claim_one_lease_and_expired_lease_recovers(owner):
    repo = delivery_repository(owner)
    repo.enqueue('id', 'http://peer', {'x': 1})
    with ThreadPoolExecutor(6) as pool:
        batches = list(pool.map(lambda _: repo.claim_due(lease_seconds=30), range(6)))
    assert sum(len(batch) for batch in batches) == 1
    conn = owner.db._get_conn()
    conn.execute('UPDATE mesh_delivery_outbox SET lease_until=0'); conn.commit()
    assert repo.claim_due()[0]['attempts'] == 2


@pytest.mark.parametrize('ack', [None, {}, {'ok': False}, {'ok': True, 'ack_id': 'wrong'},
                                  {'ok': True, 'ack_id': 'result-id', 'ack_sha256': 'wrong'}])
def test_http_200_without_exact_ack_keeps_retrying(owner, monkeypatch, ack):
    repo = delivery_repository(owner)
    report = build_result(task(), {'status': 'completed', 'output': {}})
    repo.enqueue('result-id', 'http://peer/result', report)
    monkeypatch.setattr('lan_mesh.mesh_delivery_service.requests.post', lambda *a, **kw:
                        SimpleNamespace(raise_for_status=lambda: None, json=lambda: ack))
    call_delivery(owner, 'pump')
    row = repo.list_outbox()[0]
    assert row['state'] == 'pending' and row['attempts'] == 1 and row['last_error']


def test_successful_checksum_ack_stops_redelivery(owner, monkeypatch):
    report = build_result(task(), {'status': 'completed', 'output': {}})
    repo = delivery_repository(owner)
    repo.enqueue(report['result_id'], 'http://peer/result', report)
    sent = []

    def post(url, **kwargs):
        sent.append(kwargs)
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {
            'ok': True, 'ack_id': report['result_id'], 'ack_sha256': report['payload_sha256']})

    monkeypatch.setattr('lan_mesh.mesh_delivery_service.requests.post', post)
    call_delivery(owner, 'pump'); call_delivery(owner, 'pump')
    assert len(sent) == 1 and sent[0]['timeout'] == 10
    assert repo.list_outbox()[0]['state'] == 'acked'


def test_result_receipt_survives_pm_absence_then_applies_once(owner):
    envelope = task()
    report = build_result(envelope, {'status': 'completed', 'output': {'text': '完整'}})
    ack = call_delivery(owner, 'accept_result', payload=report)
    assert ack['ack_id'] == report['result_id']
    assert delivery_repository(owner).received_results()
    pm, seen = make_pm(owner, envelope)
    call_delivery(owner, 'pump')
    wait_until(lambda: len(seen) == 1)
    wait_until(lambda: call_delivery(owner, 'inspect')['inbox'].get('processed') == 1)
    assert call_delivery(owner, 'accept_result', payload=report)['receipt_state'] == 'processed'
    call_delivery(owner, 'pump')
    assert len(seen) == 1
    restored = PMState.from_snapshot(json.loads(delivery_repository(owner).get_pm(pm.pm_id)['state_json']))
    assert report['result_id'] in restored.received_results
    assert envelope['attempt_id'] in restored.settled_attempts


@pytest.mark.parametrize('change', [
    {'pm_id': 'another-pm'}, {'parent_task_id': 'another-task'},
    {'attempt_id': 'old-attempt'}, {'subtask_id': 'another-subtask'}])
def test_late_or_foreign_result_cannot_advance_pm(owner, change):
    envelope = task()
    pm, seen = make_pm(owner, envelope)
    report = build_result(task(**change), {'status': 'completed', 'output': {'unexpected': True}})
    assert 'ignored' in pm.receive_delivery_result(report)
    assert not seen and not pm._state.subtask_outputs


def test_second_result_id_for_settled_attempt_cannot_apply_again(owner):
    envelope = task()
    pm, seen = make_pm(owner, envelope)
    report = build_result(envelope, {'status': 'completed', 'output': {'x': 1}})
    assert pm.receive_delivery_result(report) == {'applied': True}
    alternate = seal_payload({**report, 'result_id': 'another-result', 'output': {'x': 2}})
    assert 'ignored' in pm.receive_delivery_result(alternate)
    assert len(seen) == 1 and pm._state.subtask_outputs['分析'] == {'x': 1}


def test_cancelled_pm_and_old_dispatch_are_fenced(owner, monkeypatch):
    envelope = task()
    pm, seen = make_pm(owner, envelope)
    pm._running = False; pm._delivery_status = 'cancelled'; pm.persist_delivery_state()
    report = build_result(envelope, {'status': 'completed', 'output': {}})
    call_delivery(owner, 'accept_result', payload=report)
    call_delivery(owner, 'queue_task', payload=envelope, destination='http://peer')
    monkeypatch.setattr('lan_mesh.mesh_delivery_service.requests.post',
                        lambda *a, **kw: pytest.fail('cancelled dispatch was sent'))
    call_delivery(owner, 'pump')
    wait_until(lambda: call_delivery(owner, 'inspect')['inbox'].get('processed') == 1)
    assert not seen and delivery_repository(owner).list_outbox()[0]['state'] == 'superseded'


def test_cancel_arriving_before_task_survives_reopen_without_execution(owner):
    envelope = task()
    call_delivery(owner, 'accept_cancel', payload=envelope)
    assert call_delivery(owner, 'accept_task', payload=envelope)['receipt_state'] == 'cancelled'
    reopened = Database(str(owner.db.path)).repositories.delivery
    assert reopened.executions('cancelled') and not reopened.executions('queued')
    assert not owner.calls


def test_crashed_execution_is_reported_for_review_without_reexecution(owner, monkeypatch):
    envelope = task()
    repo = delivery_repository(owner)
    call_delivery(owner, 'accept_task', payload=envelope)
    repo.claim_execution(envelope['attempt_id'])
    monkeypatch.setattr('lan_mesh.mesh_delivery_service.requests.post',
                        lambda *a, **kw: (_ for _ in ()).throw(requests.ConnectionError('offline')))
    call_delivery(owner, 'start')
    wait_until(lambda: bool(repo.executions('interrupted')))
    report = json.loads(repo.list_outbox()[0]['payload_json'])
    assert report['execution_result']['stop_reason'] == 'execution_interrupted'
    assert not owner.calls
    assert call_delivery(owner, 'accept_task', payload=envelope)['receipt_state'] == 'interrupted'


def test_crashed_result_application_is_visible_for_review(owner):
    report = build_result(task(), {'status': 'completed', 'output': {}})
    call_delivery(owner, 'accept_result', payload=report)
    repo = delivery_repository(owner)
    assert repo.claim_result(report['result_id'])
    call_delivery(owner, 'start')
    assert call_delivery(owner, 'inspect')['inbox'] == {'review_required': 1}


def test_resume_dispatch_reuses_immutable_attempt(owner):
    envelope = task()
    pm, seen = make_pm(owner, envelope)
    pm._state.task_station['分析'] = {'ip': '127.0.0.1', 'api_port': 1}
    assert pm._resume_delivery('分析')
    assert pm._resume_delivery('分析')
    rows = delivery_repository(owner).list_outbox()
    assert len(rows) == 1 and json.loads(rows[0]['payload_json']) == envelope


def test_dispatcher_creates_distinct_attempts_with_stable_subtask_id(owner):
    envelope = task(); pm, seen = make_pm(owner, envelope)
    owner.state.api_port = 12345
    station = {'ip': '127.0.0.1', 'api_port': 12346}
    info = {'agent_id': 'agent-a', 'delivery_protocol': 1, 'agent_spec': {'agent_name': '分析'}}
    sub = {'name': '分析', 'skill': 'code_generation'}
    for _ in range(2):
        pm._dispatcher.dispatch_subtask(station, info, {'task_id': 'task-a'}, sub)
    rows = delivery_repository(owner).list_outbox()
    messages = [json.loads(row['payload_json']) for row in rows]
    assert len(messages) == 2
    assert len({body['subtask_id'] for body in messages}) == 1
    assert len({body['attempt_id'] for body in messages}) == 2
    assert all(body['reply_url'] == 'http://127.0.0.1:12345' for body in messages)
    assert pm._state.active_attempts['分析'] == messages[-1]


@pytest.mark.parametrize('started', [False, True])
def test_abrupt_process_exit_preserves_accepted_task(owner, tmp_path, monkeypatch, started):
    path = tmp_path / 'crashed.db'
    source = """
import json, os, sys
from lan_mesh.database import Database
from lan_mesh.mesh_delivery_service import call_delivery, delivery_repository
from types import SimpleNamespace
Database.backup = lambda *a, **k: ''
owner = SimpleNamespace(db=Database(sys.argv[1]))
payload = json.loads(sys.argv[2])
call_delivery(owner, 'accept_task', payload=payload)
if sys.argv[3] == 'True':
    delivery_repository(owner).claim_execution(payload['attempt_id'])
os._exit(17)
"""
    child = subprocess.run([sys.executable, '-c', source, str(path), json.dumps(task()), str(started)],
                           capture_output=True, timeout=15)
    assert child.returncode == 17, child.stderr.decode()
    reopened = Owner(path)
    monkeypatch.setattr('lan_mesh.mesh_delivery_service.requests.post',
                        lambda *a, **kw: (_ for _ in ()).throw(requests.ConnectionError('offline')))
    try:
        call_delivery(reopened, 'start')
        repo = delivery_repository(reopened)
        expected = 'interrupted' if started else 'completed'
        wait_until(lambda: bool(repo.executions(expected)))
        assert len(reopened.calls) == (0 if started else 1)
        assert len(repo.list_outbox()) == 1
    finally:
        reopened.close()


def test_legacy_unscoped_results_cannot_override_protocol_attempt(owner):
    envelope = task()
    pm, seen = make_pm(owner, envelope)
    pm.receive_progress_report({'task_name': '分析', 'status': 'completed'})
    pm.receive_subtask_result('分析', 'completed', {'unexpected': True})
    assert not seen


def test_pm_application_exception_preserves_review_receipt(owner):
    envelope = task()
    pm, seen = make_pm(owner, envelope)
    pm._monitor.receive_progress_report = lambda report: (_ for _ in ()).throw(RuntimeError('apply interrupted'))
    report = build_result(envelope, {'status': 'completed', 'output': {}})
    call_delivery(owner, 'accept_result', payload=report)
    call_delivery(owner, 'pump')
    wait_until(lambda: call_delivery(owner, 'inspect')['inbox'].get('review_required') == 1)


def test_interrupted_side_effects_wait_for_review_without_retry_or_aggregation(owner):
    envelope = task(); pm, seen = make_pm(owner, envelope)
    events = []
    pm.report_status = lambda status, **kw: events.append(status)
    pm.report_progress = lambda *a, **kw: None
    pm.sync_subtasks = lambda: None
    runtime = SimpleNamespace(_call_llm_with_routing=lambda *a, **kw: pytest.fail('reviewed task aggregated'))
    monitor = PMMonitor(pm.pm_id, runtime, pm.secretary_url, pm._state, pm, pm._dispatcher)
    monitor.handle_subagent_failure = lambda *a, **kw: pytest.fail('ambiguous execution automatically retried')
    report = build_result(envelope, {'status': 'failed', 'output': {},
                                     'stop_reason': 'execution_interrupted', 'error': 'worker restarted'})
    monitor.receive_progress_report(report)
    pm._state.plan = {'decomposition': [{'name': '分析'}]}
    monitor.aggregate_results()
    assert pm._state.review_required['分析'] == 'worker restarted'
    assert events == ['awaiting_input', 'awaiting_input']


def test_cancel_during_aggregation_prevents_delivery(owner):
    envelope = task(); pm, seen = make_pm(owner, envelope)
    pm._state.plan = {'decomposition': [{'name': '分析'}]}
    pm._state.subtask_outputs = {'分析': {'text': 'complete'}}

    def model(*args, **kwargs):
        pm._running = False
        return {'content': 'late aggregation'}

    runtime = SimpleNamespace(_call_llm_with_routing=model)
    monitor = PMMonitor(pm.pm_id, runtime, pm.secretary_url, pm._state, pm, pm._dispatcher)
    pm.report_status = lambda *a, **kw: pytest.fail('cancelled PM marked completed')
    pm.deliver_result = lambda *a, **kw: pytest.fail('cancelled PM delivered')
    monitor.aggregate_results()
    assert '_aggregated' not in pm._state.subtask_outputs


def test_real_http_two_isolated_journals_dispatch_and_recover_long_result(owner, tmp_path):
    remote = Owner(tmp_path / 'remote.db')
    servers = []
    session = requests.Session(); session.trust_env = False
    try:
        for instance in (owner, remote):
            app = FastAPI(); app.include_router(build_delivery_routes(instance))
            sock = socket.socket(); sock.bind(('127.0.0.1', 0)); sock.listen(128)
            instance.state.api_port = sock.getsockname()[1]
            server = uvicorn.Server(uvicorn.Config(app, log_level='error'))
            thread = threading.Thread(target=lambda s=server, sk=sock: s.run(sockets=[sk]), daemon=True)
            thread.start(); servers.append((server, thread, sock))
            wait_until(lambda: server.started)
        envelope = task(reply_url=f'http://127.0.0.1:{owner.state.api_port}')
        pm, seen = make_pm(owner, envelope)
        call_delivery(owner, 'queue_task', payload=envelope,
                      destination=f'http://127.0.0.1:{remote.state.api_port}')
        call_delivery(owner, 'pump')
        assert delivery_repository(owner).list_outbox()[0]['state'] == 'acked'
        call_delivery(remote, 'pump')
        wait_until(lambda: bool(delivery_repository(remote).executions('completed')))
        # The complete result stays on remote disk while the origin is unavailable.
        server, thread, sock = servers[0]
        server.should_exit = True; thread.join(5)
        call_delivery(remote, 'pump')
        pending = delivery_repository(remote).list_outbox()[0]
        assert pending['state'] == 'pending' and pending['last_error']
        report = json.loads(pending['payload_json'])
        assert len(report['output']['text']) == 300000
        validate_payload(report, 'result')
        # Restart origin with its same DB but a new application/PM instance.
        restarted = Owner(owner.db.path)
        restored_pm, restored_seen = make_pm(restarted, envelope)
        checkpoint = delivery_repository(owner).get_pm(pm.pm_id)
        restored_pm._state.restore_from(json.loads(checkpoint['state_json']))
        app = FastAPI(); app.include_router(build_delivery_routes(restarted))
        sock2 = socket.socket(); sock2.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock2.bind(('127.0.0.1', owner.state.api_port)); sock2.listen(128)
        server2 = uvicorn.Server(uvicorn.Config(app, log_level='error'))
        thread2 = threading.Thread(target=lambda: server2.run(sockets=[sock2]), daemon=True)
        thread2.start(); servers.append((server2, thread2, sock2))
        wait_until(lambda: server2.started)
        conn = remote.db._get_conn(); conn.execute('UPDATE mesh_delivery_outbox SET next_attempt_at=0'); conn.commit()
        call_delivery(remote, 'pump')
        assert delivery_repository(remote).list_outbox()[0]['state'] == 'acked'
        call_delivery(restarted, 'pump')
        wait_until(lambda: len(restored_seen) == 1)
        url = f'http://127.0.0.1:{owner.state.api_port}/mesh-delivery/result'
        assert session.post(url, json=report, timeout=10).json()['ok']
        call_delivery(restarted, 'pump')
        assert len(remote.calls) == 1 and len(restored_seen) == 1
        assert restored_seen[0]['output'] == report['output']
        assert restored_seen[0]['execution_result']['usage']['output_tokens'] == 34
        restarted.close()
    finally:
        for server, thread, sock in reversed(servers):
            server.should_exit = True; thread.join(5); sock.close()
        session.close(); remote.close()
