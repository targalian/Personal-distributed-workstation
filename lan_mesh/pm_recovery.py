"""Read checkpoint and original execution receipts to assess recovery without running work."""
import ipaddress
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from .execution_routes import normalize_result
from .http_retry import http_get
from .pm_checkpoint import read_checkpoint
from .pm_dispatcher import PMDispatcher

STEP_MESSAGES = {
    "saved_completed": "成功输出已保存，可作为后续恢复依据",
    "receipt_completed": "已取得成功回执，尚需 PM 质量检查",
    "awaiting_result": "原执行仍在等待结果，不重派",
    "result_unknown": "执行结果待核实，不能按未执行处理",
    "failed": "原执行已返回失败，需核对原因与副作用",
    "unstarted": "检查点未登记执行请求，后续仍需恢复授权",
    "invalid": "执行编号或节点归属不匹配，不能采信回执",
    "not_checked": "本次查询预算已用完，请再次核实",
}


def _receipt_status(attempt: dict, receipt: dict) -> str:
    if not isinstance(receipt, dict) or any(not attempt.get(k) or receipt.get(k) != attempt.get(k)
                                         for k in ("subtask_id", "parent_task_id", "pm_id", "reporter_id", "name")):
        return "invalid"
    if receipt.get("status") == "completed":
        return "receipt_completed" if normalize_result(receipt)["status"] == "completed" else "failed"
    return {"failed": "failed", "started": "awaiting_result", "unknown": "result_unknown"}.get(
        receipt.get("status"), "result_unknown")


def _remote_receipt(attempt: dict, deadline: float, nodes: dict) -> str:
    """Use only the persisted address; verify node identity before reading its receipt."""
    station = attempt.get("station") or {}
    try:
        address = ipaddress.ip_address(station["ip"])
        port = station["api_port"]
        if type(port) is not int or not 1 <= port <= 65535 or not station.get("device_id"):
            return "invalid"
        host = f"[{address}]" if address.version == 6 else str(address)
        base = f"http://{host}:{port}"
        key = (base, station["device_id"])
        def get(path):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            return http_get(base + path, timeout=min(2.0, remaining), retries=0, allow_redirects=False)
        if key not in nodes:
            response = get("/info")
            if response is None:
                return "not_checked" if time.monotonic() >= deadline else "result_unknown"
            body = response.json() if response.status_code == 200 else {}
            nodes[key] = isinstance(body, dict) and body.get("device_id") == station["device_id"]
        if not nodes[key]:
            return "invalid"
        response = get("/tasks/executions/" + quote(attempt["subtask_id"], safe=""))
        if response is None:
            return "not_checked" if time.monotonic() >= deadline else "result_unknown"
        if response.status_code != 200:
            return "result_unknown"  # Missing receipt never proves absence of execution.
        return _receipt_status(attempt, response.json())
    except (KeyError, TypeError, ValueError):
        return "invalid"
    except Exception:
        return "result_unknown"


def _step_status(name: str, snapshot: dict, pm_id: str, task_id: str, deadline: float, nodes: dict) -> tuple[str, str]:
    if name in snapshot["subtask_outputs"]:
        return "saved_completed", ""
    if name in snapshot.get("terminal_failures", {}):
        return "failed", snapshot["execution_attempts"].get(name, {}).get("subtask_id", "")
    attempt = snapshot["execution_attempts"].get(name)
    if not attempt:
        return ("result_unknown" if name in snapshot["dispatched"] else "unstarted"), ""
    execution_id = attempt.get("subtask_id", "")
    if (attempt.get("pm_id") != pm_id or attempt.get("parent_task_id") != task_id
            or attempt.get("name") != name or not isinstance(execution_id, str) or not execution_id):
        return "invalid", ""
    report = attempt.get("received_result")
    if report:
        receipt = {**report, "name": report.get("task_name"), "error": report.get("error", "")}
        return _receipt_status(attempt, receipt), execution_id
    if not attempt.get("station"):
        return "result_unknown", execution_id  # Local tools have no separate durable result ledger.
    if time.monotonic() >= deadline:
        return "not_checked", execution_id
    return _remote_receipt(attempt, deadline, nodes), execution_id


def assess_pm_recovery(path: Path, pm_id: str, task_id: str, device_id: str, current, budget: float = 8.0, offset: int = 0, expected_revision: int | None = None) -> dict:
    """Collect bounded read-only evidence; never change checkpoint ownership or outputs."""
    result = {"format": 1, "pm_id": pm_id, "task_id": task_id, "device_id": device_id,
              "state": "unavailable", "steps": [], "blockers": [], "resume_supported": False,
              "checked_at": datetime.now(timezone.utc).isoformat()}
    before = current()
    if before.get("pm_id") == pm_id and (before.get("task_id") != task_id or before.get("device_id") != device_id):
        return {**result, "state": "mismatch", "blockers": ["当前 PM 与检查点归属不匹配"]}
    if before.get("pm_id") == pm_id and before.get("running") is True:
        return {**result, "state": "running", "blockers": ["原 PM 仍在运行，请等待或先停止并核实"]}
    try:
        record = read_checkpoint(path, pm_id, task_id, device_id)
        if record is None:
            return {**result, "state": "missing", "blockers": ["没有可核实的执行现场；缺失不证明未执行"]}
        if expected_revision is not None and record["revision"] != expected_revision:
            return {**result, "state": "changed", "blockers": ["检查点版本变化，请从头重新核实"]}
        snapshot = record["snapshot"]
        plan = snapshot["plan"]
        decomposition = plan.get("decomposition", [])
        if len(decomposition) > 150:
            raise ValueError("计划超出查询上限")
        if decomposition:
            PMDispatcher._validate_plan(decomposition)
        if (decomposition and not 0 <= offset < len(decomposition)) or (not decomposition and offset != 0):
            raise ValueError("查询起点超出计划范围")
        result.update(revision=record["revision"], checkpoint_at=record["updated_at"],
                      offset=offset, next_offset=None, total_steps=len(decomposition))
        if snapshot.get("stopped") or snapshot.get("stop_reason"):
            result["blockers"].append("检查点包含停止意图，不能直接恢复")
        if snapshot.get("phase") in ("failed", "completed"):
            result["blockers"].append("检查点已有终态，请先核对任务与交付")
        if time.time() - snapshot["start_time"] > snapshot["global_timeout"]:
            result["blockers"].append("原任务全局时限已耗尽，不能直接重置时限")
        if (snapshot.get("delivery", {}).get("state") == "acknowledged"
                or (snapshot.get("delivery", {}).get("state") == "sending" and not snapshot.get("process_lease"))):
            result["blockers"].append("已有交付请求或确认，请先核对 Secretary 的交付与验收记录")
        deadline, nodes = time.monotonic() + max(0, min(budget, 8.0)), {}
        direct = snapshot.get("direct_execution", {})
        for index, sub in enumerate(decomposition[offset:], start=offset):
            name = sub["name"]
            if plan.get("pattern") == "single" and direct:
                if name in snapshot["subtask_outputs"]:
                    status = "saved_completed"
                elif direct.get("state") == "returned":
                    returned = direct.get("result", {})
                    normalized = normalize_result({"status": returned.get("status"),
                                                   "output": {"summary": returned.get("summary", "")}})
                    status = "receipt_completed" if normalized["status"] == "completed" else "failed"
                else:
                    status = "result_unknown"
                execution_id = direct.get("execution_id", "")
            else:
                status, execution_id = _step_status(name, snapshot, pm_id, task_id, deadline, nodes)
            if status == "not_checked":
                result["next_offset"] = index
                break
            result["steps"].append({"name": name, "state": status, "execution_id": execution_id,
                                    "message": STEP_MESSAGES[status]})
        latest = read_checkpoint(path, pm_id, task_id, device_id)
        after = current()
        if (latest is None or latest["revision"] != record["revision"]
                or (after.get("pm_id") == pm_id and after.get("running") is True)):
            return {**result, "state": "changed", "steps": [], "blockers": ["查询期间执行现场变化，请重新核实"]}
        from .pm_lease import lease_identity
        from .pm_resume import validate_snapshot
        lease = lease_identity(path)
        try:
            validate_snapshot(snapshot, pm_id, task_id)
            result["resume_supported"] = bool(lease["machine"] and snapshot.get("process_lease") == lease)
            if not result["resume_supported"]:
                result["resume_message"] = "该现场缺少原进程排除凭据，或已迁移；不能直接续跑"
        except (ValueError, KeyError, TypeError) as exc:
            result["resume_message"] = str(exc)
        if snapshot.get("stop_reason") == "paused":
            from .pm_activity import retire_instances
            from .pm_continue import continuation_snapshot
            pause_id = snapshot.get("control_receipt", {}).get("request_id", "")
            result["pause_request_id"] = pause_id
            result["continue_supported"] = False
            try:
                continuation_snapshot(snapshot, pm_id, task_id, pause_id)
                retire_instances(path, pm_id, commit=False)
                if not lease["machine"] or snapshot.get("process_lease") != lease:
                    raise ValueError("暂停现场不在原 Linux 主机原路径，不能继续")
                result["continue_supported"] = True
                prepared = snapshot.get("continuation", {})
                if prepared.get("state") == "prepared" and prepared.get("pause_request_id") == pause_id:
                    result["continue_revision"] = prepared["expected_revision"]
                    result["continue_request_id"] = prepared["request_id"]
                result["continue_message"] = "可申请继续暂停任务；沿用原时限，原始结果仍需质量检查"
                result["blockers"] = [b for b in result["blockers"] if b != "检查点包含停止意图，不能直接恢复"]
            except (ValueError, KeyError, TypeError) as exc:
                result["continue_message"] = str(exc)
        unresolved = any(s["state"] not in ("saved_completed", "receipt_completed", "unstarted") for s in result["steps"])
        result["state"] = "review_required" if unresolved or result["blockers"] else "evidence_complete"
        if offset or result["next_offset"] is not None:
            result["state"] = "partial"
        return result
    except Exception:
        return {**result, "state": "unavailable", "steps": [], "blockers": ["执行现场无法验证，请保留原数据并检查日志"]}


def assess_task_recovery(controller, task_id: str, offset: int = 0, expected_revision: int | None = None) -> dict:
    """Query the original PM node and reject stale/foreign assessment responses."""
    task = controller.db.get_task(task_id)
    if task is None:
        raise KeyError(task_id)
    pm = controller.db.get_pm_agent(task.pm_agent_id) if task.pm_agent_id else None
    result = {"task_id": task_id, "state": "unavailable", "steps": [], "blockers": [], "resume_supported": False}
    if not pm or pm.task_id != task_id:
        return {**result, "blockers": ["任务没有有效的原 PM 归属"]}
    try:
        if pm.device_id == controller.state.device_id:
            evidence = controller._local_assess_pm_recovery(pm.pm_id, task_id, offset=offset, expected_revision=expected_revision)
        else:
            response = http_get(f"http://{pm.ip}:{pm.api_port}/role/pm-recovery/{quote(pm.pm_id, safe='')}",
                                params={"task_id": task_id, "offset": offset, **({"expected_revision": expected_revision} if expected_revision is not None else {})}, timeout=12, retries=0, allow_redirects=False)
            if response is None or response.status_code != 200:
                return {**result, "blockers": ["原节点不可达或尚不支持恢复核实，请升级或恢复连接后重试"]}
            evidence = response.json()
        if (not isinstance(evidence, dict) or evidence.get("format") != 1 or evidence.get("pm_id") != pm.pm_id
                or evidence.get("task_id") != task_id or evidence.get("device_id") != pm.device_id):
            return {**result, "state": "mismatch", "blockers": ["返回的节点或任务归属不匹配"]}
        allowed = ("state", "revision", "checkpoint_at", "checked_at", "blockers", "offset", "next_offset", "total_steps", "resume_message", "continue_message", "pause_request_id", "continue_revision", "continue_request_id")
        if (evidence.get("state") not in ("unavailable", "missing", "running", "changed", "mismatch", "review_required", "evidence_complete", "partial")
                or not isinstance(evidence.get("steps"), list) or len(evidence["steps"]) > 150
                or not isinstance(evidence.get("blockers"), list)
                or any(not isinstance(item, str) for item in evidence["blockers"])):
            raise ValueError("恢复核实响应格式错误")
        result.update({k: evidence[k] for k in allowed if k in evidence})
        result["resume_supported"] = evidence.get("resume_supported") is True
        result["continue_supported"] = evidence.get("continue_supported") is True
        for step in evidence["steps"]:
            if (not isinstance(step, dict) or step.get("state") not in STEP_MESSAGES
                    or not isinstance(step.get("name"), str) or not isinstance(step.get("execution_id"), str)):
                raise ValueError("执行步骤格式错误")
        result["steps"] = [{"name": step["name"], "state": step["state"], "execution_id": step["execution_id"],
                            "message": STEP_MESSAGES[step["state"]]} for step in evidence["steps"]]
    except Exception:
        return {**result, "state": "unavailable", "steps": [], "blockers": ["恢复证据查询失败，请稍后重试"]}
    latest = controller.db.get_task(task_id)
    latest_pm = controller.db.get_pm_agent(pm.pm_id)
    if (latest is None or latest.pm_agent_id != task.pm_agent_id or latest.status != task.status
            or latest.output_data != task.output_data or latest_pm is None
            or any(getattr(latest_pm, key) != getattr(pm, key) for key in ("task_id", "device_id", "ip", "api_port"))):
        return {**result, "state": "changed", "steps": [], "continue_supported": False, "resume_supported": False,
                "blockers": ["查询期间任务变化，请刷新任务后重试"]}
    if result.get("continue_supported"):
        from .task_continue import _eligible
        try:
            request = task.output_data.get("_pm_continue", {})
            prepared = bool(result.get("continue_request_id") and request.get("request_id") == result["continue_request_id"])
            _eligible(controller, task, request if prepared else None)
            pause_id = request.get("pause_request_id") if prepared else task.output_data["_pm_control"]["request_id"]
            if pause_id != result.get("pause_request_id"):
                raise ValueError("原暂停请求不匹配，请重新核实")
        except (KeyError, ValueError) as exc:
            result["continue_supported"] = False
            result["continue_message"] = str(exc)
    if result.get("continue_supported"):
        result["resume_supported"] = False
        return result
    if task.status not in ("running", "planning", "executing", "monitoring", "awaiting_input", "interrupted"):
        result["blockers"].append("当前任务状态不允许直接恢复")
    if "_delivery" in task.output_data or "_pm_control" in task.output_data:
        result["blockers"].append("任务已有交付或停止意图，请先核对业务记录")
    if controller.db._get_conn().execute("SELECT 1 FROM task_resubmissions WHERE source_task_id=?", (task_id,)).fetchone():
        result["blockers"].append("原任务已有替代任务，不能同时恢复原执行")
    if result["blockers"]:
        result["resume_supported"] = False
    if result["blockers"] and result["state"] == "evidence_complete":
        result["state"] = "review_required"
    return result
