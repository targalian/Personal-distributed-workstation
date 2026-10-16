"""Resume the original PM after process exclusion and a version-bound authorization."""
import ipaddress
import threading
import time
from pathlib import Path
from urllib.parse import quote

from .http_retry import http_get
from .pm_activity import activity
from .pm_agent import ProjectManagerAgent
from .pm_checkpoint import PMCheckpoints, read_checkpoint
from .pm_dispatcher import PMDispatcher
from .pm_lease import acquire_pm_lease, lease_identity
from .pm_recovery import _receipt_status
from .pm_start import _START_LOCK


def validate_snapshot(snapshot: dict, pm_id: str, task_id: str, *, paused_results=False) -> None:
    """Reject ambiguous local effects; remote effects retain their original IDs."""
    if (snapshot.get('stopped') or snapshot.get('stop_reason') or snapshot.get('terminal_failures')
            or snapshot.get('phase') in ('completed', 'failed')):
        raise ValueError('执行现场已停止或失败，不能直接续跑')
    if time.time() - snapshot['start_time'] >= snapshot['global_timeout']:
        raise ValueError('原任务全局时限已耗尽，不能重置时限')
    delivery = snapshot.get('delivery', {}).get('payload')
    if delivery and (delivery.get('task_id') != task_id or delivery.get('pm_id') != pm_id
                     or not isinstance(delivery.get('deliverable'), str) or not delivery['deliverable'].strip()):
        raise ValueError('已保存交付请求身份或内容无效')
    plan = snapshot['plan']
    steps = plan.get('decomposition', [])
    if not steps:
        phase = snapshot.get('recovery', {}).get('source_phase') if snapshot['phase'] == 'recovering' else snapshot['phase']
        if phase == 'awaiting_input':
            from .pm_clarification import valid_refinement
            if not valid_refinement(snapshot):
                raise ValueError('澄清现场没有可验证的继续点，不能直接恢复')
        elif phase not in ('starting', 'refining', 'refined', 'planning') and not (phase == 'planned' and plan.get('pattern') == 'single'):
            raise ValueError('执行计划缺失，请核实现场')
        if snapshot['execution_attempts'] or snapshot['direct_execution'] or snapshot['dispatched']:
            raise ValueError('已有执行记录但没有计划，不能重新规划')
        return
    if snapshot['phase'] == 'awaiting_input':
        raise ValueError('执行中的澄清没有已支持的继续点')
    if len(steps) > 150:
        raise ValueError('执行计划超出支持范围')
    PMDispatcher._validate_plan(steps)
    direct = snapshot.get('direct_execution', {})
    for sub in steps:
        name = sub['name']
        if name in snapshot['subtask_outputs']:
            continue
        if plan.get('pattern') == 'single' and direct:
            if direct.get('state') != 'returned' or not isinstance(direct.get('result'), dict):
                raise ValueError('本地直接执行结果未知，不能重做')
            continue
        attempt = snapshot['execution_attempts'].get(name)
        if attempt:
            if (attempt.get('pm_id') != pm_id or attempt.get('parent_task_id') != task_id
                    or attempt.get('name') != name or not attempt.get('subtask_id') or not attempt.get('reporter_id')):
                raise ValueError('原执行归属不匹配')
            report = attempt.get('received_result')
            if report:
                status = _receipt_status(attempt, {**report, 'name': report.get('task_name')})
                if status != 'receipt_completed' and not (paused_results and status in ('failed', 'result_unknown')
                        and report.get('status') in ('failed', 'unknown')):
                    raise ValueError('原结果失败、未知或身份不符，不能自动续跑')
            elif not attempt.get('station') or attempt.get('phase') != 'active':
                raise ValueError('原执行没有可核实的在途回执，不能重做')
            else:
                station = attempt['station']
                ipaddress.ip_address(station.get('ip', ''))
                if not station.get('device_id') or type(station.get('api_port')) is not int or not 1 <= station['api_port'] <= 65535:
                    raise ValueError('原执行节点身份或地址无效')
        elif name in snapshot['dispatched']:
            raise ValueError('步骤已派发但缺少执行编号，不能重做')


@activity
def _run_resumed(pm: ProjectManagerAgent) -> None:
    st = pm._state
    keep_monitoring = False
    try:
        if not pm.execution_allowed():
            return
        if st.plan.get('pattern') == 'single':
            pm._execute_single(st.task, st.plan)
            return
        if not st.plan.get('decomposition'):
            pm._run_task(st.task)
            keep_monitoring = pm.running
            return
        # No outstanding attempt can be put back in the dispatch queue.
        with st.lock:
            st.pending_subtasks = {name: info for name, info in st.pending_subtasks.items()
                                   if name not in st.execution_attempts and name not in st.subtask_outputs}
            reports = []
            for name, attempt in st.execution_attempts.items():
                if name not in st.subtask_outputs and attempt.get('received_result'):
                    attempt['phase'] = 'active'
                    reports.append(dict(attempt['received_result']))
            if not pm.checkpoint('recovering'):
                return
        for report in reports:
            pm.receive_progress_report(report)  # Same quality gate as a live result.
            if st.stop_event.is_set():
                return
        pm._dispatcher.create_team_and_dispatch(st.task, st.plan)
        if st.stop_event.is_set():
            return
        with st.lock:
            st.monitoring_ready.set()
            if not pm.checkpoint('monitoring'):
                return
        pm.report_status('monitoring')
        keep_monitoring = True
    except Exception as exc:
        pm._monitor._fail_task('恢复执行', str(exc))
    finally:
        if not keep_monitoring:
            pm.running = False


def resume_pm_once(*, payload: dict, receipt_path: Path, runtime, device_id: str,
                   device_name: str, current, assign, authorization=None) -> dict:
    if "pause_request_id" in payload:
        from .pm_continue import continue_pm_once
        return continue_pm_once(payload=payload, receipt_path=receipt_path, runtime=runtime, device_id=device_id,
                                device_name=device_name, current=current, assign=assign, authorization=authorization)
    identity = {k: payload.get(k) for k in ('pm_id', 'task_id', 'device_id', 'request_id', 'expected_revision')}
    result = {'ok': False, **identity}
    claimed_pm = None
    try:
        if (not all(isinstance(identity[k], str) and identity[k] for k in ('pm_id', 'task_id', 'device_id', 'request_id'))
                or identity['device_id'] != device_id or type(identity['expected_revision']) is not int
                or identity['expected_revision'] < 1):
            raise ValueError('恢复请求身份或检查点版本无效')
        with _START_LOCK:
            record = read_checkpoint(receipt_path, identity['pm_id'], identity['task_id'], device_id)
            if record is None:
                raise ValueError('没有可恢复的检查点')
            snapshot = record['snapshot']
            secretary_url = snapshot.get('secretary_url', '').rstrip('/')
            if not secretary_url or runtime is None:
                raise ValueError('原秘书地址或执行运行时不可用')
            if snapshot.get('process_lease') != lease_identity(receipt_path):
                raise ValueError('旧任务无进程排除凭据或检查点已迁移，不能自动续跑')
            lease = acquire_pm_lease(receipt_path)
            def authorized():
                if authorization:
                    evidence = authorization(identity['task_id'], identity['request_id'])
                else:
                    response = http_get(f"{secretary_url}/api/tasks/{quote(identity['task_id'], safe='')}/resume-authorization",
                                        params={'request_id': identity['request_id']}, timeout=5, retries=0, allow_redirects=False)
                    evidence = response.json() if response is not None and response.status_code == 200 else {}
                auth = evidence.get('authorization', {})
                task = evidence.get('task', {})
                if (any(auth.get(k) != v for k, v in identity.items())
                        or task.get('task_id') != identity['task_id'] or task.get('pm_agent_id') != identity['pm_id']):
                    raise ValueError('秘书未授权该版本恢复，或任务归属已变化')
                return evidence
            # Duplicate requests only report prior claims; never relaunch a thread.
            if snapshot.get('recovery', {}).get('request_id') == identity['request_id']:
                if snapshot['recovery'].get('expected_revision') != identity['expected_revision']:
                    raise ValueError('同一恢复请求不能改变检查点版本')
                pm = current()
                return {'ok': True, **identity, 'duplicate': True,
                        'running': bool(pm and pm.pm_id == identity['pm_id'] and pm.running),
                        'message': '该恢复请求已登记；不重复启动，请核实当前任务和原节点'}
            pm = current()
            if pm and pm.running:
                raise ValueError('当前节点已有运行中的 PM')
            validate_snapshot(snapshot, identity['pm_id'], identity['task_id'])
            authorized()
            from .pm_activity import retire_instances
            retire_instances(receipt_path, identity['pm_id'])
            store = PMCheckpoints(receipt_path, lease=lease)
            claim = store.takeover(identity['pm_id'], identity['task_id'], device_id,
                                   identity['expected_revision'], identity['request_id'])
            if claim['duplicate']:
                return {'ok': True, **identity, 'duplicate': True, 'running': False, 'message': '恢复已登记，未重复启动'}
            pm = ProjectManagerAgent(identity['pm_id'], runtime, secretary_url, device_id, device_name)
            claimed_pm = pm
            pm._checkpoint_store, pm._checkpoint_revision = store, claim['revision']
            pm._state.restore(claim['snapshot'])
            pm._resume_authorization = authorized
            pm._started, pm.running = True, True
            assign(pm)
            if not pm.execution_allowed() or not pm.checkpoint('recovering'):
                raise ValueError('接管后授权失效或检查点写入失败，未启动执行')
            pm._planner.load_skill()
            pm._thread = threading.Thread(target=_run_resumed, args=(pm,), daemon=True, name=f"pm-resume-{pm.pm_id[:8]}")
            pm._progress_thread = threading.Thread(target=pm._monitor.progress_loop, daemon=True, name=f"pm-progress-{pm.pm_id[:8]}")
            pm._thread.start()
            pm._progress_thread.start()
            return {'ok': True, **identity, 'duplicate': False, 'running': pm.running,
                    'message': '已接管原执行现场；复用成功结果并继续原任务，请查看执行进度'}
    except Exception as exc:
        if claimed_pm is not None:
            claimed_pm._signal_stop('recovery_failed')
        return {**result, 'message': str(exc)}
