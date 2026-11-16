"""Existing task, team, L1/L2 conversation and takeover views over the Yomk bus."""

import json
from starlette.concurrency import run_in_threadpool

from .collaboration_repository import CollaborationRepository
from .service_context import StationService
from .service_runtime import ServiceError


class CollaborationService(StationService):
    def _task(self, task_id: str) -> object:
        self.require_secretary()
        task = self.context.controller.db.get_task(task_id)
        if not task:
            raise ServiceError(404, '任务不存在')
        return task

    def overview(self, task_id: str) -> dict:
        task = self._task(task_id)
        controller, db = self.context.controller, self.context.controller.db
        repo = CollaborationRepository(db)
        pm = db.get_pm_agent(task.pm_agent_id) if task.pm_agent_id else None
        teams = [team.to_dict() for team in db.list_teams(pm_id=task.pm_agent_id)] if pm else []
        chat = getattr(controller, 'chat_handler', None)
        conv_id = chat.find_conv_by_pm(task.pm_agent_id) if chat and task.pm_agent_id else ''
        graph = controller.get_task_graph_data(task_id) or {'nodes': [], 'edges': []}
        snapshot = db.get_pm_snapshot_by_task(task_id) or {}
        try:
            state = snapshot.get('state') or json.loads(snapshot.get('state_json', '{}'))
        except (ValueError, TypeError):
            state = {}
        if not isinstance(state, dict):
            state = {}
        if snapshot.get('pm_id') and snapshot['pm_id'] != task.pm_agent_id:
            state, snapshot = {}, {}
        if pm and pm.status == 'awaiting_input' and task.status not in {'completed', 'accepted', 'rejected', 'cancelled', 'failed'}:
            state = {**state, 'clarification_question': state.get('clarification_question') or 'PM 等待你的输入'}
        status = self._status(task, state, db, snapshot)
        return dict(task=task.to_dict(), pm=pm.to_dict() if pm else None, teams=teams,
                    graph=graph, conversation_id=conv_id or '', pm_thread_id=task.pm_agent_id,
                    takeover=repo.get(task_id), notices=repo.notices(task_id),
                    unread_count=repo.unread_count(task_id), collaboration_status=status,
                    clarification=state.get('clarification_question', ''))

    @staticmethod
    def _status(task: object, state: dict, db: object, snapshot: dict) -> dict:
        """Derive collaboration state without mutating task state on reads."""
        raw = getattr(task, 'status', 'unknown')
        execution = 'completed' if raw in {'accepted', 'rejected'} else raw
        delivery = (task.output_data or {}).get('_delivery') or {}
        current_pm = getattr(task, 'pm_agent_id', '')
        valid = bool(delivery.get('delivered_at') and delivery.get('pm_id') == current_pm
                     and current_pm and ('deliverable' in delivery))
        try:
            if float(delivery.get('delivered_at', 0)) < float(state.get('start_time', 0)):
                valid = False
        except (ValueError, TypeError):
            valid = False
        received = 'station_received' if valid else ('undelivered' if execution == 'completed' else 'unknown')
        accepted = delivery.get('accepted') if valid else None
        acceptance = 'accepted' if accepted is True else 'rejected' if accepted is False else 'unknown'
        phase = str(snapshot.get('phase', '') or state.get('phase', '')).lower()
        code = execution
        if execution == 'completed':
            code = 'completed' if valid else 'undelivered'
        if state.get('clarification_question') or raw == 'awaiting_input':
            code = 'awaiting_input'
        if state.get('interrupted') or phase == 'interrupted':
            code = 'interrupted'
        if state.get('review_required') or phase == 'review_required':
            code = 'review_required'
        labels = {'completed': '执行完成，结果已入站', 'undelivered': '结果未送达',
                  'failed': '执行失败', 'cancelled': '已取消', 'running': '执行中',
                  'pending': '排队中', 'paused': '已暂停', 'awaiting_input': '等待输入',
                  'interrupted': '执行中断', 'review_required': '待人工核对'}
        repo = CollaborationRepository(db)
        notices = repo.notices(task.task_id)
        return dict(code=code if code in labels else 'unknown', label=labels.get(code, '状态未知'),
                    execution=execution, delivery=received, acceptance=acceptance,
                    model_review=delivery.get('acceptance_review', {}) if valid else {},
                    read='unread' if repo.unread_count(task.task_id) else 'read' if notices else 'unknown',
                    subtask_transport=CollaborationService._delivery_evidence(db, task.task_id, state))

    @staticmethod
    def _delivery_evidence(db: object, task_id: str, state: dict) -> list[dict]:
        """Only current subtask attempts; transport receipts never prove final delivery."""
        attempts = state.get('active_attempts', {})
        result = []
        for table in ('mesh_delivery_outbox', 'mesh_delivery_inbox'):
            for row in db._get_conn().execute('SELECT * FROM ' + table):
                try:
                    payload = json.loads(row['payload_json'])
                except (ValueError, TypeError):
                    continue
                if payload.get('parent_task_id') != task_id:
                    continue
                if not any(isinstance(attempt, dict)
                           and payload.get('subtask_id') == attempt.get('subtask_id')
                           and payload.get('attempt_id') == attempt.get('attempt_id')
                           and bool(attempt.get('attempt_id')) for name, attempt in attempts.items()):
                    continue
                result.append(dict(source=table, delivery_id=row['delivery_id'], state=row['state']))
        return result

    async def takeover(self, task_id: str) -> dict:
        task = self._task(task_id)
        if task.status in {'completed', 'cancelled', 'failed'}:
            raise ServiceError(409, '任务已终结，无需接管')
        controller = self.context.controller
        repo = CollaborationRepository(controller.db)
        current = repo.get(task_id)
        if current['state'] == 'human':
            return current
        repo.set(task_id, 'pausing', {})
        try:
            result = await run_in_threadpool(controller.pause_task, task_id)
        except Exception as exc:
            result = {'ok': False, 'message': str(exc)}
        state = 'human' if result.get('ok') and result.get('quiescent') is True else 'pausing'
        item = repo.set(task_id, state, result)
        print(f'[Task] 人工接管: {task_id}: {state}')
        return item

    def release(self, task_id: str) -> dict:
        self._task(task_id)
        repo = CollaborationRepository(self.context.controller.db)
        if repo.get(task_id)['state'] == 'pausing':
            raise ServiceError(409, '执行尚未确认退出，请先重新确认暂停')
        # Releasing control is deliberately separate from resuming side effects.
        return repo.set(task_id, 'automatic', {'message': '接管已结束；任务仍暂停，请核对产物后使用恢复任务'})

    async def reply(self, task_id: str, payload: dict) -> dict:
        task = self._task(task_id)
        controller = self.context.controller
        message = str(payload.get('message', '')).strip()
        if not message or len(message) > 8000:
            raise ServiceError(422, '回复需为1至8000字符')
        if CollaborationRepository(controller.db).get(task_id)['state'] != 'automatic':
            raise ServiceError(409, '人工接管期间不向自动执行注入回复')
        if not task.pm_agent_id:
            raise ServiceError(409, '任务尚未分配PM')
        return await run_in_threadpool(controller.inject_input_to_pm, task.pm_agent_id,
                                      {'response': message, 'choice': ''})

    def mark_read(self, task_id: str, payload: dict | None = None) -> dict:
        self._task(task_id)
        repo = CollaborationRepository(self.context.controller.db)
        event_id = str((payload or {}).get('event_id', ''))
        return {'ok': True, 'marked': repo.mark_read(task_id, event_id),
                'unread_count': repo.unread_count(task_id), 'shared_identity': True}
