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
        return dict(task=task.to_dict(), pm=pm.to_dict() if pm else None, teams=teams,
                    graph=graph, conversation_id=conv_id or '', pm_thread_id=task.pm_agent_id,
                    takeover=repo.get(task_id), notices=repo.notices(task_id),
                    clarification=state.get('clarification_question', ''))

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
