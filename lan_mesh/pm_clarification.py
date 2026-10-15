"""Persist question identities and answers before acknowledging or continuing a PM."""

import copy
import math
import time
import uuid
from pathlib import Path
from urllib.parse import quote

from .http_retry import http_get, http_post
from .pm_checkpoint import read_checkpoint


def wait_for_answer(pm, question: str, options: list | None, timeout: float | None, continuation: dict | None) -> dict:
    st = pm._state
    with st.lock:
        if st.stop_event.is_set():
            return {"stopped": True}
        previous = st.clarification_request
        reuse = bool(
            continuation
            and previous.get("format") == 1
            and not previous.get("consumed")
            and previous.get("continuation") == continuation
        )
        if reuse:
            if previous["question"] != question or previous["options"] != (options or []):
                raise ValueError("澄清继续点与原问题不匹配")
        else:
            duration = st.clarification_timeout if timeout is None else timeout
            if not isinstance(duration, (int, float)) or not math.isfinite(duration) or duration < 0:
                raise ValueError("澄清等待时间无效")
            now = time.time()
            st.clarification_request = {
                "format": 1,
                "question_id": uuid.uuid4().hex[:16],
                "question": question,
                "options": options or [],
                "asked_at": now,
                "deadline": min(now + duration, st.start_time + st.global_timeout),
                "timeout": duration,
                "continuation": continuation or {},
                "consumed": False,
            }
            st.clarification_response = {}
        st.clarification_event.clear()
        if st.clarification_response:
            st.clarification_event.set()
        if not st.persist("awaiting_input"):
            return {"checkpoint_failed": True}
        request = dict(st.clarification_request)
    pm.report_status(
        "awaiting_input",
        clarification=request,
        task_list=[{"name": "请求决策", "status": "awaiting_input", "description": question}],
    )
    pm.report_progress(-1.0, "awaiting_input", question, reporter_type="pm_clarification", task_name=question[:100])
    remaining = max(0.0, request["deadline"] - time.time())
    st.clarification_event.wait(remaining)
    with st.lock:
        if st.stop_event.is_set():
            return {"stopped": True}
        if st.clarification_response:
            return copy.deepcopy(st.clarification_response)
        st.clarification_response = {
            "question_id": request["question_id"],
            "timed_out": True,
            "response": "",
            "choice": "",
        }
        if not st.persist():
            return {"checkpoint_failed": True}
        return dict(st.clarification_response)


def receive_answer(pm, payload: dict) -> dict:
    if not isinstance(payload, dict) or not isinstance(payload.get("question_id"), str) or not payload["question_id"]:
        return {"ok": False, "message": "缺少有效问题编号，请刷新当前问题后回复"}
    st = pm._state
    with st.lock:
        request = st.clarification_request
        historical = st.clarification_history.get(payload.get("question_id"), {})
        if historical:
            request = historical["request"]
        identity = {
            "pm_id": pm.pm_id,
            "task_id": st.task.get("task_id"),
            "device_id": pm.device_id,
            "question_id": request.get("question_id"),
        }
        if not identity["question_id"] or any(payload.get(k) != v for k, v in identity.items()):
            return {"ok": False, "message": "回复的节点、PM、任务或问题编号不匹配，请刷新问题"}
        answer = payload.get("response") or payload.get("choice")
        if not isinstance(answer, str) or not answer.strip() or len(answer) > 20000:
            return {"ok": False, "message": "回复须为 1–20000 字符的非空文本"}
        normalized = {"question_id": identity["question_id"], "response": answer.strip(), "choice": answer.strip()}
        saved = historical.get("response", {}) if historical else st.clarification_response
        if saved == normalized:
            return {"ok": True, **identity, "duplicate": True, "message": "该回复已保存，未重复应用"}
        if (
            payload.get("duplicate_only")
            or historical
            or st.stop_event.is_set()
            or st.clarification_response
            or request.get("consumed")
            or time.time() >= request["deadline"]
        ):
            return {"ok": False, "message": "该问题已结束、已回复或任务已停止，请刷新当前问题"}
        st.clarification_response = normalized
        if not st.persist():
            st.clarification_response = {}
            return {"ok": False, "message": "回复保存失败，尚未确认接收，请核实原节点"}
        st.clarification_event.set()
        return {"ok": True, **identity, "duplicate": False, "message": "回复已保存到原 PM 执行现场"}


def inspect_question(path: Path, pm_id: str, task_id: str, device_id: str, current) -> dict:
    """Expose only the pending question and its answer, including while the PM is down."""
    record = read_checkpoint(path, pm_id, task_id, device_id)
    snapshot = record["snapshot"] if record else {}
    pm = current()
    if pm and pm.pm_id == pm_id and pm._state.task.get("task_id") == task_id and pm.device_id == device_id:
        if record is None and pm._checkpoint_store is None:
            with pm._state.lock:
                snapshot = pm._state.snapshot()
        running = pm.running
    else:
        running = False
    request = snapshot.get("clarification_request", {})
    response = snapshot.get("clarification_response", {})
    state = (
        "answered"
        if response and not response.get("timed_out")
        else "closed"
        if response or request.get("consumed") or snapshot.get("stopped") or time.time() >= request.get("deadline", 0)
        else "waiting"
    )
    return {
        "pm_id": pm_id,
        "task_id": task_id,
        "device_id": device_id,
        "question_id": request.get("question_id", ""),
        "question": request.get("question", ""),
        "options": request.get("options", []),
        "deadline": request.get("deadline"),
        "response": response.get("response", ""),
        "state": state if request.get("format") == 1 else "missing",
        "running": running,
        "can_reply": running and state == "waiting" and not snapshot.get("stopped"),
    }


def _owner(controller, pm_id: str):
    pm = controller.db.get_pm_agent(pm_id)
    task = controller.db.get_task(pm.task_id) if pm else None
    if task is None or task.pm_agent_id != pm_id:
        raise ValueError("原任务或 PM 归属不存在")
    return pm, task


def query_question(controller, pm_id: str) -> dict:
    pm, task = _owner(controller, pm_id)
    if pm.device_id == controller.state.device_id:
        result = controller._local_pm_question(pm_id, task.task_id)
    else:
        response = http_get(
            f"http://{pm.ip}:{pm.api_port}/role/pm-clarification/{quote(pm_id, safe='')}",
            params={"task_id": task.task_id},
            timeout=5,
            retries=0,
            allow_redirects=False,
        )
        result = response.json() if response is not None and response.status_code == 200 else {}
    if not isinstance(result, dict) or any(
        result.get(k) != v for k, v in {"pm_id": pm_id, "task_id": task.task_id, "device_id": pm.device_id}.items()
    ):
        raise ValueError("无法核实原问题，请检查原节点连接")
    latest_pm, latest = _owner(controller, pm_id)
    if any(getattr(latest_pm, key) != getattr(pm, key) for key in ("task_id", "device_id", "ip", "api_port")):
        raise ValueError("查询期间任务归属变化，请重新读取问题")
    allowed = (
        "pm_id",
        "task_id",
        "device_id",
        "question_id",
        "question",
        "options",
        "deadline",
        "response",
        "state",
        "running",
        "can_reply",
    )
    result = {k: result[k] for k in allowed if k in result}
    if (
        latest.status in ("cancelled", "paused", "completed", "accepted", "rejected", "failed")
        or "_pm_control" in latest.output_data
        or "_delivery" in latest.output_data
    ):
        result["can_reply"] = False
    return result


def route_answer(controller, pm_id: str, payload: dict) -> dict:
    """Route by durable ownership; never infer the question ID for an old reply."""
    try:
        if not isinstance(payload, dict):
            raise ValueError("回复格式无效")
        pm, task = _owner(controller, pm_id)
        duplicate_only = (
            task.status not in ("running", "planning", "executing", "monitoring", "awaiting_input", "interrupted")
            or "_pm_control" in task.output_data
            or "_delivery" in task.output_data
        )
        if not isinstance(payload.get("question_id"), str) or not payload["question_id"]:
            raise ValueError("缺少问题编号，请刷新当前问题后回复")
        request = {
            **payload,
            "pm_id": pm_id,
            "task_id": task.task_id,
            "device_id": pm.device_id,
            "duplicate_only": duplicate_only,
        }
        if pm.device_id == controller.state.device_id:
            result = controller._local_inject_input(request)
        else:
            response = http_post(
                f"http://{pm.ip}:{pm.api_port}/pm/inject-input",
                json=request,
                timeout=5,
                retries=0,
                allow_redirects=False,
            )
            result = response.json() if response is not None and response.status_code == 200 else {}
        if (
            isinstance(result, dict)
            and result.get("ok") is True
            and all(result.get(k) == request[k] for k in ("pm_id", "task_id", "device_id", "question_id"))
        ):
            return result
        message = result.get("message") if isinstance(result, dict) else None
        return {"ok": False, "message": message or "回复未获确认；保留原文，核实问题后可重试同一回复"}
    except Exception as exc:
        return {"ok": False, "message": f"回复未获确认：{exc}；请保留原文并核实原问题"}


def valid_refinement(snapshot: dict) -> bool:
    progress = snapshot.get("refinement", {})
    request = snapshot.get("clarification_request", {})
    return (
        progress.get("format") == 1
        and type(progress.get("round")) is int
        and 0 <= progress["round"] < 2
        and isinstance(progress.get("context"), list)
        and len(progress["context"]) == progress["round"]
        and progress.get("task", {}).get("task_id") == snapshot.get("task", {}).get("task_id")
        and request.get("format") == 1
        and bool(request.get("question_id"))
        and not request.get("consumed")
        and request.get("continuation") == {"kind": "refinement", "round": progress["round"]}
        and isinstance(request.get("deadline"), (float, int))
        and math.isfinite(request["deadline"])
        and not snapshot.get("execution_attempts")
        and not snapshot.get("direct_execution")
        and not snapshot.get("dispatched")
    )
