"""Exercise PM worker and monitor threads through completion and cooperative stop."""
import threading
from unittest.mock import Mock

import pytest

from lan_mesh.pm_agent import ProjectManagerAgent


@pytest.fixture
def pm(monkeypatch):
    runtime = Mock()
    runtime.execute.return_value = {'status': 'completed', 'output': {'summary': 'done'}}
    runtime._call_llm_with_routing.return_value = {'content': '完整交付产物'}
    agent = ProjectManagerAgent('pm-life', runtime, 'http://unused', 'self')
    calls = []
    def post(url, **kwargs):
        calls.append((url, kwargs.get('json', {})))
        return Mock(status_code=200, json=lambda: {'ok': True, 'task_id': 'task-life'})
    monkeypatch.setattr('lan_mesh.pm_agent.http_post', post)
    agent._planner.load_skill = Mock()
    agent._planner.refine_requirements = lambda task: task
    agent._planner.analyze_with_skill = lambda task: {
        'pattern': 'orchestrator', 'decomposition': [
            {'name': 'B', 'depends_on': ['A']}, {'name': 'A', 'depends_on': []}]}
    agent._dispatcher.get_available_stations = lambda: []
    agent._distribute_artifacts = Mock()
    agent._record_task_memory = Mock()
    agent._monitor.poll_interval = 0.005
    agent.test_calls = calls
    yield agent
    agent.stop()
    for thread in (agent._thread, agent._progress_thread):
        if thread is not None:
            thread.join(timeout=2)
            assert not thread.is_alive()


def task():
    return {'task_id': 'task-life', 'name': '线程测试', 'description': '完整任务要求', 'input_data': {}}


def join_task(pm):
    pm._thread.join(timeout=2)
    pm._progress_thread.join(timeout=2)
    assert not pm._thread.is_alive() and not pm._progress_thread.is_alive()


def test_local_dependencies_and_monitor_reach_delivery(pm):
    observed = []
    def execute(subtask):
        observed.append(subtask['name'])
        if subtask['name'] == 'B':
            assert subtask['input_data']['_dependency_outputs']['A'] == {'summary': 'A done'}
        return {'status': 'completed', 'output': {'summary': subtask['name']+' done'}}
    pm.runtime.execute.side_effect = execute
    pm.start_task(task())
    join_task(pm)
    assert observed == ['A', 'B']
    assert len(pm._state.subagents) == 2
    assert not pm.running
    assert sum(url.endswith('/deliver') for url, _ in pm.test_calls) == 1
    assert pm.test_calls[-1][1]['status'] == 'completed'


def test_remote_creation_failure_preserves_local_dependency_order(pm):
    pm._dispatcher.get_available_stations = lambda: [{'device_id': 'remote'}]
    pm._dispatcher._create_subagent_on_station = Mock(return_value=None)
    pm.start_task(task())
    join_task(pm)
    assert [call.args[0]['name'] for call in pm.runtime.execute.call_args_list] == ['A', 'B']
    assert pm.runtime.execute.call_args_list[1].args[0]['input_data']['_dependency_outputs']['A']
    assert sum(url.endswith('/deliver') for url, _ in pm.test_calls) == 1


def test_dispatch_thread_exit_keeps_monitor_waiting_for_remote_result(pm):
    registered = threading.Event()
    def dispatch(task, plan):
        with pm._state.lock:
            pm._state.subagents['worker'] = {'agent_id': 'worker', 'current_task': 'A', 'status': 'busy'}
        registered.set()
    pm._planner.analyze_with_skill = lambda task: {'pattern': 'orchestrator', 'decomposition': [{'name': 'A'}]}
    pm._dispatcher.create_team_and_dispatch = dispatch
    pm.start_task(task())
    assert registered.wait(1)
    pm._thread.join(timeout=1)
    assert not pm._thread.is_alive()
    assert pm.running and pm._progress_thread.is_alive()
    pm.receive_subtask_result('A', 'completed', {'summary': 'remote done'}, 'worker')
    join_task(pm)
    assert sum(url.endswith('/deliver') for url, _ in pm.test_calls) == 1


@pytest.mark.parametrize('action', ['cancel', 'pause', 'stop'])
@pytest.mark.parametrize('stage', ['planning', 'single', 'aggregation'])
def test_stop_during_blocking_stage_discards_late_result(pm, action, stage):
    entered, release = threading.Event(), threading.Event()
    def blocking(result):
        entered.set()
        assert release.wait(2)
        return result
    if stage == 'planning':
        pm._planner.analyze_with_skill = lambda task: blocking({'pattern': 'single'})
    elif stage == 'single':
        pm._planner.analyze_with_skill = lambda task: {'pattern': 'single'}
        pm._planner.execute_directly = lambda task: blocking({'status': 'completed', 'summary': 'late'})
    else:
        pm.runtime._call_llm_with_routing.side_effect = lambda *args: blocking({'content': 'late'})
    pm.start_task(task())
    try:
        assert entered.wait(1)
        getattr(pm, action)()
        call_count = len(pm.test_calls)
    finally:
        release.set()
    join_task(pm)
    assert not any(url.endswith('/deliver') for url, _ in pm.test_calls)
    assert not any(payload.get('status') in ('completed', 'executing', 'monitoring', 'failed')
                   for _, payload in pm.test_calls[call_count:])
    pm._record_task_memory.assert_not_called()


def test_cancel_wakes_clarification_without_planning(pm):
    pm._planner.refine_requirements = lambda task: (pm.request_clarification('确认需求', timeout=30), task)[1]
    pm._planner.analyze_with_skill = Mock()
    waiting = threading.Event()
    original = pm.report_progress
    def progress(*args, **kwargs):
        if args[1] == 'awaiting_input':
            waiting.set()
        original(*args, **kwargs)
    pm.report_progress = progress
    pm.start_task(task())
    assert waiting.wait(1)
    pm.cancel()
    join_task(pm)
    pm._planner.analyze_with_skill.assert_not_called()


def test_cancel_ignores_late_child_completion_and_failure(pm):
    pm.cancel()
    pm._monitor.handle_subagent_failure = Mock()
    pm._dispatcher.try_dispatch_pending = Mock()
    pm.receive_subtask_result('A', 'completed', {'summary': 'late'}, 'worker')
    pm.receive_subtask_result('A', 'failed', {'error': 'late'}, 'worker')
    assert not pm._state.subtask_outputs
    pm._dispatcher.try_dispatch_pending.assert_not_called()
    pm._monitor.handle_subagent_failure.assert_not_called()


def test_cancel_during_local_execution_stops_dependent_dispatch(pm):
    entered, release = threading.Event(), threading.Event()
    def execute(subtask):
        entered.set()
        assert release.wait(2)
        return {'status': 'completed', 'output': {'summary': 'late'}}
    pm.runtime.execute.side_effect = execute
    pm.start_task(task())
    try:
        assert entered.wait(1)
        pm.cancel()
    finally:
        release.set()
    join_task(pm)
    assert pm.runtime.execute.call_count == 1
    assert not pm._state.subtask_outputs
    assert 'B' in pm._state.pending_subtasks


def test_pm_instance_cannot_start_another_task_while_prior_thread_returns(pm):
    pm.start_task(task())
    with pytest.raises(RuntimeError):
        pm.start_task(task())
    join_task(pm)


def test_global_timeout_wakes_waiting_clarification_and_prevents_planning(pm):
    pm._state.global_timeout = 0.03
    pm._planner.refine_requirements = lambda task: (pm.request_clarification('等候', timeout=30), task)[1]
    pm._planner.analyze_with_skill = Mock()
    pm.start_task(task())
    join_task(pm)
    assert pm._state.stop_reason == 'failed'
    pm._planner.analyze_with_skill.assert_not_called()
    assert any(payload.get('status') == 'failed' for _, payload in pm.test_calls)


def test_monitor_does_not_aggregate_while_dispatch_still_builds_team(pm):
    entered, release, tick = threading.Event(), threading.Event(), threading.Event()
    timed_out = pm._monitor.is_global_timed_out
    def check_timeout():
        if entered.is_set():
            tick.set()
        return timed_out()
    pm._monitor.is_global_timed_out = check_timeout
    def dispatch(task, plan):
        pm._state.subtask_outputs.update(A={'summary': 'done'}, B={'summary': 'done'})
        entered.set()
        assert release.wait(2)
    pm._dispatcher.create_team_and_dispatch = dispatch
    pm.start_task(task())
    try:
        assert entered.wait(1)
        assert tick.wait(1)
        assert not pm._state.monitoring_ready.is_set()
        assert not any(url.endswith('/deliver') for url, _ in pm.test_calls)
    finally:
        release.set()
    join_task(pm)
    assert sum(url.endswith('/deliver') for url, _ in pm.test_calls) == 1
