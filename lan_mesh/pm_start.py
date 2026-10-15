"""Start a preassigned PM once, after verifying its persisted task ownership."""
import threading
from pathlib import Path
from typing import Callable

from .execution_receipts import PROCESS_OWNER, ExecutionReceipts
from .pm_agent import ProjectManagerAgent
from .pm_checkpoint import PMCheckpoints

_START_LOCK = threading.RLock()


def start_pm_once(*, pm_id: str, task_id: str, secretary_url: str, task_data: dict,
                  runtime, device_id: str, device_name: str, receipt_path: Path,
                  current: Callable, assign: Callable, lookup: Callable) -> dict:
    """Repeated starts return the first outcome; restart uncertainty never launches again."""
    if not pm_id or not isinstance(task_data, dict) or task_data.get("task_id") != task_id or task_data.get("pm_agent_id") != pm_id:
        return {"ok": False, "uncertain": False, "message": "缺少预登记 PM 编号或任务关联不匹配"}
    payload = {"subtask_id": pm_id, "pm_id": pm_id, "parent_task_id": task_id, "name": "PM startup",
               "input_data": {"task": task_data, "secretary_url": secretary_url}}
    with _START_LOCK:
        store = ExecutionReceipts(receipt_path)
        try:
            claimed, receipt = store.claim(payload)
        except ValueError as exc:
            return {"ok": False, "uncertain": True, "pm_id": pm_id, "message": str(exc)}
        if not claimed:
            previous = receipt.get("output", {})
            if previous.get("process_owner") != PROCESS_OWNER:
                return {"ok": False, "uncertain": True, "pm_id": pm_id,
                        "message": "PM 启动已有记录，进程或结果待核实；不自动重启"}
            return {k: v for k, v in {**previous, "duplicate": True}.items() if k != "process_owner"}
        result = {"ok": False, "uncertain": False, "pm_id": pm_id, "task_id": task_id, "device_id": device_id}
        try:
            active = current()
            if active and active.running:
                result["message"] = "本机 PM Agent 已在运行"
            elif runtime is None:
                result["message"] = "AgentRuntime 未初始化"
            else:
                authoritative = lookup(task_id)
                if (not authoritative or authoritative.get("pm_agent_id") != pm_id
                        or authoritative.get("task_id") != task_id or authoritative.get("status") != "running"):
                    result["message"] = "任务未授权该 PM 启动，或任务已停止"
                else:
                    pm = ProjectManagerAgent(pm_id, runtime, secretary_url, device_id, device_name)
                    pm._checkpoint_store = PMCheckpoints(receipt_path)
                    assign(pm)
                    # start_task can raise after creating a thread. Do not claim
                    # that failure proves no execution occurred.
                    result["uncertain"] = True
                    pm.start_task(authoritative)
                    result.update(ok=True, uncertain=False)
        except Exception as exc:
            result["message"] = f"PM 启动异常：{exc}"
        store.finish(pm_id, {"status": "completed", "output": {**result, "process_owner": PROCESS_OWNER}})
        return result
