"""Apply identity-bound PM stop requests and persist the distinction between intent and acknowledgment."""
from typing import TYPE_CHECKING, Callable
from urllib.parse import quote

from fastapi import APIRouter, HTTPException
from starlette.concurrency import run_in_threadpool

from .http_retry import http_get, http_post
from .logger import get_logger
from .pm_start import _START_LOCK

if TYPE_CHECKING:
    from .station_controller import StationController

logger = get_logger("pm_control")


def control_pm(current: Callable, device_id: str, payload: dict) -> dict:
    """Signal only the requested PM; a late pause cannot downgrade cancellation."""
    identity = {key: payload.get(key, "") for key in ("pm_id", "task_id", "device_id", "request_id", "action")}
    if (not all(isinstance(value, str) and value for value in identity.values())
            or identity["action"] not in ("cancel", "pause") or identity["device_id"] != device_id):
        return {"ok": False, "message": "停止请求缺少身份或节点不匹配"}
    with _START_LOCK:
        pm = current()
        if pm is None:
            return {"ok": False, "message": "原 PM 实例不存在，请核实启动记录及工具产物"}
        with pm._state.lock:
            if (pm.pm_id != identity["pm_id"] or pm._state.task.get("task_id") != identity["task_id"]
                    or pm.device_id != device_id):
                return {"ok": False, "message": "当前 PM 或任务不匹配，未停止其他任务"}
            if pm._state.stop_reason == "cancelled" and identity["action"] == "pause":
                return {"ok": False, "message": "PM 已取消，拒绝晚到的暂停请求"}
            if pm._state.phase in ("completed", "failed"):
                return {"ok": False, "message": "PM 已有终态，拒绝修改停止意图"}
            if (not pm._checkpoint_error and _matches(pm._state.control_receipt, identity)
                    and pm._state.stop_event.is_set()):
                return dict(pm._state.control_receipt)
            receipt = {"ok": True, **identity, "scope": "pm_cooperative"}
            previous = dict(pm._state.control_receipt)
            pm._state.control_receipt = receipt
            pm._signal_stop("cancelled" if identity["action"] == "cancel" else "paused")
            if pm._checkpoint_error:
                pm._state.control_receipt = previous
                return {"ok": False, "message": "PM 已设置停止信号，但检查点保存失败；停止确认仍待核实"}
            logger.info("PM %s 任务 %s 已设置协作停止信号: %s (请求 %s)",
                        pm.pm_id, identity["task_id"], identity["action"], identity["request_id"])
            # The Secretary has already persisted intent. Do not block this
            # acknowledgment on synchronous callbacks to that same Secretary.
            return dict(receipt)


def _matches(receipt: dict, request: dict) -> bool:
    return (isinstance(receipt, dict) and receipt.get("ok") is True
            and receipt.get("scope") == "pm_cooperative"
            and all(receipt.get(key) == value for key, value in request.items()))


def request_task_control(controller: "StationController", task_id: str, action: str) -> dict:
    """Fence late results before sending once; keep ambiguous stops visible and retryable."""
    try:
        task = controller.db.begin_task_control(task_id, action)
    except (KeyError, ValueError) as exc:
        return {"ok": False, "message": str(exc)}
    control = task.output_data["_pm_control"]
    if control["state"] != "confirmed":
        pm = controller.db.get_pm_agent(task.pm_agent_id)
        confirmed = False
        message = "原 PM 归属缺失，停止结果待核实"
        if pm and pm.task_id == task_id:
            request = {"pm_id": pm.pm_id, "task_id": task_id, "device_id": pm.device_id,
                       "request_id": control["request_id"], "action": action}
            try:
                if pm.device_id == controller.state.device_id:
                    receipt = controller._local_control_pm(request)
                else:
                    response = http_post(f"http://{pm.ip}:{pm.api_port}/role/pm-control", json=request,
                                         timeout=5, retries=0, allow_redirects=False)
                    receipt = response.json() if response is not None and response.status_code == 200 else {}
                confirmed = _matches(receipt, request)
            except Exception:
                confirmed = False
            if not confirmed:
                # A lost acknowledgment can be checked, but never causes a second POST.
                try:
                    if pm.device_id == controller.state.device_id:
                        evidence = controller._local_inspect_pm_start(pm.pm_id, task_id)
                    else:
                        response = http_get(f"http://{pm.ip}:{pm.api_port}/role/pm-starts/{quote(pm.pm_id, safe='')}",
                                            params={"task_id": task_id}, timeout=5, retries=0, allow_redirects=False)
                        evidence = response.json() if response is not None and response.status_code == 200 else {}
                    confirmed = (evidence.get("format") == 1 and evidence.get("pm_id") == pm.pm_id
                                 and evidence.get("state") not in ("mismatch", "unavailable", "changed")
                                 and evidence.get("task_id") == task_id and evidence.get("device_id") == pm.device_id
                                 and _matches(evidence.get("control_receipt", {}), request))
                except Exception:
                    pass
            message = ("原节点已确认 PM 停止后续调度；在途模型和工具仍需核实" if confirmed else
                       "已登记停止意图，原节点未确认；请核实原节点、网络与版本，必要时重发停止请求")
        controller.db.finish_task_control(task_id, control["request_id"], confirmed, message)
    latest = controller.db.get_task(task_id)
    if latest is None or latest.pm_agent_id != task.pm_agent_id:
        return {"ok": False, "message": "任务归属已变化，请刷新后核实"}
    current = latest.output_data.get("_pm_control", {})
    if current.get("request_id") != control["request_id"]:
        return {"ok": False, "message": "停止请求已被较新的操作替代，请刷新任务"}
    try:
        controller._queue_ws_broadcast("task_updated", latest.to_dict())
    except Exception:
        pass
    if current.get("state") == "confirmed" and control.get("state") != "confirmed":
        try:
            controller.bot_gateway.notify(f"task_{latest.status}", {"task_id": task_id, "name": latest.name})
        except Exception:
            pass
    return {"ok": True, "confirmed": current.get("state") == "confirmed", "task_id": task_id,
            "status": latest.status, "control": current, "message": current.get("message", "停止结果待核实")}


def build_pm_control_routes(handler: Callable) -> APIRouter:
    """Keep legacy paths, but require identity on every stop operation."""
    router = APIRouter()

    async def apply(payload: dict, action: str = ""):
        result = await run_in_threadpool(handler, {**payload, **({"action": action} if action else {})})
        if not result.get("ok"):
            raise HTTPException(status_code=409, detail=result.get("message", "原 PM 未确认停止"))
        return result

    @router.post("/role/pm-control")
    async def control(payload: dict):
        return await apply(payload)

    @router.post("/role/cancel-pm")
    @router.post("/role/stop-pm")
    async def cancel(payload: dict):
        return await apply(payload, "cancel")

    @router.post("/role/pause-pm")
    async def pause(payload: dict):
        return await apply(payload, "pause")

    return router
