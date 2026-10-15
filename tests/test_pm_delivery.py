"""Delivery outcomes must reflect persisted acknowledgements, not child progress or model errors."""
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import urlparse

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lan_mesh.database import Database
from lan_mesh.pm_agent import ProjectManagerAgent
from lan_mesh.protocol import PMAgent, Task
from lan_mesh.station_routes_pm import build_pm_routes
from lan_mesh.station_routes_tasks import build_task_routes


@pytest.fixture
def execution(tmp_path, monkeypatch):
    db = Database(str(tmp_path / 'tasks.sqlite3'))
    task = Task(task_id='task-test', name='验收示例', description='输出完整的项目说明与验证证据。',
                status='running', pm_agent_id='pm-test')
    db.save_task(task)
    db.upsert_pm_agent(PMAgent(pm_id='pm-test', task_id=task.task_id))
    controller = SimpleNamespace(db=db, state=SimpleNamespace(ws_clients=set()),
                                 secretary_active=True, discovery=Mock(), bot_gateway=Mock())
    app = FastAPI()
    app.include_router(build_task_routes(controller))
    app.include_router(build_pm_routes(controller))
    client = TestClient(app)
    runtime = Mock()
    runtime._call_llm_with_routing.return_value = {'content': '经过聚合的完整交付内容'}
    agent = ProjectManagerAgent('pm-test', runtime, 'http://secretary', 'worker-test')
    agent._state.task = task.to_dict()
    agent._state.plan = {'pattern': 'orchestrator', 'decomposition': [{'name': 'A'}]}
    agent._state.subtask_outputs = {'A': '完成的子任务结果'}
    agent._state.subagents = {'member-a': {'current_task': 'A', 'status': 'completed'}}
    agent._distribute_artifacts = Mock()
    agent._record_task_memory = Mock()
    agent.running = True
    requests = []

    def post(url, **kwargs):
        path = urlparse(url).path
        requests.append((path, kwargs.get('json', {})))
        return client.post(path, json=kwargs.get('json', {}))

    monkeypatch.setattr('lan_mesh.pm_agent.http_post', post)
    return SimpleNamespace(db=db, client=client, agent=agent, runtime=runtime,
                           task=task, requests=requests, post=post, controller=controller)


def assert_no_success(execution):
    assert not any(payload.get('status') == 'completed' for _, payload in execution.requests)
    assert execution.db.get_task('task-test').status == 'failed'
    execution.agent._record_task_memory.assert_not_called()


@pytest.mark.parametrize('response', [
    {'content': '[模型调用失败] 降级链不可用'},
    {'content': '  [LLM 调用失败] timeout'},
    {'content': '[未配置 LLM API Key]'},
    {'content': '看似有效的文本', '_error': True},
    {'content': '看似有效的文本', 'error': 'failed'},
    {'content': 'fallback error', 'model': 'none'},
    {'content': '   '},
    {'content': None},
])
def test_invalid_aggregation_never_becomes_delivery(execution, response):
    execution.runtime._call_llm_with_routing.return_value = response
    execution.agent._monitor.aggregate_results()
    assert_no_success(execution)
    assert '_delivery' not in execution.db.get_task('task-test').output_data
    assert '_aggregated' not in execution.agent._state.subtask_outputs
    assert execution.agent._state.subtask_outputs['A'] == '完成的子任务结果'
    assert execution.agent.running is False


def test_aggregation_exception_is_failed_without_delivering_error_text(execution):
    execution.runtime._call_llm_with_routing.side_effect = TimeoutError('model timeout')
    execution.agent._monitor.aggregate_results()
    assert_no_success(execution)
    assert not any(path.endswith('/deliver') for path, _ in execution.requests)


@pytest.mark.parametrize('outcome', ['http503', 'invalid_json', 'wrong_task', 'not_ok', 'timeout'])
def test_delivery_requires_matching_acknowledgement(execution, monkeypatch, outcome):
    def post(url, **kwargs):
        if url.endswith('/deliver'):
            if outcome == 'timeout':
                raise TimeoutError('unknown outcome')
            response = Mock(status_code=503 if outcome == 'http503' else 200)
            if outcome == 'invalid_json':
                response.json.side_effect = ValueError('bad response')
            else:
                response.json.return_value = {'ok': outcome != 'not_ok', 'task_id': 'other'}
            return response
        return execution.post(url, **kwargs)
    monkeypatch.setattr('lan_mesh.pm_agent.http_post', post)
    execution.agent._monitor.aggregate_results()
    assert_no_success(execution)
    execution.agent._distribute_artifacts.assert_called_once()
    assert execution.agent._state.subtask_outputs['_aggregated'] == '经过聚合的完整交付内容'


def test_delivery_ack_lost_after_commit_does_not_claim_success(execution, monkeypatch):
    def post(url, **kwargs):
        response = execution.post(url, **kwargs)
        if url.endswith('/deliver'):
            assert response.status_code == 200
            raise TimeoutError('ack lost after commit')
        return response
    monkeypatch.setattr('lan_mesh.pm_agent.http_post', post)
    execution.agent._monitor.aggregate_results()
    assert_no_success(execution)
    assert execution.db.get_task('task-test').output_data['_delivery']['accepted'] is None


def test_delivery_storage_failure_cannot_be_acknowledged(execution, monkeypatch):
    execution.db._get_conn().executescript(
        "CREATE TRIGGER fail_delivery BEFORE UPDATE OF output_data ON tasks "
        "BEGIN SELECT RAISE(ABORT, 'simulated disk failure'); END;")
    execution.agent._monitor.aggregate_results()
    assert_no_success(execution)
    assert '_delivery' not in execution.db.get_task('task-test').output_data
    execution.controller.bot_gateway.notify.assert_not_called()


def test_aggregation_delivery_is_persisted_before_parent_completes(execution):
    execution.agent._monitor.aggregate_results()
    task = execution.db.get_task('task-test')
    assert task.status == 'completed'
    assert task.output_data['_delivery']['deliverable'] == '经过聚合的完整交付内容'
    paths = [path for path, _ in execution.requests]
    delivery_index = paths.index('/api/pm/pm-test/deliver')
    assert all(payload.get('status') != 'completed'
               for _, payload in execution.requests[:delivery_index])
    execution.agent._record_task_memory.assert_called_once()
    assert execution.client.post('/api/tasks/task-test/accept').status_code == 200
    assert execution.db.get_task('task-test').output_data['_delivery']['accepted'] is True


@pytest.mark.parametrize('acknowledged', [True, False])
def test_single_execution_completion_depends_on_delivery(execution, monkeypatch, acknowledged):
    agent = execution.agent
    agent._planner.refine_requirements = lambda task: task
    agent._planner.analyze_with_skill = lambda task: {'pattern': 'single', 'decomposition': [{'name': 'A'}]}
    agent._planner.execute_directly = lambda task: {'status': 'completed', 'summary': '单任务交付内容'}
    if not acknowledged:
        def post(url, **kwargs):
            if url.endswith('/deliver'):
                raise ConnectionError('offline')
            return execution.post(url, **kwargs)
        monkeypatch.setattr('lan_mesh.pm_agent.http_post', post)
    agent._run_task(execution.task.to_dict())
    if acknowledged:
        task = execution.db.get_task('task-test')
        assert task.status == 'completed'
        assert task.output_data['_delivery']['deliverable'] == '单任务交付内容'
    else:
        assert_no_success(execution)


def test_local_child_completion_does_not_complete_parent(execution):
    execution.agent._state.subtask_outputs.clear()
    execution.runtime.execute.return_value = {'status': 'completed', 'output': {'summary': '子任务完成'}}
    execution.agent.receive_subtask_result = Mock()
    execution.agent._dispatcher.execute_subtask_locally(execution.task.to_dict(), {'name': 'A'})
    assert execution.db.get_task('task-test').status == 'running'
    report = execution.requests[-1][1]
    assert report['status'] == 'completed' and report['reporter_type'] == 'member'


def test_progress_does_not_complete_parent_before_aggregation(execution, monkeypatch):
    execution.agent._monitor.poll_interval = 0.001
    execution.agent._state.monitoring_ready.set()
    def aggregate():
        assert execution.db.get_task('task-test').status == 'running'
        assert not any(payload.get('status') == 'completed' for _, payload in execution.requests)
        execution.agent.running = False
    execution.agent._monitor.aggregate_results = aggregate
    execution.agent._monitor.progress_loop()


@pytest.mark.parametrize('task_id,pm_id,content,status', [
    ('missing', 'pm-test', 'result', 404),
    ('task-test', 'wrong-pm', 'result', 409),
    ('task-test', 'pm-test', ' ', 422),
])
def test_delivery_endpoint_does_not_ack_unstored_content(execution, task_id, pm_id, content, status):
    response = execution.client.post(f'/api/pm/{pm_id}/deliver',
                                     json={'task_id': task_id, 'deliverable': content})
    assert response.status_code == status
    assert '_delivery' not in execution.db.get_task('task-test').output_data
    execution.controller.bot_gateway.notify.assert_not_called()


@pytest.mark.parametrize('status', ['cancelled', 'paused'])
def test_stopped_task_rejects_late_delivery_and_pm_status(execution, status):
    execution.db.update_task_status('task-test', status)
    execution.db.update_pm_status('pm-test', status)
    response = execution.client.post('/api/pm/pm-test/deliver',
                                     json={'task_id': 'task-test', 'deliverable': 'late output'})
    assert response.status_code == 409
    response = execution.client.post('/api/pm/pm-test/status', json={'status': 'completed'})
    assert response.json()['ignored'] is True
    assert execution.db.get_pm_agent('pm-test').status == status
    assert execution.db.get_task('task-test').status == status
    assert '_delivery' not in execution.db.get_task('task-test').output_data


def test_threaded_local_plan_reaches_persisted_delivery(execution):
    agent = execution.agent
    agent._state.subtask_outputs.clear()
    agent._state.subagents.clear()
    agent._planner.load_skill = Mock()
    agent._planner.refine_requirements = lambda task: task
    agent._planner.analyze_with_skill = lambda task: {
        'pattern': 'orchestrator', 'decomposition': [{'name': 'B', 'depends_on': ['A']}, {'name': 'A'}]}
    agent._dispatcher.get_available_stations = lambda: []
    execution.runtime.execute.return_value = {'status': 'completed', 'output': {'summary': 'done'}}
    agent._monitor.poll_interval = 0.005
    try:
        agent.start_task(execution.task.to_dict())
        agent._thread.join(timeout=5)
        agent._progress_thread.join(timeout=5)
        assert not agent._thread.is_alive() and not agent._progress_thread.is_alive()
        task = execution.db.get_task('task-test')
        assert task.status == 'completed'
        assert task.output_data['_delivery']['deliverable'] == '经过聚合的完整交付内容'
        assert [call.args[0]['name'] for call in execution.runtime.execute.call_args_list] == ['A', 'B']
        assert sum(path.endswith('/deliver') for path, _ in execution.requests) == 1
    finally:
        agent.stop()
        for thread in (agent._thread, agent._progress_thread):
            if thread:
                thread.join(timeout=1)


def test_final_failure_persists_blocked_dependencies_without_delivery(execution):
    agent = execution.agent
    agent._state.subtask_outputs.clear()
    agent._state.subagents.clear()
    agent._planner.load_skill = Mock()
    agent._planner.refine_requirements = lambda task: task
    agent._planner.analyze_with_skill = lambda task: {
        'pattern': 'orchestrator', 'decomposition': [{'name': 'B', 'depends_on': ['A']}, {'name': 'A'}]}
    agent._dispatcher.get_available_stations = lambda: []
    execution.runtime.execute.return_value = {'status': 'failed', 'error': '实际失败原因', 'output': {}}
    agent._monitor.poll_interval = 0.005
    try:
        agent.start_task(execution.task.to_dict())
        agent._thread.join(timeout=5)
        agent._progress_thread.join(timeout=5)
        assert not agent._thread.is_alive() and not agent._progress_thread.is_alive()
        task = execution.db.get_task('task-test')
        assert task.status == 'failed'
        assert '_delivery' not in task.output_data
        subtasks = {sub['name']: sub for sub in task.subtasks}
        assert subtasks['A']['status'] == 'failed' and '实际失败原因' in subtasks['A']['error']
        assert subtasks['B']['status'] == 'blocked'
        assert 'A' in subtasks['B']['error']
        assert execution.runtime.execute.call_count == 3
    finally:
        agent.stop()
        for thread in (agent._thread, agent._progress_thread):
            if thread:
                thread.join(timeout=1)


def test_incomplete_results_cannot_be_aggregated_into_success(execution):
    execution.agent._state.plan['decomposition'].append({'name': 'missing'})
    execution.agent._monitor.aggregate_results()
    execution.runtime._call_llm_with_routing.assert_not_called()
    assert execution.db.get_task('task-test').status == 'failed'
    assert '_delivery' not in execution.db.get_task('task-test').output_data
