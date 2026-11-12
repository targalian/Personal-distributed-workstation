"""Authenticated mesh task/result delivery endpoints shared by Station and Worker."""

from fastapi import APIRouter, HTTPException
from starlette.concurrency import run_in_threadpool

from .mesh_delivery_service import call_delivery


def build_delivery_routes(owner: object) -> APIRouter:
    """Add protocol v1 routes while retaining every legacy endpoint."""
    router = APIRouter()

    async def accept(payload: dict, operation: str) -> dict:
        try:
            return await run_in_threadpool(call_delivery, owner, operation, payload=payload)
        except (ValueError, TypeError, KeyError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/mesh-delivery/task")
    async def receive_task(payload: dict) -> dict:
        return await accept(payload, "accept_task")

    @router.post("/mesh-delivery/result")
    async def receive_result(payload: dict) -> dict:
        return await accept(payload, "accept_result")

    @router.post("/mesh-delivery/cancel")
    async def receive_cancel(payload: dict) -> dict:
        return await accept(payload, "accept_cancel")

    @router.get("/mesh-delivery/status")
    async def delivery_status() -> dict:
        return await run_in_threadpool(call_delivery, owner, "inspect")

    return router
