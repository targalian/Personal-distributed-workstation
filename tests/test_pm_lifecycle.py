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
        pm._dispatcher._begin_attempt("A", task, "worker", {})
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
    pm.receive_subtask_result('A', 'completed', {'summary': 'remote done'}, 'worker',
                              execution_id=pm._state.execution_attempts['A']['subtask_id'])
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


@pytest.mark.parametrize('failure', ['result', 'exception', 'model_error'])
def test_local_failure_retries_are_bounded_and_dependents_stop(pm, failure):
    if failure == 'exception':
        pm.runtime.execute.side_effect = RuntimeError('执行异常')
    elif failure == 'model_error':
        pm.runtime.execute.return_value = {'status': 'completed', 'output': {'code': '[模型调用失败] offline'}}
    else:
        pm.runtime.execute.return_value = {'status': 'failed', 'error': '执行失败', 'output': {}}
    pm.start_task(task())
    join_task(pm)
    assert pm.runtime.execute.call_count == 3
    assert {call.args[0]['name'] for call in pm.runtime.execute.call_args_list} == {'A'}
    assert 'A' in pm._state.terminal_failures
    assert 'B' in pm._state.blocked_subtasks
    assert not pm._state.pending_subtasks
    assert not any(url.endswith('/deliver') for url, _ in pm.test_calls)
    snapshot = {item['name']: item for item in pm._build_subtask_status()}
    assert snapshot['A']['status'] == 'failed'
    assert snapshot['B']['status'] == 'blocked' and snapshot['B']['error']


def test_successful_retry_unblocks_dependency_without_failure_escalation(pm):
    pm.runtime.execute.side_effect = [
        {'status': 'failed', 'error': 'temporary', 'output': {}},
        {'status': 'completed', 'output': {'summary': 'A'}},
        {'status': 'completed', 'output': {'summary': 'B'}},
    ]
    pm.start_task(task())
    join_task(pm)
    assert [call.args[0]['name'] for call in pm.runtime.execute.call_args_list] == ['A', 'A', 'B']
    assert pm._state.retry_counts['A'] == 1
    assert sum(url.endswith('/deliver') for url, _ in pm.test_calls) == 1
    assert not any(payload.get('status') == 'escalated' for _, payload in pm.test_calls)


def test_quality_rejection_after_retry_budget_never_publishes_output(pm):
    pm._monitor._verify_output_quality = lambda *args: {'accepted': False, 'score': 2, 'issues': '缺少要求'}
    pm.start_task(task())
    join_task(pm)
    assert pm.runtime.execute.call_count == 3
    assert not pm._state.subtask_outputs
    assert '质量验证' in pm._state.terminal_failures['A']
    assert not any(url.endswith('/deliver') for url, _ in pm.test_calls)


@pytest.mark.parametrize('succeeds', [False, True])
def test_remote_recovery_uses_one_other_station_then_one_local_takeover(pm, monkeypatch, succeeds):
    pm._planner.analyze_with_skill = lambda task: {'pattern': 'orchestrator', 'decomposition': [{'name': 'A'}]}
    pm._dispatcher.get_available_stations = lambda: [
        {'device_id': 's1', 'ip': '127.0.0.1', 'api_port': 1},
        {'device_id': 's2', 'ip': '127.0.0.1', 'api_port': 2}]
    pm._dispatcher._create_subagent_on_station = lambda *args, **kw: {'agent_id': kw['preferred_agent_id']}
    remote = Mock(return_value=Mock(status_code=200, json=lambda: {'status': 'failed', 'output': {'error': 'remote failed'}}))
    monkeypatch.setattr('lan_mesh.pm_dispatcher.requests.post', remote)
    if not succeeds:
        pm.runtime.execute.return_value = {'status': 'failed', 'error': 'local failed', 'output': {}}
    pm.start_task(task())
    join_task(pm)
    assert remote.call_count == 4  # Initial + two same-station retries + one other station.
    assert pm.runtime.execute.call_count == 1
    assert pm._state.cross_station_attempted == {'A'}
    assert bool(pm._state.terminal_failures) is not succeeds
    assert sum(url.endswith('/deliver') for url, _ in pm.test_calls) == int(succeeds)
    assert not any(payload.get('status') == 'escalated' for _, payload in pm.test_calls)


@pytest.mark.parametrize('plan', [[], [{'name': 'A'}, {'name': 'A'}],
                                 [{'name': 'A', 'depends_on': ['missing']}],
                                 [{'name': 'A', 'depends_on': ['B']}, {'name': 'B', 'depends_on': ['A']}],
                                 [{'name': ''}]])
def test_invalid_dependency_plan_fails_before_execution(pm, plan):
    pm._planner.analyze_with_skill = lambda task: {'pattern': 'orchestrator', 'decomposition': plan}
    pm.start_task(task())
    join_task(pm)
    pm.runtime.execute.assert_not_called()
    assert not any(url.endswith('/deliver') for url, _ in pm.test_calls)
    assert any(payload.get('status') == 'failed' for _, payload in pm.test_calls)


def test_subtask_timeout_does_not_start_another_attempt(pm):
    import time
    pm.running = True
    pm._state.plan = {'decomposition': [{'name': 'A'}, {'name': 'B', 'depends_on': ['A']}]}
    pm._state.subtask_timeout = 1
    pm._state.subtask_start_times['A'] = time.time() - 10
    pm._monitor.check_subtask_timeouts()
    pm.runtime.execute.assert_not_called()
    assert pm._state.stop_event.is_set()
    assert '待核实' in pm._state.terminal_failures['A']


def test_completed_subtask_ignores_duplicate_or_late_failure(pm):
    pm._state.subtask_outputs['A'] = {'summary': 'verified'}
    pm.receive_subtask_result('A', 'failed', {'error': 'late'}, 'old-worker')
    pm.receive_subtask_result('A', 'completed', {'summary': 'duplicate'}, 'old-worker')
    pm.runtime.execute.assert_not_called()
    assert pm._state.subtask_outputs['A'] == {'summary': 'verified'}
    assert not pm._state.retry_counts
