"""Explicit human browser commands share mesh authentication and task scope checks."""

from fastapi import APIRouter, HTTPException
from starlette.concurrency import run_in_threadpool
from .browser_session_service import BrowserSessionService
from .component_service import call_component
from .service_runtime import ServiceError


def build_browser_routes(owner: object) -> APIRouter:
    router = APIRouter()

    async def invoke(service_operation: str, **arguments: object) -> dict:
        try:
            return await run_in_threadpool(call_component, owner, BrowserSessionService, service_operation, **arguments)
        except ServiceError as exc:
            raise HTTPException(exc.status_code, exc.detail) from exc
        except Exception as exc:
            raise HTTPException(409, '浏览器动作未完成，请刷新画面后核对') from exc

    def scope(task_id: str) -> tuple[str, str]:
        db = getattr(owner, 'db', None)
        task = db.get_task(task_id) if db else None
        if not task:
            raise HTTPException(404, '该主机没有此任务，请在任务所属主机创建浏览器')
        return task.task_id, task.project_id or ''

    @router.post('/api/browser/sessions')
    async def create(payload: dict) -> dict:
        task_id, project_id = scope(str(payload.get('task_id', '')))
        origins = payload.get('allowed_origins', [])
        if not isinstance(origins, list) or len(origins) > 20 or any(not isinstance(v, str) for v in origins):
            raise HTTPException(422, '网址来源需为最多20项的字符串列表')
        return await invoke('create', task_id=task_id, project_id=project_id, allowed_origins=origins)

    @router.post('/api/browser/sessions/{session_id}/{operation}')
    async def command(session_id: str, operation: str, payload: dict) -> dict:
        task_id, project_id = scope(str(payload.get('task_id', '')))
        action = str(payload.get('operation', 'snapshot')) if operation == 'actions' else operation
        if operation not in {'actions', 'takeover', 'release', 'close'}:
            raise HTTPException(404, '浏览器操作不存在')
        return await invoke('command', session_id=session_id, operation=action, params=payload,
                            task_id=task_id, project_id=project_id, actor='human')
    return router
