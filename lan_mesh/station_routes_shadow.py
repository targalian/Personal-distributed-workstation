"""影子开发 API 路由 - 提交、查询与守护状态接口。"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .logger import get_logger

logger = get_logger("station")


class ShadowRunRequest(BaseModel):
    """影子开发任务提交请求。"""

    task: str = Field(min_length=1, max_length=4000)
    backend: str = Field(default="", max_length=32)
    timeout: int = Field(default=1800, ge=30, le=1800)


def build_shadow_dev_routes(controller: object) -> APIRouter:
    """构建影子开发 API 路由。"""
    router = APIRouter()

    def _manager():
        manager = getattr(controller, "shadow_dev_manager", None)
        if manager is None:
            logger.warning("[Station] 影子开发守护未初始化")
            raise HTTPException(status_code=503, detail="影子开发守护未初始化")
        return manager

    @router.post("/api/shadow-dev/runs", status_code=202)
    def create_shadow_run(payload: ShadowRunRequest) -> dict:
        """提交影子开发任务, 由常驻守护串行执行。"""
        manager = _manager()
        try:
            record = manager.submit(
                task=payload.task, backend=payload.backend,
                timeout=payload.timeout, simulate=False)
            print(f"[Station] 影子任务已提交: {record['run_id']}", flush=True)
            return record
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.get("/api/shadow-dev/runs")
    def list_shadow_runs() -> dict:
        """列出队列中的运行与历史报告。"""
        manager = _manager()
        return {"runs": manager.list_runs(), "guardian": manager.status()}

    @router.get("/api/shadow-dev/runs/{run_id}")
    def get_shadow_run(run_id: str) -> dict:
        """获取单次影子运行详情。"""
        report = _manager().get_run(run_id)
        if report is None:
            raise HTTPException(status_code=404, detail="运行不存在")
        return report

    @router.get("/api/shadow-dev/status")
    def get_shadow_status() -> dict:
        """获取常驻守护状态。"""
        return _manager().status()

    return router
