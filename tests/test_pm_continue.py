"""Pause continuation must reuse effects, fence old work and survive partial activation."""
import copy
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import pytest

from lan_mesh.pm_agent import ProjectManagerAgent
from lan_mesh.pm_checkpoint import read_checkpoint
from lan_mesh.pm_control import control_pm, request_task_control
from lan_mesh.task_continue import continue_authorization, prepare_continue, request_task_continue
from lan_mesh.task_resume import resume_authorization
from tests.test_pm_resume import controller as _controller_fixture
from tests.test_pm_resume import finish
from tests.test_pm_resume import recovery as _recovery_fixture

controller = _controller_fixture
recovery = _recovery_fixture


@pytest.fixture
def paused(recovery):
    r = recovery
    ctl = r.controller
    task = ctl.db.begin_task_control('task-resume', 'pause')
    control = task.output_data['_pm_control']
    ctl.db.finish_task_control(task.task_id, control['request_id'], True, 'paused')
    st = r.state
    st.pause = {'format': 1, 'source_phase': st.phase, 'returned': {}}
    st.stop_reason, st.phase = 'paused', 'paused'
    st.stop_event.set()
    st.control_receipt = dict(ok=True, pm_id='pm-resume', task_id='task-resume', device_id='self',
                              request_id=control['request_id'], action='pause', scope='pm_cooperative')
    r.save()
    r.pause_id = control['request_id']
    return r


def proceed(r, revision=None, pause_id=None):
    return request_task_continue(r.controller, 'task-resume', revision or r.revision[0], pause_id or r.pause_id)


def old_instance(r):
    pm = ProjectManagerAgent('pm-resume', r.controller.chat_runtime, 'http://original', 'self')
    pm._state.restore(r.state.snapshot())
    pm._checkpoint_store, pm._checkpoint_revision = r.store, r.revision[0]
    r.controller._local_pm_agent = pm
    return pm


def test_continue_reuses_A_and_fences_old_instance_and_stale_task(paused):
    r = paused
    old = old_instance(r)
    stale = r.controller.db.get_task('task-resume')
    assert proceed(r)['ok']
    pm = finish(r)
    assert pm is not old and old._state.stop_event.is_set() and old._retired
    assert pm._state.start_time == old._state.start_time
    assert [c.args[0]['name'] for c in pm.runtime.execute.call_args_list] == ['B']
    task = r.controller.db.get_task('task-resume')
    assert task.status == 'completed' and '_pm_control' not in task.output_data
    assert task.output_data['_pm_control_history'][0]['request_id'] == r.pause_id
    assert task.output_data['_pm_generation'] == pm._state.generation
    r.controller.db.save_task(stale)
    assert r.controller.db.get_task('task-resume').to_dict() == task.to_dict()
    record = read_checkpoint(r.path, 'pm-resume', 'task-resume', 'self')
    assert not old.checkpoint()
    assert read_checkpoint(r.path, 'pm-resume', 'task-resume', 'self') == record
    # Even a writer that learns the new revision cannot overwrite the new owner.
    with pytest.raises(ValueError):
        r.store.save('pm-resume', 'task-resume', 'self', r.state.snapshot(), record['revision'])


@pytest.mark.parametrize('kind', ['cancel', 'delivery', 'replacement', 'budget', 'unconfirmed', 'wrong_pause', 'deadline', 'local_unknown'])
def test_conflict_does_not_activate_or_execute(paused, kind):
    r = paused
    task = r.controller.db.get_task('task-resume')
    if kind == 'cancel':
        r.controller.db.begin_task_control(task.task_id, 'cancel')
    elif kind == 'delivery':
        task.output_data['_delivery'] = {'deliverable': 'already done'}
        r.controller.db.save_task(task)
    elif kind == 'replacement':
        with r.controller.db._get_conn() as conn:
            conn.execute('INSERT INTO task_resubmissions VALUES (?,?,?,?,?)', (task.task_id, 'other', 1, '{}', 1))
    elif kind == 'budget':
        task.project_id = 'project'
        r.controller.db.save_task(task)
        r.controller.project_manager = Mock(check_budget=Mock(return_value=False))
    elif kind == 'unconfirmed':
        with r.controller.db._get_conn() as conn:
            conn.execute("UPDATE tasks SET output_data=json_set(output_data,'$._pm_control.state','unknown') WHERE task_id=?", (task.task_id,))
    elif kind == 'wrong_pause':
        r.pause_id = 'obsolete'
    elif kind == 'deadline':
        r.state.start_time = 1
        r.save()
    else:
        r.state.execution_attempts['B'] = dict(name='B', subtask_id='original-B', pm_id='pm-resume',
            parent_task_id='task-resume', reporter_id='local', phase='active', station={})
        r.save()
    try:
        assert not proceed(r)['ok']
    except ValueError:
        pass
    r.controller.chat_runtime.execute.assert_not_called()
    assert not r.created
    assert '_pm_generation' not in r.controller.db.get_task(task.task_id).output_data


@pytest.mark.parametrize('kind', ['direct', 'planning', 'aggregation'])
def test_saved_responses_are_reused(paused, kind):
    r = paused
    st = r.state
    if kind == 'direct':
        st.plan = {'pattern': 'single', 'decomposition': [{'name': 'A'}]}
        st.subtask_outputs = {}
        st.pause['source_phase'] = 'executing_single'
        st.direct_execution = {'execution_id': 'original-direct', 'state': 'returned',
                               'result': {'status': 'completed', 'summary': 'saved direct'}}
    elif kind == 'planning':
        st.pause.update(source_phase='planning', returned={'planning': copy.deepcopy(st.plan)})
        st.plan = {}
        st.subtask_outputs = {}
    else:
        st.subtask_outputs['B'] = {'summary': 'saved B'}
        st.pause.update(source_phase='aggregating', returned={'aggregation': {'content': 'saved aggregate'}})
    r.save()
    assert proceed(r)['ok']
    pm = finish(r)
    if kind != 'planning':
        pm.runtime.execute.assert_not_called()
        pm.runtime._call_llm_with_routing.assert_not_called()
    else:
        assert [c.args[0]['name'] for c in pm.runtime.execute.call_args_list] == ['A', 'B']
    assert r.controller.db.get_task('task-resume').status == 'completed'


def test_old_authorization_and_delayed_controls_cannot_stop_new_generation(paused):
    r = paused
    entered, release = threading.Event(), threading.Event()
    def execute(payload):
        entered.set()
        assert release.wait(5)
        return {'status': 'completed', 'output': {'summary': 'B'}}
    r.controller.chat_runtime.execute.side_effect = execute
    old = old_instance(r)
    try:
        assert proceed(r)['ok']
        assert entered.wait(2)
        pm = r.controller._local_pm_agent
        late = control_pm(lambda: pm, 'self', {**old._state.control_receipt, 'request_id': 'old-cancel', 'action': 'cancel'})
        assert not late['ok'] and not pm._state.stop_event.is_set()
        with pytest.raises(ValueError):
            resume_authorization(r.controller, 'task-resume', 'old-recovery')
        r.controller._local_control_pm = lambda payload: control_pm(lambda: pm, 'self', payload)
        assert request_task_control(r.controller, 'task-resume', 'cancel')['confirmed']
        assert pm._state.stop_reason == 'cancelled'
        with pytest.raises(ValueError):
            continue_authorization(r.controller, 'task-resume', pm._state.generation)
    finally:
        release.set()
    finish(r)
    assert '_delivery' not in r.controller.db.get_task('task-resume').output_data


def test_concurrent_clicks_and_lost_ack_only_execute_once(paused):
    r = paused
    entered, release = threading.Event(), threading.Event()
    def execute(payload):
        entered.set()
        assert release.wait(5)
        return {'status': 'completed', 'output': {'summary': 'B'}}
    r.controller.chat_runtime.execute.side_effect = execute
    try:
        with ThreadPoolExecutor(max_workers=5) as pool:
            results = list(pool.map(lambda _: proceed(r), range(5)))
        assert all(result['ok'] for result in results), results
        assert len({result['request_id'] for result in results}) == 1
        assert len(r.created) == 1 and entered.wait(2)
        assert proceed(r)['duplicate']
    finally:
        release.set()
    finish(r)
    assert r.controller.chat_runtime.execute.call_count == 1


@pytest.mark.parametrize('stage', ['before_activation', 'activation_response', 'first_thread', 'second_thread'])
def test_retry_after_prelaunch_failure_reuses_prepared_claim(paused, monkeypatch, stage):
    import lan_mesh.task_continue as central
    r = paused
    original = central.continue_authorization
    failed = []
    def authorize(*args, **kwargs):
        if kwargs.get('activate') and not failed:
            failed.append(True)
            if stage == 'activation_response':
                original(*args, **kwargs)
            raise RuntimeError('lost activation')
        return original(*args, **kwargs)
    with monkeypatch.context() as patch:
        if stage in ('before_activation', 'activation_response'):
            patch.setattr(central, 'continue_authorization', authorize)
        else:
            start = threading.Thread.start
            calls = []
            def start_thread(self):
                calls.append(self)
                if len(calls) == (1 if stage == 'first_thread' else 2):
                    raise RuntimeError('cannot start thread')
                start(self)
            patch.setattr(threading.Thread, 'start', start_thread)
        result = proceed(r)
        assert not result['ok']
        r.controller.chat_runtime.execute.assert_not_called()
        assert read_checkpoint(r.path, 'pm-resume', 'task-resume', 'self')['snapshot']['continuation']['state'] == 'prepared'
        from lan_mesh.pm_recovery import assess_task_recovery
        evidence = assess_task_recovery(r.controller, 'task-resume')
        assert evidence['continue_supported'], evidence
        assert evidence['continue_revision'] == r.revision[0]
    for pm in r.created:
        for name in ('_thread', '_progress_thread'):
            thread = getattr(pm, name)
            if thread and thread.ident is not None:
                thread.join(timeout=2)
            elif thread:
                setattr(pm, name, None)
    assert proceed(r)['ok']
    finish(r)
    assert r.controller.chat_runtime.execute.call_count == 1


def test_cancel_between_node_claim_and_activation_keeps_cancel(paused, monkeypatch):
    import lan_mesh.pm_continue as node
    original = node._claim
    def claim(*args):
        value = original(*args)
        paused.controller.db.begin_task_control('task-resume', 'cancel')
        return value
    monkeypatch.setattr(node, '_claim', claim)
    assert not proceed(paused)['ok']
    assert paused.controller.db.get_task('task-resume').status == 'cancelled'
    paused.controller.chat_runtime.execute.assert_not_called()


def test_callback_quality_work_blocks_continue_even_if_current_was_removed(paused, monkeypatch):
    from lan_mesh.pm_activity import activity
    r = paused
    old = old_instance(r)
    entered, release = threading.Event(), threading.Event()
    @activity
    def blocked(pm):
        entered.set()
        assert release.wait(5)
    thread = threading.Thread(target=blocked, args=(old,))
    thread.start()
    try:
        assert entered.wait(2)
        r.controller._local_pm_agent = None
        assert not proceed(r)['ok']
        assert not old._retired
        assert r.controller.db.get_task('task-resume').status == 'paused'
    finally:
        release.set()
        thread.join(timeout=2)
    assert proceed(r)['ok']
    finish(r)


def test_assessment_offers_continue_only_for_current_confirmed_pause(paused):
    from lan_mesh.pm_recovery import assess_task_recovery
    r = paused
    evidence = assess_task_recovery(r.controller, 'task-resume')
    assert evidence['continue_supported'] and not evidence['resume_supported'], evidence
    assert evidence['pause_request_id'] == r.pause_id
    r.controller.db.begin_task_control('task-resume', 'cancel')
    assert not assess_task_recovery(r.controller, 'task-resume')['continue_supported']


def test_api_requires_original_pause_and_version(paused):
    client = paused.client
    assert client.post('/api/tasks/task-resume/continue', json={}).status_code == 409
    assert client.post('/api/tasks/missing/continue', json={'expected_revision': 1, 'pause_request_id': 'x'}).status_code == 404
    assert client.get('/api/tasks/task-resume/continue-authorization?request_id=missing').status_code == 409
    paused.controller.secretary_active = False
    assert client.post('/api/tasks/task-resume/continue', json={}).status_code == 503


@pytest.mark.parametrize('stage', ['planning', 'direct', 'subtask', 'aggregation', 'quality'])
def test_live_pause_waits_for_original_work_then_continues_through_delivery(recovery, stage):
    r = recovery
    ctl = r.controller
    pm = ProjectManagerAgent('pm-resume', ctl.chat_runtime, 'http://original', 'self')
    pm._checkpoint_store, pm._checkpoint_revision = r.store, r.revision[0]
    ctl._local_pm_agent = pm
    ctl._local_control_pm = lambda payload: control_pm(lambda: ctl._local_pm_agent, 'self', payload)
    plan = copy.deepcopy(r.state.plan)
    pm._planner.refine_requirements = lambda task: task
    pm._planner.analyze_with_skill = Mock(return_value=plan)
    entered, release = threading.Event(), threading.Event()
    def block(result):
        entered.set()
        assert release.wait(5)
        return result
    if stage == 'planning':
        pm._planner.analyze_with_skill.side_effect = lambda _: block(plan)
    elif stage == 'direct':
        pm._planner.analyze_with_skill.return_value = {'pattern': 'single'}
        ctl.chat_runtime.execute.side_effect = lambda _: block({'status': 'completed', 'output': {'summary': 'original'}})
    elif stage == 'subtask':
        ctl.chat_runtime.execute.side_effect = lambda sub: block({'status': 'completed', 'output': {'summary': sub['name']}})
    elif stage == 'quality':
        ctl.chat_runtime.execute.return_value = {'status': 'completed', 'output': {'summary': 'x' * 80}}
        pm._monitor._verify_output_quality = lambda *args: block({'accepted': True})
    else:
        ctl.chat_runtime._call_llm_with_routing.side_effect = lambda *args: block({'content': 'original aggregate'})
    pm.start_task(ctl.db.get_task('task-resume').to_dict())
    try:
        assert entered.wait(3)
        assert request_task_control(ctl, 'task-resume', 'pause')['confirmed']
        control = ctl.db.get_task('task-resume').output_data['_pm_control']
        revision = pm._checkpoint_revision
        assert not request_task_continue(ctl, 'task-resume', revision, control['request_id'])['ok']
        assert ctl.db.get_task('task-resume').status == 'paused'
        assert '_delivery' not in ctl.db.get_task('task-resume').output_data
    finally:
        release.set()
    pm._thread.join(timeout=3)
    pm._progress_thread.join(timeout=3)
    assert not pm._thread.is_alive() and not pm._progress_thread.is_alive()
    if stage == 'quality':
        ctl.chat_runtime._call_llm_with_routing.return_value = {'content': '{"accepted":true,"score":8}'}
    revision = pm._checkpoint_revision
    counts = ctl.chat_runtime.execute.call_count, ctl.chat_runtime._call_llm_with_routing.call_count
    assert request_task_continue(ctl, 'task-resume', revision, control['request_id'])['ok']
    resumed = finish(r)
    assert resumed is not pm and pm._state.stop_event.is_set()
    assert ctl.db.get_task('task-resume').status == 'completed'
    assert resumed._state.start_time == pm._state.start_time
    if stage == 'direct':
        assert ctl.chat_runtime.execute.call_count == counts[0]
    elif stage == 'aggregation':
        assert ctl.chat_runtime._call_llm_with_routing.call_count == counts[1]
    else:
        assert [c.args[0]['name'] for c in ctl.chat_runtime.execute.call_args_list] == ['A', 'B']
    if stage == 'planning':
        assert pm._planner.analyze_with_skill.call_count == 1


def test_pause_continue_pause_continue_preserves_results_and_ignores_previous_controls(paused):
    r = paused
    entered, release = threading.Event(), threading.Event()
    def execute(payload):
        entered.set()
        assert release.wait(5)
        return {'status': 'completed', 'output': {'summary': 'B once'}}
    r.controller.chat_runtime.execute.side_effect = execute
    try:
        assert proceed(r)['ok']
        assert entered.wait(2)
        pm = r.controller._local_pm_agent
        r.controller._local_control_pm = lambda payload: control_pm(lambda: r.controller._local_pm_agent, 'self', payload)
        assert request_task_control(r.controller, 'task-resume', 'pause')['confirmed']
        new_pause = r.controller.db.get_task('task-resume').output_data['_pm_control']
        assert new_pause['generation'] == pm._state.generation
    finally:
        release.set()
    finish(r)
    assert request_task_continue(r.controller, 'task-resume', pm._checkpoint_revision, new_pause['request_id'])['ok']
    second = finish(r)
    assert second._state.generation != pm._state.generation
    assert r.controller.chat_runtime.execute.call_count == 1
    assert r.controller.db.get_task('task-resume').status == 'completed'
    assert not proceed(r)['ok']


@pytest.mark.parametrize('status', ['failed', 'unknown'])
def test_saved_terminal_results_follow_normal_failure_rules(paused, status):
    r = paused
    report = dict(subtask_id='original-B', parent_task_id='task-resume', pm_id='pm-resume',
                  reporter_id='local', task_name='B', status=status, output={}, message='original failure')
    r.state.execution_attempts['B'] = {**report, 'name': 'B', 'station': {}, 'phase': 'processing', 'received_result': report}
    r.state.dispatched.add('B')
    r.save()
    assert proceed(r)['ok']
    pm = finish(r)
    if status == 'unknown':
        pm.runtime.execute.assert_not_called()
        assert r.controller.db.get_task('task-resume').status == 'failed'
    else:
        assert pm.runtime.execute.call_count == 1
        assert r.controller.db.get_task('task-resume').status == 'completed'


@pytest.mark.parametrize('endpoint', ['status', 'progress', 'subtasks', 'deliver'])
def test_late_old_generation_reports_do_not_change_active_task(paused, endpoint):
    r = paused
    entered, release = threading.Event(), threading.Event()
    def execute(payload):
        entered.set()
        assert release.wait(5)
        return {'status': 'completed', 'output': {'summary': 'B'}}
    r.controller.chat_runtime.execute.side_effect = execute
    try:
        assert proceed(r)['ok']
        assert entered.wait(2)
        before = r.controller.db.get_task('task-resume').to_dict()
        payload = dict(status='failed', reporter_type='pm', task_id='task-resume',
                       subtasks=[{'name': 'old'}], deliverable='old delivery')
        response = r.client.post(f'/api/pm/pm-resume/{endpoint}', json=payload)
        if endpoint == 'deliver':
            assert response.status_code == 409
        else:
            assert response.json()['ignored']
        assert r.controller.db.get_task('task-resume').to_dict() == before
    finally:
        release.set()
    finish(r)


def test_new_generation_stop_receipt_survives_lost_ack_and_missing_instance(paused):
    from lan_mesh.pm_inspection import inspect_pm_start
    r = paused
    entered, release = threading.Event(), threading.Event()
    def execute(payload):
        entered.set()
        assert release.wait(5)
        return {'status': 'completed', 'output': {'summary': 'B'}}
    r.controller.chat_runtime.execute.side_effect = execute
    try:
        assert proceed(r)['ok']
        assert entered.wait(2)
        pm = r.controller._local_pm_agent
        def lost(payload):
            assert control_pm(lambda: pm, 'self', payload)['ok']
            return {}
        r.controller._local_control_pm = lost
        r.controller._local_inspect_pm_start = lambda pid, tid: inspect_pm_start(r.path, pid, tid, 'self', {})
        response = request_task_control(r.controller, 'task-resume', 'pause')
        assert response['confirmed']
        assert response['control']['generation'] == pm._state.generation
    finally:
        release.set()
    finish(r)


@pytest.mark.parametrize('mode', ['single', 'orchestrator'])
def test_child_process_pauses_and_exits_then_original_outputs_continue(paused, tmp_path, mode):
    import json
    import subprocess
    import sys

    from lan_mesh.pm_resume import resume_pm_once
    r = paused
    path = tmp_path / 'child-pause.sqlite3'
    script = """
import json,os,sys,time
from pathlib import Path
from unittest.mock import Mock
from lan_mesh.pm_agent import ProjectManagerAgent
from lan_mesh.pm_checkpoint import PMCheckpoints
from lan_mesh.pm_control import control_pm
from lan_mesh.pm_lease import acquire_pm_lease
import lan_mesh.pm_agent as module
module.http_post=Mock()
pm=ProjectManagerAgent('pm-resume',Mock(),'http://original','self')
pm._checkpoint_store=PMCheckpoints(Path(sys.argv[1]),lease=acquire_pm_lease(Path(sys.argv[1])))
pm._planner.load_skill=Mock();pm._planner.refine_requirements=lambda task:task
plan={'pattern':sys.argv[2],'decomposition':[{'name':'A'},{'name':'B','depends_on':['A']}]}
if sys.argv[2]=='single':plan['decomposition']=[{'name':'A'}]
pm._planner.analyze_with_skill=lambda task:plan
pm._dispatcher.get_available_stations=lambda:[]
original=pm.checkpoint
def checkpoint(phase=''):
    ok=original(phase)
    st=pm._state
    if ok and st.stop_reason=='paused' and (st.direct_execution.get('state')=='returned' or st.execution_attempts.get('A',{}).get('received_result')):
        os._exit(23)
    return ok
pm.checkpoint=checkpoint;pm._state.persist=checkpoint
def execute(task):
    assert control_pm(lambda:pm,'self',dict(pm_id='pm-resume',task_id='task-resume',device_id='self',request_id=sys.argv[3],action='pause'))['ok']
    return {'status':'completed','output':{'summary':'original A'}}
pm.runtime.execute.side_effect=execute
pm.start_task(json.loads(sys.argv[4]))
time.sleep(10)
"""
    proc = subprocess.run([sys.executable, '-c', script, str(path), mode, r.pause_id, json.dumps(r.state.task)], capture_output=True, timeout=15)
    assert proc.returncode == 23, proc.stderr.decode()
    record = read_checkpoint(path, 'pm-resume', 'task-resume', 'self')
    auth = prepare_continue(r.controller, 'task-resume', record['revision'], r.pause_id)
    result = resume_pm_once(payload=auth, receipt_path=path, runtime=r.controller.chat_runtime,
                           device_id='self', device_name='node', current=lambda: r.controller._local_pm_agent,
                           assign=lambda pm: setattr(r.controller, '_local_pm_agent', pm),
                           authorization=lambda tid, rid, activate=False: continue_authorization(r.controller, tid, rid, activate=activate))
    assert result['ok'], result
    pm = finish(r)
    assert r.controller.db.get_task('task-resume').status == 'completed'
    assert pm._state.start_time == record['snapshot']['start_time']
    assert [c.args[0]['name'] for c in pm.runtime.execute.call_args_list] == ([] if mode == 'single' else ['B'])


def test_launch_checkpoint_failure_does_not_execute_and_is_retryable(paused):
    import sqlite3
    r = paused
    with sqlite3.connect(r.path) as conn:
        conn.execute("CREATE TRIGGER deny_launch BEFORE INSERT ON pm_checkpoints "
                     "WHEN json_extract(NEW.snapshot,'$.continuation.state')='launched' "
                     "BEGIN SELECT RAISE(ABORT,'disk failure'); END")
    assert not proceed(r)['ok']
    r.controller.chat_runtime.execute.assert_not_called()
    pm = r.created[-1]
    pm._thread.join(timeout=2)
    pm._progress_thread.join(timeout=2)
    assert read_checkpoint(r.path, 'pm-resume', 'task-resume', 'self')['snapshot']['continuation']['state'] == 'prepared'
    with sqlite3.connect(r.path) as conn:
        conn.execute('DROP TRIGGER deny_launch')
    assert proceed(r)['ok']
    finish(r)
    assert r.controller.chat_runtime.execute.call_count == 1


def test_child_exits_after_claim_same_request_can_activate_and_finish(paused):
    import json
    import subprocess
    import sys
    r = paused
    auth = prepare_continue(r.controller, 'task-resume', r.revision[0], r.pause_id)
    evidence = continue_authorization(r.controller, 'task-resume', auth['request_id'])
    script = """
import json,os,sys
from pathlib import Path
from unittest.mock import Mock
import lan_mesh.pm_continue as module
original=module._claim
def claim(*args):
    original(*args)
    os._exit(24)
module._claim=claim
evidence=json.loads(sys.argv[2])
module.continue_pm_once(payload=evidence['authorization'],receipt_path=Path(sys.argv[1]),runtime=Mock(),
    device_id='self',device_name='node',current=lambda:None,assign=lambda pm:None,
    authorization=lambda *args,**kwargs:evidence)
"""
    proc = subprocess.run([sys.executable, '-c', script, str(r.path), json.dumps(evidence)], capture_output=True, timeout=10)
    assert proc.returncode == 24, proc.stderr.decode()
    assert r.controller.db.get_task('task-resume').status == 'paused'
    assert read_checkpoint(r.path, 'pm-resume', 'task-resume', 'self')['snapshot']['continuation']['state'] == 'prepared'
    assert proceed(r)['ok']
    finish(r)
    assert r.controller.db.get_task('task-resume').status == 'completed'


def test_consumed_restart_authorization_cannot_revive_after_continue(recovery):
    from lan_mesh.task_resume import prepare_resume
    r = recovery
    auth = prepare_resume(r.controller, 'task-resume', r.revision[0])
    task = r.controller.db.begin_task_control('task-resume', 'pause')
    pause_id = task.output_data['_pm_control']['request_id']
    r.controller.db.finish_task_control('task-resume', pause_id, True, 'paused')
    st = r.state
    st.pause = {'format': 1, 'source_phase': 'monitoring', 'returned': {}}
    st.stop_event.set()
    st.phase, st.stop_reason = 'paused', 'paused'
    st.control_receipt = dict(ok=True, pm_id='pm-resume', task_id='task-resume', device_id='self',
                             request_id=pause_id, action='pause', scope='pm_cooperative')
    r.save()
    entered, release = threading.Event(), threading.Event()
    def execute(payload):
        entered.set()
        assert release.wait(5)
        return {'status': 'completed', 'output': {'summary': 'B'}}
    r.controller.chat_runtime.execute.side_effect = execute
    try:
        assert request_task_continue(r.controller, 'task-resume', r.revision[0], pause_id)['ok']
        assert entered.wait(2)
        with pytest.raises(ValueError):
            resume_authorization(r.controller, 'task-resume', auth['request_id'])
    finally:
        release.set()
    finish(r)


def test_restored_local_takeover_failure_does_not_start_an_extra_retry(paused):
    r = paused
    report = dict(subtask_id='takeover-B', parent_task_id='task-resume', pm_id='pm-resume',
                  reporter_id='local', task_name='B', status='failed', output={}, message='last takeover failed')
    r.state.execution_attempts['B'] = {**report, 'name': 'B', 'station': {}, 'phase': 'processing', 'received_result': report}
    r.state.local_takeover_tasks.add('B')
    r.state.dispatched.add('B')
    r.save()
    assert proceed(r)['ok']
    pm = finish(r)
    pm.runtime.execute.assert_not_called()
    assert r.controller.db.get_task('task-resume').status == 'failed'
    assert pm._monitor._local_takeover_tasks is pm._state.local_takeover_tasks


def test_paused_refinement_keeps_question_answer_context_and_deadline(recovery, monkeypatch):
    from lan_mesh.pm_planner import PMPlanner
    r = recovery
    ctl = r.controller
    monkeypatch.setattr(PMPlanner, '_generate_refinement_question', staticmethod(
        lambda desc, context, round_num: '请补充目标' if round_num == 0 else ''))
    monkeypatch.setattr(PMPlanner, 'analyze_with_skill', lambda self, task: {'pattern': 'single'})
    entered = threading.Event()
    original_status = ProjectManagerAgent.report_status
    def status(self, value, **kwargs):
        original_status(self, value, **kwargs)
        if value == 'awaiting_input':
            entered.set()
    monkeypatch.setattr(ProjectManagerAgent, 'report_status', status)
    pm = ProjectManagerAgent('pm-resume', ctl.chat_runtime, 'http://original', 'self')
    pm._checkpoint_store, pm._checkpoint_revision = r.store, r.revision[0]
    ctl._local_pm_agent = pm
    ctl._local_control_pm = lambda payload: control_pm(lambda: ctl._local_pm_agent, 'self', payload)
    task = ctl.db.get_task('task-resume').to_dict()
    task.update(description='做工具', input_data={})
    pm.start_task(task)
    assert entered.wait(3)
    question = copy.deepcopy(pm._state.clarification_request)
    assert request_task_control(ctl, 'task-resume', 'pause')['confirmed']
    pm._thread.join(timeout=3)
    pm._progress_thread.join(timeout=3)
    entered.clear()
    pause_id = ctl.db.get_task('task-resume').output_data['_pm_control']['request_id']
    assert request_task_continue(ctl, 'task-resume', pm._checkpoint_revision, pause_id)['ok']
    assert entered.wait(3)
    resumed = ctl._local_pm_agent
    assert resumed._state.clarification_request == question
    result = resumed.receive_input(dict(pm_id='pm-resume', task_id='task-resume', device_id='self',
                                      question_id=question['question_id'], response='保留原上下文'))
    assert result['ok']
    finish(r)
    assert '保留原上下文' in resumed._state.task['description']
    assert ctl.db.get_task('task-resume').status == 'completed'
    assert resumed._state.start_time == pm._state.start_time
