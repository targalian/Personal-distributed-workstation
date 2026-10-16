"""Continue paused PMs with quiescence, fenced writers and a retryable activation boundary."""
import copy
import json
import sqlite3
import threading
import time
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

from .execution_receipts import PROCESS_OWNER
from .http_retry import http_get, http_post
from .pm_activity import retire_instances
from .pm_agent import ProjectManagerAgent
from .pm_checkpoint import PMCheckpoints, read_checkpoint
from .pm_lease import acquire_pm_lease, lease_identity
from .pm_start import _START_LOCK
from .task_continue import IDENTITY


def continuation_snapshot(snapshot: dict, pm_id: str, task_id: str, pause_request_id: str) -> dict:
    """Validate a copy; leave the old PM and persisted pause untouched until activation."""
    from .pm_resume import validate_snapshot
    receipt = snapshot.get('control_receipt', {})
    if (not snapshot.get('stopped') or snapshot.get('stop_reason') != 'paused'
            or snapshot.get('pause', {}).get('format') != 1
            or receipt.get('request_id') != pause_request_id or receipt.get('action') != 'pause'
            or receipt.get('scope') != 'pm_cooperative' or receipt.get('generation', '') != snapshot.get('generation', '')
            or receipt.get('ok') is not True or receipt.get('pm_id') != pm_id or receipt.get('task_id') != task_id):
        raise ValueError('现场没有匹配的已确认暂停，不能继续')
    candidate = copy.deepcopy(snapshot)
    candidate.update(stopped=False, stop_reason='', control_receipt={},
                     phase=snapshot['pause']['source_phase'])
    if candidate['phase'] == 'recovering':
        candidate['phase'] = candidate.get('recovery', {}).get('source_phase', '')
    returned = candidate['pause'].get('returned', {})
    if 'planning' in returned:
        plan = returned['planning']
        if not isinstance(plan, dict) or not isinstance(plan.get('pattern'), str):
            raise ValueError('保存的规划响应无效，请核实原现场')
        candidate['plan'] = plan
        candidate['phase'] = 'planned'
        refined = candidate.get('refinement', {}).get('result_task')
        if isinstance(refined, dict) and refined.get('task_id') == task_id:
            candidate['task'] = refined
    # A marked dispatch without an attempt has never crossed the durable execution boundary.
    # Only continuation of new snapshots may clear it; legacy restart rules stay conservative.
    candidate['dispatched'] = [name for name in candidate['dispatched']
                               if name in candidate['execution_attempts'] or name in candidate['subtask_outputs']]
    if candidate['phase'] == 'dispatching' and not candidate['plan'].get('decomposition'):
        raise ValueError('执行计划缺失')
    validate_snapshot(candidate, pm_id, task_id, paused_results=True)
    return candidate


def _claim(store, identity, record):
    """Claim a paused/prepared snapshot; no execution or central status change here."""
    snapshot = copy.deepcopy(record['snapshot'])
    snapshot['continuation'] = {**identity, 'state': 'prepared', 'claimed_at': time.time()}
    with closing(sqlite3.connect(store.path, timeout=10)) as conn, conn:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute('SELECT task_id,device_id,revision FROM pm_checkpoints WHERE pm_id=?',
                           (identity['pm_id'],)).fetchone()
        if row != (identity['task_id'], identity['device_id'], record['revision']):
            raise ValueError('暂停现场已变化，请重新核实')
        conn.execute('UPDATE pm_checkpoints SET owner=?,revision=?,updated_at=?,snapshot=? WHERE pm_id=?',
                     (store.owner, record['revision'] + 1, time.time(), json.dumps(snapshot, ensure_ascii=False), identity['pm_id']))
    return record['revision'] + 1


def continue_pm_once(*, payload: dict, receipt_path: Path, runtime: Any, device_id: str, device_name: str,
                     current: Callable, assign: Callable, authorization: Callable | None = None) -> dict:
    from .pm_resume import _run_resumed
    identity = {k: payload.get(k) for k in IDENTITY}
    result = {'ok': False, **identity}
    gate = threading.Event()
    armed = threading.Event()
    pm = None
    launched = False
    try:
        if (any(not isinstance(identity[k], str) or not identity[k] for k in IDENTITY if k != 'expected_revision')
                or type(identity['expected_revision']) is not int or identity['expected_revision'] < 1
                or identity['device_id'] != device_id or runtime is None):
            raise ValueError('继续请求身份、版本或运行时无效')
        with _START_LOCK:
            record = read_checkpoint(receipt_path, identity['pm_id'], identity['task_id'], device_id)
            if record is None:
                raise ValueError('暂停现场不存在')
            snapshot = record['snapshot']
            if snapshot.get('process_lease') != lease_identity(receipt_path):
                raise ValueError('仅支持原 Linux 主机原路径上的新版暂停现场')
            lease = acquire_pm_lease(receipt_path)
            previous = snapshot.get('continuation', {})
            duplicate = previous.get('request_id') == identity['request_id']
            if duplicate and any(previous.get(k) != v for k, v in identity.items()):
                raise ValueError('同一继续请求不能更换暂停现场')
            if duplicate and previous.get('state') == 'launched':
                return {'ok': True, **identity, 'duplicate': True,
                        'message': '该继续请求已启动；不重复执行，请核实当前进度。进程退出后请核实恢复条件'}
            if not duplicate and record['revision'] != identity['expected_revision']:
                raise ValueError('暂停现场已变化，请重新核实后继续')
            candidate = continuation_snapshot(snapshot, identity['pm_id'], identity['task_id'], identity['pause_request_id'])
            if snapshot['control_receipt'].get('device_id') != device_id:
                raise ValueError('暂停回执节点不匹配')
            secretary_url = snapshot.get('secretary_url', '').rstrip('/')
            if not secretary_url:
                raise ValueError('原秘书地址缺失')

            def authorized(activate=False):
                if authorization:
                    evidence = authorization(identity['task_id'], identity['request_id'], activate=activate)
                else:
                    base = f"{secretary_url}/api/tasks/{quote(identity['task_id'], safe='')}"
                    if activate:
                        response = http_post(base + '/continue-activation', json={'request_id': identity['request_id']},
                                             timeout=5, retries=0, allow_redirects=False)
                    else:
                        response = http_get(base + '/continue-authorization', params={'request_id': identity['request_id']},
                                            timeout=5, retries=0, allow_redirects=False)
                    evidence = response.json() if response is not None and response.status_code == 200 else {}
                auth, task = evidence.get('authorization', {}), evidence.get('task', {})
                if (any(auth.get(k) != v for k, v in identity.items()) or task.get('task_id') != identity['task_id']
                        or task.get('pm_agent_id') != identity['pm_id'] or (activate and auth.get('state') != 'active')):
                    raise ValueError('秘书未授权该暂停版本继续，或任务已经变化')
                return evidence

            authorized()
            active = current()
            if active and active.running:
                raise ValueError('当前节点已有运行中的 PM')
            # Freeze every matching old object, including objects no longer in current().
            retire_instances(receipt_path, identity['pm_id'])
            store = PMCheckpoints(receipt_path, owner=f'{PROCESS_OWNER}:continue:{uuid.uuid4().hex}', lease=lease)
            revision = _claim(store, identity, record)
            # If this call or its response is lost, the prepared snapshot can be retried.
            authorized(activate=True)
            pm = ProjectManagerAgent(identity['pm_id'], runtime, secretary_url, device_id, device_name)
            candidate['generation'] = identity['request_id']
            candidate['continuation'] = {**identity, 'state': 'launched'}
            candidate['recovery'] = {**identity, 'source_phase': candidate['phase']}
            pm._state.restore(candidate)
            pm._checkpoint_store, pm._checkpoint_revision = store, revision
            pm._resume_authorization = lambda: authorized(activate=True)
            pm._started, pm.running = True, True
            pm._planner.load_skill()

            def run(target):
                gate.wait()
                if armed.is_set():
                    target()

            pm._thread = threading.Thread(target=run, args=(lambda: _run_resumed(pm),), daemon=True,
                                          name=f'pm-continue-{pm.pm_id[:8]}')
            pm._progress_thread = threading.Thread(target=run, args=(pm._monitor.progress_loop,), daemon=True,
                                                   name=f'pm-progress-{pm.pm_id[:8]}')
            pm._thread.start()
            pm._progress_thread.start()
            authorized(activate=True)
            if not pm.checkpoint('recovering'):
                raise ValueError('继续启动记录保存失败，未执行；请核实原现场')
            assign(pm)
            launched = True
            armed.set()
            gate.set()
            return {'ok': True, **identity, 'duplicate': duplicate,
                    'message': '已继续原任务；沿用原时限和执行结果，请查看任务进度'}
    except Exception as exc:
        if pm is not None and not launched:
            # Threads are gated, so failure before the durable launch record has no effects.
            pm.running = False
            pm._state.stop_event.set()
        return {**result, 'message': str(exc)}
    finally:
        gate.set()
