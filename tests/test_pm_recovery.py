"""Recovery evidence must use original identities and never dispatch or overwrite state."""
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lan_mesh.execution_receipts import ExecutionReceipts
from lan_mesh.execution_routes import build_execution_routes
from lan_mesh.pm_checkpoint import PMCheckpoints
from lan_mesh.pm_recovery import assess_pm_recovery, assess_task_recovery
from lan_mesh.pm_state import PMState
from lan_mesh.protocol import PMAgent, Task
from tests.test_pm_start import client_for, live_server
from tests.test_pm_start import controller as _controller_fixture

controller = _controller_fixture


@pytest.fixture
def checkpoint(tmp_path):
    path = tmp_path / "pm.sqlite3"
    state = PMState()
    state.task = {"task_id": "task"}
    state.phase = "monitoring"
    state.plan = {"pattern": "orchestrator", "decomposition": [{"name": "A"}, {"name": "B", "depends_on": ["A"]}, {"name": "C", "depends_on": ["B"]}]}
    state.subtask_outputs = {"A": {"private": "saved artifact"}}
    state.execution_attempts = {"B": {"subtask_id": "execution-B", "parent_task_id": "task", "pm_id": "pm",
                                        "reporter_id": "worker", "name": "B", "phase": "active",
                                        "station": {"ip": "127.0.0.1", "api_port": 12345, "device_id": "executor"}}}
    store = PMCheckpoints(path, owner="old-process")
    revision = [0]
    def save():
        revision[0] = store.save("pm", "task", "node", state.snapshot(), revision[0])
    save()
    return SimpleNamespace(path=path, state=state, store=store, save=save)


def receipt(checkpoint, status="completed"):
    attempt = checkpoint.state.execution_attempts["B"]
    return {**{k: attempt[k] for k in ("subtask_id", "parent_task_id", "pm_id", "reporter_id", "name")},
            "status": status, "output": {"summary": "raw private result"}}


def network(monkeypatch, result, node="executor", status_code=200):
    calls = []
    def get(url, **kwargs):
        calls.append((url, kwargs))
        return Mock(status_code=200 if url.endswith('/info') else status_code,
                    json=lambda: {"device_id": node} if url.endswith('/info') else result)
    monkeypatch.setattr("lan_mesh.pm_recovery.http_get", get)
    return calls


def assess(checkpoint, **kwargs):
    return assess_pm_recovery(checkpoint.path, "pm", "task", "node", lambda: {}, **kwargs)


@pytest.mark.parametrize("status,expected", [("completed", "receipt_completed"), ("failed", "failed"),
                                             ("started", "awaiting_result"), ("unknown", "result_unknown")])
def test_original_receipt_classification_is_read_only(checkpoint, monkeypatch, status, expected):
    calls = network(monkeypatch, receipt(checkpoint, status))
    before = checkpoint.path.read_bytes()
    result = assess(checkpoint)
    assert [s["state"] for s in result["steps"]] == ["saved_completed", expected, "unstarted"]
    assert result["resume_supported"] is False
    assert checkpoint.path.read_bytes() == before
    assert "private" not in json.dumps(result)
    assert len(calls) == 2 and calls[1][0].endswith('/tasks/executions/execution-B')
    assert all(c[1]["retries"] == 0 and c[1]["allow_redirects"] is False for c in calls)


@pytest.mark.parametrize("field", ["subtask_id", "parent_task_id", "pm_id", "reporter_id", "name"])
def test_wrong_receipt_identity_is_not_used(checkpoint, monkeypatch, field):
    response = receipt(checkpoint)
    response[field] = "other"
    network(monkeypatch, response)
    result = assess(checkpoint)
    assert result["steps"][1]["state"] == "invalid" and result["state"] == "review_required"


def test_wrong_device_prevents_receipt_query(checkpoint, monkeypatch):
    calls = network(monkeypatch, receipt(checkpoint), node="different-device")
    assert assess(checkpoint)["steps"][1]["state"] == "invalid"
    assert len(calls) == 1


@pytest.mark.parametrize("code", [404, 401, 500, 302])
def test_missing_failed_or_redirected_receipt_never_means_unstarted(checkpoint, monkeypatch, code):
    network(monkeypatch, {}, status_code=code)
    assert assess(checkpoint)["steps"][1]["state"] == "result_unknown"


def test_completed_receipt_with_model_error_is_failed(checkpoint, monkeypatch):
    result = receipt(checkpoint)
    result["output"] = {"summary": "[模型调用失败] no model"}
    network(monkeypatch, result)
    assert assess(checkpoint)["steps"][1]["state"] == "failed"


def test_local_result_missing_does_not_trigger_remote_query(checkpoint, monkeypatch):
    checkpoint.state.execution_attempts["B"]["station"] = {}
    checkpoint.save()
    get = Mock(side_effect=AssertionError("no network"))
    monkeypatch.setattr("lan_mesh.pm_recovery.http_get", get)
    assert assess(checkpoint)["steps"][1]["state"] == "result_unknown"
    get.assert_not_called()


def test_received_but_unverified_result_is_not_promoted_to_saved_output(checkpoint, monkeypatch):
    report = receipt(checkpoint)
    report["task_name"] = report.pop("name")
    checkpoint.state.execution_attempts["B"].update(phase="processing", received_result=report)
    checkpoint.save()
    get = Mock(side_effect=AssertionError("already have receipt"))
    monkeypatch.setattr("lan_mesh.pm_recovery.http_get", get)
    assert assess(checkpoint)["steps"][1]["state"] == "receipt_completed"
    assert "B" not in checkpoint.state.subtask_outputs
    get.assert_not_called()


def test_query_budget_and_snapshot_changes_are_not_completion(checkpoint, monkeypatch):
    calls = network(monkeypatch, receipt(checkpoint))
    assert assess(checkpoint, budget=0)["next_offset"] == 1
    assert not calls
    def changing_get(url, **kwargs):
        if not url.endswith('/info'):
            checkpoint.save()
        return Mock(status_code=200, json=lambda: {"device_id": "executor"} if url.endswith('/info') else receipt(checkpoint))
    monkeypatch.setattr("lan_mesh.pm_recovery.http_get", changing_get)
    result = assess(checkpoint)
    assert result["state"] == "changed" and not result["steps"]


def test_remaining_steps_can_be_checked_without_repeating_first_page(checkpoint, monkeypatch):
    calls = network(monkeypatch, receipt(checkpoint))
    first = assess(checkpoint, budget=0)
    assert first['state'] == 'partial' and first['next_offset'] == 1 and len(first['steps']) == 1
    second = assess(checkpoint, offset=first['next_offset'], expected_revision=first['revision'])
    assert [s['name'] for s in second['steps']] == ['B', 'C']
    assert second['next_offset'] is None and second['state'] == 'partial'
    checkpoint.save()
    calls.clear()
    changed = assess(checkpoint, offset=1, expected_revision=first['revision'])
    assert changed['state'] == 'changed' and not calls


@pytest.mark.parametrize("change", ["stopped", "deadline", "delivery"])
def test_stop_deadline_and_delivery_block_recovery_even_with_complete_receipts(checkpoint, monkeypatch, change):
    if change == "stopped":
        checkpoint.state.stop_event.set()
    elif change == "deadline":
        checkpoint.state.start_time = 1
    else:
        checkpoint.state.delivery = {"state": "sending"}
    checkpoint.save()
    network(monkeypatch, receipt(checkpoint))
    result = assess(checkpoint)
    assert result["state"] == "review_required" and result["blockers"]


def test_missing_checkpoint_is_not_created(tmp_path):
    path = tmp_path / "absent" / "pm.sqlite3"
    assert assess_pm_recovery(path, "pm", "task", "node", lambda: {})["state"] == "missing"
    assert not path.parent.exists()


def test_running_pm_cannot_be_assessed_as_ready(checkpoint, monkeypatch):
    get = Mock(side_effect=AssertionError("no queries while PM running"))
    monkeypatch.setattr("lan_mesh.pm_recovery.http_get", get)
    def current():
        return {"pm_id": "pm", "task_id": "task", "device_id": "node", "running": True}
    result = assess_pm_recovery(checkpoint.path, "pm", "task", "node", current)
    assert result["state"] == "running" and not result["steps"]
    get.assert_not_called()


def test_read_only_execution_api_does_not_create_missing_ledger(tmp_path):
    path = tmp_path / "absent" / "receipts.sqlite3"
    execute = Mock()
    app = FastAPI()
    app.include_router(build_execution_routes(execute, path))
    client = TestClient(app)
    assert client.get('/tasks/executions/missing').status_code == 404
    assert not path.parent.exists()
    execute.assert_not_called()


@pytest.mark.parametrize("mode", ["station", "worker"])
def test_real_http_original_pm_queries_original_executor_after_restart(controller, checkpoint, monkeypatch, mode):
    from lan_mesh.station_controller import StationController
    from lan_mesh.station_routes_worker import build_worker_routes
    from lan_mesh.worker import WorkerAgent
    from lan_mesh.worker_routes_pm import build_pm_routes
    executor_path = checkpoint.path.with_name('executions.sqlite3')
    store = ExecutionReceipts(executor_path, owner="old-executor")
    payload = {**receipt(checkpoint), "input_data": {}}
    store.claim(payload)
    store.finish(payload['subtask_id'], {"status": "completed", "output": {"summary": "private artifact"}})
    execute = Mock(side_effect=AssertionError("must never execute"))
    executor = FastAPI()
    executor.include_router(build_execution_routes(execute, executor_path))
    @executor.get('/info')
    def info():
        return {"device_id": "executor"}
    with live_server(executor) as executor_port:
        checkpoint.state.execution_attempts['B']['station']['api_port'] = executor_port
        checkpoint.save()
        app = FastAPI()
        if mode == 'station':
            remote = StationController.__new__(StationController)
            remote.db = SimpleNamespace(path=checkpoint.path.with_name('pm-source.sqlite3'))
            remote.state = SimpleNamespace(device_id='node')
            remote.discovery = Mock()
            remote._local_pm_agent = None
            target = checkpoint.path.with_name('pm-source-pm-starts.sqlite3')
            target.write_bytes(checkpoint.path.read_bytes())
            app.include_router(build_worker_routes(remote))
        else:
            remote = WorkerAgent.__new__(WorkerAgent)
            remote.data_dir = checkpoint.path.parent
            remote.state = SimpleNamespace(device_id='node', pm_agent=None)
            target = checkpoint.path.with_name('worker-pm-starts.sqlite3')
            target.write_bytes(checkpoint.path.read_bytes())
            app.include_router(build_pm_routes(remote))
        with live_server(app) as pm_port:
            controller.db.save_task(Task(task_id='task', status='running', pm_agent_id='pm'))
            controller.db.upsert_pm_agent(PMAgent(pm_id='pm', task_id='task', device_id='node', ip='127.0.0.1', api_port=pm_port))
            before = target.read_bytes(), executor_path.read_bytes(), controller.db.get_task('task').to_dict()
            result = client_for(controller).get('/api/tasks/task/recovery-check')
            assert result.status_code == 200
            assert result.json()['state'] == 'evidence_complete'
            assert result.json()['steps'][1]['state'] == 'receipt_completed'
            assert 'private artifact' not in result.text
            assert before == (target.read_bytes(), executor_path.read_bytes(), controller.db.get_task('task').to_dict())
    execute.assert_not_called()


def test_task_changes_during_query_discard_evidence(controller, checkpoint, monkeypatch):
    controller.db.save_task(Task(task_id='task', status='running', pm_agent_id='pm'))
    controller.db.upsert_pm_agent(PMAgent(pm_id='pm', task_id='task', device_id='self'))
    def assess_local(*args, **kwargs):
        controller.db.begin_task_control('task', 'cancel')
        return {"format": 1, "device_id": "self", "pm_id": "pm", "task_id": "task",
                "state": "evidence_complete", "steps": [], "blockers": []}
    controller._local_assess_pm_recovery = assess_local
    result = assess_task_recovery(controller, 'task')
    assert result['state'] == 'changed' and not result['steps']


def test_terminal_delivered_and_replaced_task_cannot_be_ready(controller):
    controller.db.save_task(Task(task_id='task', status='failed', pm_agent_id='pm', output_data={'_delivery': {}}))
    controller.db.upsert_pm_agent(PMAgent(pm_id='pm', task_id='task', device_id='self'))
    controller._local_assess_pm_recovery = lambda *args, **kwargs: {"format": 1, "device_id": "self", "pm_id": "pm", "task_id": "task",
        "state": "evidence_complete", "steps": [], "blockers": []}
    result = assess_task_recovery(controller, 'task')
    assert result['state'] == 'review_required' and len(result['blockers']) == 2
    assert client_for(controller).get('/api/tasks/missing/recovery-check').status_code == 404
    controller.secretary_active = False
    assert client_for(controller).get('/api/tasks/task/recovery-check').status_code == 503


def test_original_task_with_replacement_cannot_be_assessed_as_ready(controller):
    from lan_mesh.task_resubmission import prepare_resubmission
    controller.db.save_task(Task(task_id='task', status='failed', pm_agent_id='pm'))
    controller.db.upsert_pm_agent(PMAgent(pm_id='pm', task_id='task', device_id='self', status='failed'))
    prepare_resubmission(controller, 'task', True)
    controller.db.update_task_status('task', 'running')
    controller._local_assess_pm_recovery = lambda *args, **kwargs: {"format": 1, "device_id": "self", "pm_id": "pm", "task_id": "task",
        "state": "evidence_complete", "steps": [], "blockers": []}
    result = assess_task_recovery(controller, 'task')
    assert result['state'] == 'review_required' and '替代任务' in result['blockers'][0]


@pytest.mark.parametrize('direct_state,result,expected', [
    ('active', {}, 'result_unknown'),
    ('returned', {'status': 'completed', 'summary': 'done'}, 'receipt_completed'),
    ('returned', {'status': 'failed', 'summary': 'failed'}, 'failed'),
    ('returned', {'status': 'completed', 'summary': '[模型调用失败] no model'}, 'failed'),
])
def test_direct_execution_is_not_reissued_or_mistaken_for_unstarted(checkpoint, monkeypatch, direct_state, result, expected):
    checkpoint.state.plan = {'pattern': 'single', 'decomposition': [{'name': 'single'}]}
    checkpoint.state.direct_execution = {'state': direct_state, 'result': result, 'execution_id': 'single-execution'}
    checkpoint.save()
    calls = network(monkeypatch, {})
    assert assess(checkpoint)['steps'][0]['state'] == expected
    assert not calls
