"""Periodic and IM HTTP adapters reuse normal mesh/user role checks and Yomk ownership."""

from fastapi import APIRouter, HTTPException
from starlette.concurrency import run_in_threadpool
from .component_service import call_component
from .im_service import ImService
from .periodic_service import PeriodicService
from .service_runtime import ServiceError


def build_periodic_routes(owner: object) -> APIRouter:
    router = APIRouter()

    async def invoke(component: type, operation: str, **arguments: object) -> dict:
        try:
            return await run_in_threadpool(call_component, owner, component, operation, **arguments)
        except ServiceError as exc:
            raise HTTPException(exc.status_code, exc.detail) from exc

    @router.get('/api/schedules')
    async def schedules(conv_id: str) -> dict:
        return await invoke(PeriodicService, 'list_schedules', conv_id=conv_id)

    @router.post('/api/schedules')
    async def create_schedule(conv_id: str, payload: dict) -> dict:
        return await invoke(PeriodicService, 'create', conv_id=conv_id, payload=payload)

    @router.put('/api/schedules/{schedule_id}')
    async def update_schedule(schedule_id: str, conv_id: str, payload: dict) -> dict:
        return await invoke(PeriodicService, 'update', schedule_id=schedule_id, conv_id=conv_id, payload=payload)

    @router.get('/api/schedules/{schedule_id}/runs')
    async def runs(schedule_id: str, conv_id: str) -> dict:
        return await invoke(PeriodicService, 'history', schedule_id=schedule_id, conv_id=conv_id)

    @router.post('/api/schedules/{schedule_id}/runs/{run_id}/resolve')
    async def resolve(schedule_id: str, run_id: str, conv_id: str, payload: dict) -> dict:
        return await invoke(PeriodicService, 'resolve_run', schedule_id=schedule_id, run_id=run_id, conv_id=conv_id, payload=payload)

    @router.get('/api/station/im-bindings')
    async def bindings(conv_id: str) -> dict:
        return await invoke(ImService, 'bindings', conv_id=conv_id)

    @router.post('/api/station/im-bindings')
    async def bind(conv_id: str, payload: dict) -> dict:
        return await invoke(ImService, 'bind', conv_id=conv_id, payload=payload)

    @router.get('/api/im/events')
    async def events(conv_id: str) -> dict:
        return await invoke(ImService, 'events', conv_id=conv_id)

    @router.post('/api/im/{channel}/messages')
    async def message(channel: str, payload: dict) -> dict:
        return await invoke(ImService, 'receive', channel=channel, payload=payload)
    return router
