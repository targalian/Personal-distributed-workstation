"""Owned periodic producer using the same task scheduler, budgets and PM model as Web."""

import json
import threading
import time
import uuid
from typing import Any

from .application_lifecycle import stop_signal
from .component_service import ComponentService, call_component
from .idempotent_submission import IdempotentSubmissionService
from .periodic_repository import PeriodicRepository
from .periodic_spec import next_due, validate_spec
from .service_runtime import ServiceError


def conversation_scope(controller: object, conv_id: str) -> str:
    if not controller.secretary_active:
        raise ServiceError(503, 'Secretary未激活')
    handler = getattr(controller, 'chat_handler', None)
    for row in getattr(handler, '_conv_index', []):
        if row.get('id') == conv_id:
            return str(row.get('project_id', ''))
    raise ServiceError(404, '所属对话不存在')


class PeriodicService(ComponentService):
    OPERATIONS = ('start', 'pump', 'list_schedules', 'create', 'update', 'history', 'resolve_run')

    def __init__(self, server: Any, owner: object):
        super().__init__(server, owner)
        self._stop = threading.Event()
        self._pump_lock = threading.Lock()
        self._start_lock = threading.Lock()
        self._started = False

    def start(self) -> None:
        with self._start_lock:
            if self._started:
                return
            self._started = True
            thread = threading.Thread(target=self._loop, name='periodic-tasks', daemon=True)
            vars(self.owner).setdefault('_threads', []).append(thread)
            thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set() and not stop_signal(self.owner).is_set():
            try:
                if self.owner.secretary_active:
                    self.pump()
            except Exception as exc:
                print(f'[Task] 周期任务轮询失败: {exc}')
            self._stop.wait(1)

    def deinit(self) -> None:
        self._stop.set()

    def list_schedules(self, conv_id: str) -> dict:
        conversation_scope(self.owner, conv_id)
        return {'items': PeriodicRepository(self.owner.db).list(conv_id)}

    def create(self, conv_id: str, payload: dict) -> dict:
        project_id = conversation_scope(self.owner, conv_id)
        now = time.time()
        try:
            spec = validate_spec(payload.get('spec', {}), now)
        except (ValueError, TypeError, OverflowError) as exc:
            raise ServiceError(422, str(exc)) from exc
        template = payload.get('template', {})
        name = str(payload.get('name', '')).strip()
        if not name or len(name) > 200 or not isinstance(template, dict) or not 1 <= len(str(template.get('description', '')).strip()) <= 32000:
            raise ServiceError(422, '请填写计划名称和任务要求')
        catchup, concurrency = payload.get('catchup', 'skip'), payload.get('concurrency', 'forbid')
        if catchup not in {'skip', 'once'} or concurrency not in {'forbid', 'allow'}:
            raise ServiceError(422, '补跑策略应为skip/once，并发策略应为forbid/allow')
        template = {key: template[key] for key in ('name', 'description', 'input_data') if key in template}
        template.setdefault('name', name)
        if not isinstance(template.get('input_data', {}), dict):
            raise ServiceError(422, '任务输入应为对象')
        item = dict(schedule_id=uuid.uuid4().hex, conv_id=conv_id, project_id=project_id,
                    name=name, spec=spec, template=template, catchup=catchup,
                    concurrency=concurrency, next_run=next_due(spec, now))
        result = PeriodicRepository(self.owner.db).create(item)
        print(f'[Task] 周期计划已创建: {result["schedule_id"]}: {conv_id}')
        return result

    def update(self, schedule_id: str, conv_id: str, payload: dict) -> dict:
        project = conversation_scope(self.owner, conv_id)
        repo = PeriodicRepository(self.owner.db)
        item = repo.get(schedule_id, conv_id)
        if not item:
            raise ServiceError(404, '当前对话没有该计划')
        target = str(payload.get('target_conv_id', conv_id))
        if target != conv_id and any(row['state'] in {'reserved', 'retry_wait', 'review_required'} for row in repo.runs(schedule_id)):
            raise ServiceError(409, '计划仍有待处理触发，请先处理运行记录再迁移')
        if conversation_scope(self.owner, target) != project or item['project_id'] != project:
            raise ServiceError(409, '计划仅可迁移到同一项目的对话')
        enabled = payload.get('enabled', bool(item['enabled']))
        if not isinstance(enabled, bool):
            raise ServiceError(422, 'enabled必须为布尔值')
        return repo.change(schedule_id, conv_id, enabled, item['next_run'], target)

    def history(self, schedule_id: str, conv_id: str) -> dict:
        conversation_scope(self.owner, conv_id)
        repo = PeriodicRepository(self.owner.db)
        if not repo.get(schedule_id, conv_id):
            raise ServiceError(404, '当前对话没有该计划')
        return {'items': repo.runs(schedule_id)}

    def resolve_run(self, schedule_id: str, run_id: str, conv_id: str, payload: dict) -> dict:
        self.history(schedule_id, conv_id)
        note = str(payload.get('note', '')).strip()
        if not 1 <= len(note) <= 2000:
            raise ServiceError(422, '请填写人工核对结论')
        conn = self.owner.db._get_conn()
        row = conn.execute('SELECT * FROM periodic_runs WHERE schedule_id=? AND run_id=?', (schedule_id, run_id)).fetchone()
        if not row or row['state'] != 'review_required':
            raise ServiceError(409, '运行记录并非待核对状态')
        task = self.owner.db.get_task(row['task_id'])
        if not task or task.status not in {'completed', 'failed', 'cancelled', 'accepted'}:
            raise ServiceError(409, '请先核对并完成或停止关联任务，再确认此记录')
        with conn:
            conn.execute("UPDATE periodic_runs SET state='reviewed',detail=? WHERE run_id=?", (note, run_id))
            conn.execute("UPDATE periodic_submissions SET state='completed',response_json=? WHERE task_id=?",
                         (json.dumps(task.to_dict(), ensure_ascii=False), task.task_id))
        print(f'[Task] 周期触发已由用户核对: {run_id}')
        return {'ok': True, 'state': 'reviewed'}

    def pump(self, now: float | None = None) -> dict:
        if not self.owner.secretary_active or not self._pump_lock.acquire(blocking=False):
            return {'processed': 0}
        try:
            now = time.time() if now is None else now
            repo = PeriodicRepository(self.owner.db)
            repo.claim_due(now)
            processed = 0
            for run in repo.pending(now):
                if self._stop.is_set() or stop_signal(self.owner).is_set() or not self.owner.secretary_active:
                    break
                self._submit(repo, run, now)
                processed += 1
            return {'processed': processed}
        finally:
            self._pump_lock.release()

    def _submit(self, repo: PeriodicRepository, run: dict, now: float) -> None:
        if not run['enabled']:
            repo.finish(run['run_id'], 'cancelled')
            return
        try:
            project = conversation_scope(self.owner, run['conv_id'])
            if project != run['project_id']:
                raise ServiceError(409, '所属对话项目已变化，需重新创建计划')
            payload = json.loads(run['template_json'])
            payload.update(project_id=project, created_by='schedule:' + run['conv_id'])
            result = call_component(self.owner, IdempotentSubmissionService, 'submit',
                                    event_id='schedule:' + run['run_id'], payload=payload)
            state = 'review_required' if result.get('status') == 'review_required' else 'submitted'
            repo.finish(run['run_id'], state, result.get('task_id', ''), result.get('error', ''))
            print(f'[Task] 周期触发已记账: {run["run_id"]}: {state}')
        except Exception as exc:
            repo.finish(run['run_id'], 'retry_wait', detail=str(exc),
                        retry_at=now + min(300, 2 ** min(run['attempts'] + 1, 8)))
