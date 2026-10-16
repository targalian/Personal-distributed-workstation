"""Persist explicit, version-bound recovery authorization on the task's original PM."""
import json
import time
import uuid

from .http_retry import http_post

ACTIVE = ('running', 'planning', 'executing', 'monitoring', 'awaiting_input', 'interrupted')


def _eligible(controller, task):
    if task is None:
        raise KeyError('任务不存在')
    output = task.output_data or {}
    if task.status not in ACTIVE or '_delivery' in output or '_pm_control' in output:
        raise ValueError('任务已停止或已有交付，不能恢复执行')
    pm = controller.db.get_pm_agent(task.pm_agent_id)
    if pm is None or pm.task_id != task.task_id:
        raise ValueError('原 PM 归属缺失或变化')
    if controller.db._get_conn().execute('SELECT 1 FROM task_resubmissions WHERE source_task_id=?', (task.task_id,)).fetchone():
        raise ValueError('原任务已有替代任务，不能同时恢复')
    manager = getattr(controller, 'project_manager', None)
    if task.project_id and manager and not manager.check_budget(task.project_id):
        raise ValueError('项目预算已耗尽或项目已暂停')
    return pm


def prepare_resume(controller, task_id: str, revision: int) -> dict:
    """Serialize concurrent requests; retries for the same version reuse authorization."""
    if type(revision) is not int or revision < 1:
        raise ValueError('请先核实执行现场并提供有效版本')
    db = controller.db
    with db._get_conn() as conn:
        conn.execute('BEGIN IMMEDIATE')
        task = db.get_task(task_id)
        pm = _eligible(controller, task)
        previous = (task.output_data or {}).get('_pm_resume', {})
        history = previous.get('history', [])
        for earlier in [previous, *history]:
            if earlier.get('expected_revision') == revision and earlier.get('pm_id') == pm.pm_id:
                return {k: v for k, v in earlier.items() if k != 'history'}
        request = {'request_id': uuid.uuid4().hex, 'pm_id': pm.pm_id, 'task_id': task_id,
                   'device_id': pm.device_id, 'expected_revision': revision, 'state': 'authorized',
                   'authorized_at': time.time(), 'generation': (task.output_data or {}).get('_pm_generation', '')}
        if previous:
            request['history'] = [*history, {k: v for k, v in previous.items() if k != 'history'}]
        output = {**(task.output_data or {}), '_pm_resume': request}
        conn.execute('UPDATE tasks SET output_data=? WHERE task_id=?', (json.dumps(output, ensure_ascii=False), task_id))
    return request


def resume_authorization(controller, task_id: str, request_id: str) -> dict:
    """Revalidate current task, ownership, replacement and budget before resumed effects."""
    db = controller.db
    with db._get_conn() as conn:
        conn.execute('BEGIN IMMEDIATE')
        task = db.get_task(task_id)
        pm = _eligible(controller, task)
        latest = (task.output_data or {}).get('_pm_resume', {})
        request = next((r for r in [latest, *latest.get('history', [])] if r.get('request_id') == request_id), {})
        if (not request_id or request.get('request_id') != request_id
                or request.get('pm_id') != pm.pm_id or request.get('device_id') != pm.device_id
                or request.get('generation', '') != (task.output_data or {}).get('_pm_generation', '')):
            raise ValueError('恢复授权已变化，请重新核实')
        return {'authorization': dict(request), 'task': task.to_dict()}


def request_task_resume(controller, task_id: str, revision: int) -> dict:
    request = prepare_resume(controller, task_id, revision)
    pm = controller.db.get_pm_agent(request['pm_id'])
    payload = {k: request[k] for k in ('request_id', 'pm_id', 'task_id', 'device_id', 'expected_revision')}
    try:
        if pm.device_id == controller.state.device_id:
            result = controller._local_resume_pm(payload)
        else:
            response = http_post(f'http://{pm.ip}:{pm.api_port}/role/resume-pm', json=payload,
                                 timeout=12, retries=0, allow_redirects=False)
            result = response.json() if response is not None and response.status_code == 200 else {}
        if isinstance(result, dict) and all(result.get(k) == v for k, v in payload.items()):
            return result
        if (not isinstance(result, dict) or not result.get('ok')
                or any(result.get(k) != v for k, v in payload.items())):
            return {'ok': False, 'uncertain': True, **payload,
                    'message': result.get('message', '恢复未获确认，请核实原节点；重试同一版本复用原请求') if isinstance(result, dict) else '恢复未获确认'}
        return result
    except Exception:
        return {'ok': False, 'uncertain': True, **payload,
                'message': '恢复响应未确认；请核实原节点，重试同一版本不会另建 PM'}
