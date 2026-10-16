"""Read original-node PM evidence without dispatching work or changing task state."""
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from .http_retry import http_get
from .pm_checkpoint import inspect_checkpoint, read_checkpoint

GUIDANCE = {
    "running": ("原节点 PM 仍在运行", "等待进度或交付；需要停止时使用暂停或取消，并核实在途工具是否结束。"),
    "inactive": ("原节点 PM 已停止", "核对已保存交付、原节点日志及外部工具产物；停止不代表已完成，也不保证工具已停止。"),
    "start_failed": ("原节点记录启动未成功", "检查模型配置、节点容量及启动日志；处理原因后再决定是否创建新任务。"),
    "unknown": ("原节点有启动记录，执行结果待核实", "核对原节点日志、交付和工具产物，再核实恢复条件；不要删除回执或直接重派。旧版现场不支持 PM 重启续跑。"),
    "not_found": ("原节点未找到该 PM 启动记录", "检查是否连接原节点、是否恢复过旧备份，以及节点日志；记录缺失不能证明任务未执行。"),
    "unavailable": ("暂时无法核实原节点", "检查原节点进程、网络与认证，恢复连接后再次核实。"),
    "unsupported": ("原节点尚不支持按任务核实", "将原节点升级到支持 PM 启动记录查询的版本后再次核实。"),
    "mismatch": ("节点或任务归属不匹配", "核对原节点身份与地址；不要将其他任务的状态当作本任务结果。"),
    "unbound": ("任务没有可核实的 PM 归属", "检查任务提交结果及调度记录。"),
    "changed": ("查询期间任务已变化", "关闭详情并刷新任务，再次核实当前任务。"),
}


def inspect_pm_start(path: Path, pm_id: str, task_id: str, device_id: str, current: dict) -> dict:
    """Read a startup receipt and current PM; never create a receipt database."""
    result = {"format": 1, "pm_id": pm_id, "task_id": task_id, "device_id": device_id,
              "state": "not_found", "startup_recorded": False,
              "checkpoint": inspect_checkpoint(path, pm_id, task_id, device_id)}
    receipt = None
    try:
        record = read_checkpoint(path, pm_id, task_id, device_id)
    except (ValueError, TypeError, AttributeError, sqlite3.Error, OSError):
        record = None  # Broken checkpoint data cannot supply a stop acknowledgment.
    if record:
        snapshot = record['snapshot']
        control = snapshot.get('control_receipt', {})
        if (snapshot.get('stopped') is True and isinstance(control, dict)
                and control.get('ok') is True and control.get('scope') == 'pm_cooperative'
                and control.get('pm_id') == pm_id and control.get('task_id') == task_id
                and control.get('device_id') == device_id and control.get('request_id')
                and control.get('action') in ('pause', 'cancel')
                and snapshot.get('stop_reason') == ('paused' if control['action'] == 'pause' else 'cancelled')):
            result['control_receipt'] = {key: control[key] for key in
                ('ok', 'scope', 'pm_id', 'task_id', 'device_id', 'request_id', 'action')}
    if path.is_file():
        with closing(sqlite3.connect(path.absolute().as_uri() + "?mode=ro", uri=True, timeout=5)) as conn:
            table = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='execution_receipts'").fetchone()
            row = conn.execute("SELECT identity,result FROM execution_receipts WHERE execution_id=?", (pm_id,)).fetchone() if table else None
        if row:
            identity = json.loads(row[0])
            if identity.get("pm_id") != pm_id or identity.get("parent_task_id") != task_id:
                result.pop("control_receipt", None)
                return {**result, "state": "mismatch"}
            result["startup_recorded"] = True
            receipt = json.loads(row[1]) if row[1] else {}
            result["state"] = "unknown"
    if current.get("pm_id") == pm_id:
        if current.get("task_id") != task_id or current.get("device_id") != device_id:
            result.pop("control_receipt", None)
            return {**result, "state": "mismatch"}
        if isinstance(current.get("running"), bool):
            result["state"] = "running" if current["running"] else "inactive"
            result["control_receipt"] = current.get("control_receipt", {})
            result["checkpoint_error"] = current.get("checkpoint_error") is True
            return result
    # A successful launch receipt is not evidence that the PM survived restart.
    output = (receipt or {}).get("output", {})
    if output.get("ok") is False and output.get("uncertain") is False:
        result["state"] = "start_failed"
    return result


def inspect_task_pm(controller, task_id: str) -> dict:
    """Use persisted ownership, including after Secretary restart, for one GET."""
    task = controller.db.get_task(task_id)
    if task is None:
        raise KeyError(task_id)
    pm = controller.db.get_pm_agent(task.pm_agent_id) if task.pm_agent_id else None
    result = {"task_id": task_id, "pm_id": task.pm_agent_id, "task_status": task.status,
              "checked_at": datetime.now(timezone.utc).isoformat(), "state": "unbound",
              "node": None, "startup_recorded": False}
    if pm and pm.task_id == task_id:
        result["node"] = {"device_id": pm.device_id, "name": pm.hostname, "ip": pm.ip, "port": pm.api_port}
        try:
            if pm.device_id == controller.state.device_id:
                evidence = controller._local_inspect_pm_start(pm.pm_id, task_id)
            else:
                response = http_get(f"http://{pm.ip}:{pm.api_port}/role/pm-starts/{quote(pm.pm_id, safe='')}",
                                    params={"task_id": task_id}, timeout=5, retries=0, allow_redirects=False)
                if response is None:
                    evidence = {"state": "unavailable"}
                elif response.status_code == 404:
                    evidence = {"state": "unsupported"}
                elif response.status_code != 200:
                    evidence = {"state": "unavailable"}
                else:
                    evidence = response.json()
            if not isinstance(evidence, dict):
                evidence = {"state": "unavailable"}
            if evidence.get("state") in ("unavailable", "unsupported"):
                result["state"] = evidence["state"]
            elif (evidence.get("format") != 1 or evidence.get("pm_id") != pm.pm_id
                  or evidence.get("task_id") != task_id or evidence.get("device_id") != pm.device_id):
                result["state"] = "mismatch"
            else:
                result["state"] = evidence.get("state") if evidence.get("state") in GUIDANCE else "unavailable"
                result["startup_recorded"] = evidence.get("startup_recorded") is True
                if isinstance(evidence.get("checkpoint"), dict):
                    allowed = ("state", "revision", "updated_at", "phase", "completed", "uncertain", "unstarted",
                               "failed", "total", "direct_state", "delivery_state", "stopped", "resume_supported")
                    result["checkpoint"] = {key: evidence["checkpoint"][key] for key in allowed
                                            if key in evidence["checkpoint"]}
                result["checkpoint_error"] = evidence.get("checkpoint_error") is True
        except Exception:
            # Do not expose credentials or remote error bodies in diagnostics.
            result["state"] = "unavailable"
    latest = controller.db.get_task(task_id)
    if not latest or latest.pm_agent_id != task.pm_agent_id or latest.status != task.status:
        result["state"] = "changed"
        result.pop("checkpoint", None)
        result.pop("checkpoint_error", None)
    result["message"], result["next_action"] = GUIDANCE[result["state"]]
    return result
