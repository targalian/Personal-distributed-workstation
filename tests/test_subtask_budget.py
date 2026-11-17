"""Subtask budgets survive transport and stop execution before tool side effects."""

import json
import asyncio
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from lan_mesh.agent_runtime import AgentRuntime, infer_result_status
from lan_mesh.pm_dispatcher import PMDispatcher
from lan_mesh.pm_monitor import PMMonitor
from lan_mesh.pm_state import PMState
from lan_mesh.protocol import SubTask
from lan_mesh.tool_registry import ToolRegistry


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr('lan_mesh.runtime_trace.trace_subtask_start', lambda **kw: 'budget-test')
    monkeypatch.setattr('lan_mesh.runtime_trace.trace_subtask_end', lambda *a, **kw: None)
    value = AgentRuntime('budget-test', str(tmp_path))
    value._skills_cache_dir = tmp_path / 'skills'
    yield value
    if hasattr(value, 'services'):
        value.services.close()


def tool_response(input_tokens=1, output_tokens=1):
    return {'model': 'fake', 'input_tokens': input_tokens, 'output_tokens': output_tokens,
            'message': {'content': '', 'tool_calls': [
                {'id': 'write', 'type': 'function', 'function': {
                    'name': 'file_write', 'arguments': '{"path":"unwanted","content":"x"}'}}]}}


@pytest.mark.parametrize('limits,iterations', [({}, 30), ({'max_steps': 2}, 2)])
def test_step_limit_and_legacy_default(runtime, monkeypatch, limits, iterations):
    model, tool = Mock(side_effect=lambda *a: tool_response()), Mock(return_value={'content': []})
    monkeypatch.setattr(runtime, '_call_llm_messages_with_tools', model)
    monkeypatch.setattr(ToolRegistry, 'call_tool', tool)
    result = runtime.execute({'required_skill': 'react_agent', 'budget': limits})
    assert result['status'] == 'failed'
    assert result['output']['stop_reason'] == 'iteration_limit'
    assert model.call_count == tool.call_count == iterations


@pytest.mark.parametrize('message', [tool_response(60, 40), {
    'model': 'fake', 'input_tokens': 60, 'output_tokens': 40, 'message': {'content': 'done'}}])
def test_token_limit_stops_before_tools_or_success(runtime, monkeypatch, message):
    model, tool = Mock(return_value=message), Mock()
    monkeypatch.setattr(runtime, '_call_llm_messages_with_tools', model)
    monkeypatch.setattr(ToolRegistry, 'call_tool', tool)
    result = runtime.execute({'required_skill': 'react_agent', 'budget': {'max_tokens': 100}})
    assert result['status'] == 'failed'
    assert result['output']['stop_reason'] == 'budget_exceeded'
    assert '100/100' in result['error']
    assert result['usage']['input_tokens'] == 60
    assert result['usage']['output_tokens'] == 40
    model.assert_called_once()
    tool.assert_not_called()
    assert not Path(runtime.shared_folder, 'unwanted').exists()


def test_tokens_accumulate_across_model_calls(runtime, monkeypatch):
    model = Mock(side_effect=[tool_response(30, 20), tool_response(30, 20)])
    tool = Mock(return_value={'content': []})
    monkeypatch.setattr(runtime, '_call_llm_messages_with_tools', model)
    monkeypatch.setattr(ToolRegistry, 'call_tool', tool)
    result = runtime.execute({'required_skill': 'react_agent', 'budget': {'max_tokens': 100}})
    assert result['output']['stop_reason'] == 'budget_exceeded'
    assert model.call_count == 2 and tool.call_count == 1


@pytest.mark.parametrize('field', ['error_class', 'stop_reason', 'status'])
def test_budget_failure_status(field):
    assert infer_result_status({field: 'budget_exceeded'})[0] == 'failed'


def test_protocol_budget_roundtrip_and_independent_default():
    first, second = SubTask(), SubTask()
    first.budget['max_steps'] = 2
    assert second.budget == {}
    assert SubTask.from_dict(json.loads(json.dumps(first.to_dict()))).budget == first.budget


def test_execution_injects_budget_without_mutating_input(runtime):
    seen = []
    runtime._handlers['budget_probe'] = lambda data: seen.append(data) or {'result': 'ok'}
    data = {'requirement': 'work', '_budget': {'max_steps': 99}}
    limits = {'max_steps': 2, 'max_tokens': 100, 'budget_usd': 0.5}
    assert runtime.execute({'required_skill': 'budget_probe', 'input_data': data,
                            'budget': limits})['status'] == 'completed'
    assert seen[0]['_budget'] == limits
    assert data['_budget'] == {'max_steps': 99}


def test_dispatch_payload_roundtrips_budget_to_runtime(runtime, monkeypatch):
    limits = {'max_steps': 2, 'max_tokens': 100, 'budget_usd': 0.5}
    state, agent = PMState(), SimpleNamespace()
    dispatcher = PMDispatcher('pm', runtime, 'http://unused', 'device', state, agent)
    monkeypatch.setattr(dispatcher, '_record_subtask_start', Mock())
    post = Mock(return_value=SimpleNamespace(status_code=200, json=lambda: {'status': 'started'}))
    monkeypatch.setattr('lan_mesh.pm_dispatcher.requests.post', post)
    dispatcher.dispatch_subtask({'ip': '127.0.0.1', 'api_port': 1}, {'agent_id': 'a'},
                                {'task_id': 'task'}, {'name': 'work', 'skill': 'probe', 'budget': limits})
    payload = json.loads(json.dumps(post.call_args.kwargs['json']))
    assert payload['budget'] == limits
    seen = []
    runtime._handlers['probe'] = lambda data: seen.append(data) or {}
    from lan_mesh.station_routes_worker import build_worker_routes
    from lan_mesh.station_local_pm import StationLocalPmMixin
    controller = SimpleNamespace(db=Mock(), state=SimpleNamespace(),
                                 _local_sub_agents={}, chat_runtime=runtime,
                                 _report_subtask_result=Mock())
    controller._local_execute_task = lambda data: StationLocalPmMixin._local_execute_task(controller, data)
    route = next(r for r in build_worker_routes(controller).routes if r.path == '/tasks/execute')
    class InlineThread:
        def __init__(self, target, **kwargs):
            self.target = target

        def start(self):
            self.target()

    monkeypatch.setattr('threading.Thread', InlineThread)
    assert asyncio.run(route.endpoint(payload))['status'] == 'started'
    assert seen[0]['_budget'] == limits


def test_durable_dispatch_budget_is_sealed_and_local_execution_keeps_it(monkeypatch):
    from lan_mesh.delivery_protocol import validate_payload
    limits = {'max_steps': 2, 'max_tokens': 100, 'budget_usd': 0.5}
    state = PMState()
    runtime = SimpleNamespace(execute=Mock(return_value={'status': 'completed', 'output': {}}))
    agent = SimpleNamespace(_delivery_owner=SimpleNamespace(state=SimpleNamespace(api_port=8080)),
                            persist_delivery_state=Mock(), sync_subtasks=Mock(),
                            receive_subtask_result=Mock(), report_progress=Mock())
    dispatcher = PMDispatcher('pm', runtime, 'http://unused', 'device', state, agent)
    monkeypatch.setattr(dispatcher, '_record_subtask_start', Mock())
    delivery = Mock()
    monkeypatch.setattr('lan_mesh.mesh_delivery_service.call_delivery', delivery)
    station = {'ip': '127.0.0.1', 'api_port': 1}
    task, sub = {'task_id': 'task'}, {'name': 'work', 'budget': limits}
    dispatcher.dispatch_subtask(station, {'agent_id': 'a', 'delivery_protocol': 1}, task, sub)
    payload = json.loads(json.dumps(delivery.call_args.kwargs['payload']))
    validate_payload(payload, 'task')
    assert payload['budget'] == limits
    dispatcher.execute_subtask_locally(task, sub)
    assert runtime.execute.call_args.args[0]['budget'] == limits


def test_cli_passes_usd_budget_to_harness(runtime, monkeypatch):
    from lan_mesh import agent_runtime as policy
    requests = []
    def stream(request):
        requests.append(request)
        yield SimpleNamespace(kind='run.completed', data={
            'status': 'completed', 'usage_known': False, 'elapsed_secs': 0})
    monkeypatch.setattr(policy, 'resolve_cli_backend', lambda _: ('codex', None))
    monkeypatch.setattr(policy, 'validate_cli_agent_cwd', lambda *a: '')
    monkeypatch.setattr(policy, '_which_with_fallback', lambda _: None)
    monkeypatch.setattr('lan_mesh.harness.registry.get', lambda _: SimpleNamespace(stream=stream))
    monkeypatch.setattr('lan_mesh.runtime_usage.record_external_usage', Mock())
    runtime.execute({'required_skill': 'cli_agent', 'budget': {'budget_usd': 0.25},
                     'input_data': {'requirement': 'work', 'budget_usd': 9}})
    assert requests[0]['budget_usd'] == 0.25


@pytest.mark.parametrize('via_report', [False, True])
@pytest.mark.parametrize('valid_replan', [False, True])
def test_budget_failure_bypasses_retries_and_keeps_replan_bound(monkeypatch, via_report, valid_replan):
    state = PMState(task={'task_id': 'task'})
    state.plan = {'decomposition': [{'name': 'work', 'skill': 'react_agent', 'depends_on': []}]}
    state.subagents['agent'] = {'current_task': 'work', 'status': 'busy'}
    runtime = SimpleNamespace(_call_llm_with_routing=Mock(return_value={
        'content': json.dumps([{'name': 'smaller', 'description': 'small step',
                               'skill': 'react_agent', 'depends_on': []}]) if valid_replan else '[]'}))
    agent = SimpleNamespace(running=True, sync_subtasks=Mock(), report_status=Mock(),
                            report_progress=Mock())
    dispatcher = Mock()
    monitor = PMMonitor('pm', runtime, 'http://unused', state, agent, dispatcher)
    monitor._report_escalation = Mock()
    monkeypatch.setattr('lan_mesh.runtime_trace.trace_task_event', Mock())
    previous = deepcopy(state.retry_counts)
    if via_report:
        monitor.receive_progress_report({'reporter_id': 'agent', 'task_name': 'work',
            'status': 'failed', 'output': {'stop_reason': 'budget_exceeded'}, 'message': 'limit'})
    else:
        monitor.handle_subagent_failure('work', 'budget_exceeded')
    assert state.retry_counts.get('work') == previous.get('work')
    assert state.replan_counts == {'work': 1}
    runtime._call_llm_with_routing.assert_called_once()
    assert ('smaller' in [s['name'] for s in state.plan['decomposition']]) == valid_replan
    assert monitor._report_escalation.called == (not valid_replan)
    dispatcher.get_available_stations.assert_not_called()
    assert all(call.args[1]['name'] != 'work'
               for call in dispatcher.execute_subtask_locally.call_args_list)
    monitor.handle_subagent_failure('smaller' if valid_replan else 'work', 'budget_exceeded')
    assert state.replan_counts == {'work': 1}
    runtime._call_llm_with_routing.assert_called_once()
