"""Authorize pause continuation, then activate only after the original node claims it."""
import json
import time
import uuid
from typing import TYPE_CHECKING

from .http_retry import http_post
from .task_resume import ACTIVE

if TYPE_CHECKING:
    from .station_controller import StationController

IDENTITY = ('request_id', 'pm_id', 'task_id', 'device_id', 'expected_revision', 'pause_request_id')


def _eligible(controller, task, request=None):
    if task is None:
        raise KeyError('任务不存在')
    output = task.output_data or {}
    pm = controller.db.get_pm_agent(task.pm_agent_id)
    if not pm or pm.task_id != task.task_id or '_delivery' in output:
        raise ValueError('原 PM 归属变化或已有交付，不能继续')
    if controller.db._get_conn().execute('SELECT 1 FROM task_resubmissions WHERE source_task_id=?', (task.task_id,)).fetchone():
        raise ValueError('原任务已有替代任务，不能继续')
    manager = getattr(controller, 'project_manager', None)
    if task.project_id and manager and not manager.check_budget(task.project_id):
        raise ValueError('项目预算已耗尽或项目已暂停')
    if request and request.get('state') == 'active':
        if (task.status not in ACTIVE or '_pm_control' in output
                or output.get('_pm_generation', '') != request['request_id']):
            raise ValueError('继续授权已被停止操作或新执行替代')
    else:
        control = output.get('_pm_control', {})
        if (task.status != 'paused' or control.get('action') != 'pause'
                or control.get('state') != 'confirmed' or control.get('scope') != 'pm_cooperative'):
            raise ValueError('请先确认原 PM 已暂停，再申请继续')
        if request and control.get('request_id') != request.get('pause_request_id'):
            raise ValueError('原暂停请求已变化，请重新核实')
    return pm


def prepare_continue(controller: "StationController", task_id: str, revision: int, pause_request_id: str) -> dict:
    if (type(revision) is not int or revision < 1
            or not isinstance(pause_request_id, str) or not pause_request_id):
        raise ValueError('请提供已核实的检查点版本与原暂停请求编号')
    with controller.db._get_conn() as conn:
        conn.execute('BEGIN IMMEDIATE')
        task = controller.db.get_task(task_id)
        if task is None:
            raise KeyError('任务不存在')
        previous = (task.output_data or {}).get('_pm_continue', {})
        for item in [previous, *previous.get('history', [])]:
            if item.get('expected_revision') == revision and item.get('pause_request_id') == pause_request_id:
                # Return old requests for receipt lookup only. Authorization is checked separately.
                return {k: v for k, v in item.items() if k != 'history'}
        pm = _eligible(controller, task)
        if task.output_data['_pm_control']['request_id'] != pause_request_id:
            raise ValueError('原暂停请求已变化，请重新核实')
        request = dict(request_id=uuid.uuid4().hex, pm_id=pm.pm_id, task_id=task_id,
                       device_id=pm.device_id, expected_revision=revision, pause_request_id=pause_request_id,
                       state='authorized', authorized_at=time.time())
        if previous:
            request['history'] = [*previous.get('history', []), {k: v for k, v in previous.items() if k != 'history'}]
        output = {**task.output_data, '_pm_continue': request}
        conn.execute('UPDATE tasks SET output_data=? WHERE task_id=?', (json.dumps(output, ensure_ascii=False), task_id))
        return request


def continue_authorization(controller: "StationController", task_id: str, request_id: str, *, activate: bool = False) -> dict:
    with controller.db._get_conn() as conn:
        conn.execute('BEGIN IMMEDIATE')
        task = controller.db.get_task(task_id)
        if task is None:
            raise KeyError('任务不存在')
        output = task.output_data or {}
        request = output.get('_pm_continue', {})
        if not request_id or request.get('request_id') != request_id:
            raise ValueError('继续授权已变化，请重新核实')
        pm = _eligible(controller, task, request)
        if request.get('pm_id') != pm.pm_id or request.get('device_id') != pm.device_id:
            raise ValueError('继续授权的 PM 归属已变化')
        if activate and request['state'] == 'authorized':
            # This transaction is the sole path that removes an active pause.
            output.setdefault('_pm_control_history', []).append(output.pop('_pm_control'))
            output['_pm_generation'] = request_id
            output.pop('_pm_resume', None)  # Earlier restart permissions cannot revive this generation.
            output.pop('_pm_recovery', None)
            request.update(state='active', activated_at=time.time())
            conn.execute('UPDATE tasks SET status=?,output_data=? WHERE task_id=?',
                         ('running', json.dumps(output, ensure_ascii=False), task_id))
            conn.execute('UPDATE pm_agents SET status=?,updated_at=? WHERE pm_id=? AND task_id=?',
                         ('recovering', time.time(), pm.pm_id, task_id))
            task.status, task.output_data = 'running', output
        return {'authorization': dict(request), 'task': task.to_dict()}


def request_task_continue(controller: "StationController", task_id: str, revision: int, pause_request_id: str) -> dict:
    request = prepare_continue(controller, task_id, revision, pause_request_id)
    payload = {k: request[k] for k in IDENTITY}
    # Terminal, cancelled or superseded retries only acknowledge the saved request.
    try:
        continue_authorization(controller, task_id, request['request_id'])
    except ValueError as exc:
        return {'ok': False, **payload, 'duplicate': True, 'message': str(exc)}
    pm = controller.db.get_pm_agent(request['pm_id'])
    try:
        if pm.device_id == controller.state.device_id:
            result = controller._local_resume_pm(payload)
        else:
            response = http_post(f'http://{pm.ip}:{pm.api_port}/role/resume-pm', json=payload,
                                 timeout=12, retries=0, allow_redirects=False)
            result = response.json() if response is not None and response.status_code == 200 else {}
        if isinstance(result, dict) and all(result.get(k) == v for k, v in payload.items()):
            return result
    except Exception:
        pass
    return {'ok': False, 'uncertain': True, **payload,
            'message': '继续响应未确认；可重试同一请求或核实原节点，不会重复执行'}
