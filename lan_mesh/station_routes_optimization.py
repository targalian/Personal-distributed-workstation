"""Station routes for the resident workstation optimization workflow."""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from typing import Literal


class OptimizationCreateRequest(BaseModel):
    """Boss/bottleneck/Agent optimization item submission."""

    source: Literal["boss", "bottleneck", "agent"]
    title: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=4000)
    priority: Literal["low", "normal", "high", "urgent"] = "normal"


class OptimizationDecisionRequest(BaseModel):
    """Boss decision for a candidate optimization item."""

    decision: Literal["approve", "reject", "clarify"]
    reply: str = Field(default="", max_length=4000)


def build_optimization_routes(controller: object) -> APIRouter:
    """Build workstation optimization API routes."""
    router = APIRouter()

    def manager():
        optimizer = getattr(controller, "workstation_optimizer", None)
        if optimizer is None:
            raise HTTPException(status_code=503, detail="工作站优化守护未初始化")
        return optimizer

    @router.get("/api/workstation-optimization/summary")
    def optimization_summary() -> dict:
        """Return optimization guardian and queue summary."""
        return manager().summary()

    @router.get("/api/workstation-optimization/items")
    def optimization_items(status: str = "", limit: int = 100) -> dict:
        """List optimization items."""
        if limit < 1 or limit > 500:
            raise HTTPException(status_code=422, detail="limit 必须在 1-500")
        return {"items": manager().list_items(status=status, limit=limit)}

    @router.post("/api/workstation-optimization/items", status_code=201)
    def create_optimization_item(payload: OptimizationCreateRequest) -> dict:
        """Create an optimization item."""
        try:
            item = manager().submit(
                source=payload.source, title=payload.title,
                description=payload.description, priority=payload.priority)
            print(f"[Station] 工作站优化 API 项已创建: {item['id']}", flush=True)
            return {"item": item}
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/api/workstation-optimization/items/{item_id}/decision")
    def decide_optimization_item(item_id: str,
                                 payload: OptimizationDecisionRequest) -> dict:
        """Apply a Boss decision to an optimization item."""
        try:
            item = manager().decide(
                item_id=item_id, decision=payload.decision, reply=payload.reply)
            return {"item": item}
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="优化项不存在") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    return router
