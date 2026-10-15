"""Shared task submission: persist PM ownership before any local or remote execution."""
import uuid

from .host_info import pick_reachable_ip
from .http_retry import http_get, http_post
from .logger import get_logger
from .protocol import PMAgent, Task

logger = get_logger("task_submission")


def _broadcast(controller, event: str, data: dict):
    try:
        controller._queue_ws_broadcast(event, data)
    except Exception as exc:
        logger.warning("任务通知失败: %s", exc)


def submit_task(controller, name: str, description: str, created_by: str = "secretary",
                priority: str = "normal", task_id: str = "", input_data: dict | None = None,
                project_id: str = "") -> dict:
    task = Task(task_id=task_id or f"task-{uuid.uuid4().hex[:12]}", name=name, description=description,
                created_by=created_by, project_id=project_id, status="pending",
                input_data={**(input_data or {}), "_priority": priority})
    if not controller.db.create_task_once(task):
        return controller.db.get_task(task.task_id).to_dict()
    _broadcast(controller, "task_submitted", task.to_dict())
    return dispatch_task(controller, task)


def dispatch_task(controller, task: Task, target=None) -> dict:
    """Bind once, start once, then return current persisted state, never an old snapshot."""
    db, state = controller.db, controller.state
    active = getattr(controller, "_local_pm_agent", None)
    local = (target is None and getattr(controller, "secretary_active", False)
             and getattr(controller, "chat_runtime", None) is not None and not (active and active.running))
    if not local and target is None:
        hosts = [h for h in db.list_hosts() if h.online and h.device_id != state.device_id]
        loads = {}
        for pm in db.list_pm_agents():
            if pm.status in ("starting", "planning", "executing", "monitoring", "awaiting_input", "unknown"):
                loads[pm.device_id] = loads.get(pm.device_id, 0) + 1
        tiers = {"S": 5, "A": 4, "B": 3, "C": 2, "D": 1}
        target = min(hosts, key=lambda h: (-tiers.get(h.rating_tier, 0), loads.get(h.device_id, 0)), default=None)
    if not local and target is None:
        # Do not overwrite a task already claimed by a concurrent queue dispatcher.
        conn = db._get_conn()
        with conn:
            conn.execute("UPDATE tasks SET status='failed',output_data=? WHERE task_id=? AND status='pending' AND pm_agent_id=''",
                         ('{"error":"无可用 work_station"}', task.task_id))
        return db.get_task(task.task_id).to_dict()
    pm_id = f"pm-{uuid.uuid4().hex[:12]}"
    ip = "127.0.0.1" if local else target.ip
    port = state.api_port if local else target.api_port
    device_id = state.device_id if local else target.device_id
    host_name = state.device_name if local else target.device_name or target.hostname
    pm = PMAgent(pm_id=pm_id, agent_name=f"PM-{pm_id[:8]}", task_id=task.task_id, project_id=task.project_id,
                 device_id=device_id, hostname=host_name, ip=ip, api_port=port, status="starting")
    if not db.bind_task_pm(pm):
        return db.get_task(task.task_id).to_dict()
    controller._pm_worker_map[pm_id] = {"ip": ip, "api_port": port, "device_id": device_id,
                                      "local": bool(local), "task_id": task.task_id}
    task_data = db.get_task(task.task_id).to_dict()
    _broadcast(controller, "pm_registered", pm.to_dict())
    try:
        controller._auto_attach_pm_thread(pm_id, task.name, pm.agent_name)
        controller.bot_gateway.notify("pm_registered", {"pm_id": pm_id[:12], "task": task.name, "station": host_name})
    except Exception as exc:
        logger.warning("PM 对话绑定或通知失败: %s", exc)
    result = None
    try:
        secretary_ip = "127.0.0.1" if local else pick_reachable_ip(ip)
        if not secretary_ip:
            result = {"ok": False, "uncertain": False, "message": "无法确定远端可达的 Secretary 地址"}
        else:
            url = f"http://{secretary_ip}:{state.api_port}"
            if local:
                result = controller._local_start_pm(task.task_id, url, task_data, pm_id=pm_id)
            else:
                response = http_post(f"http://{ip}:{port}/role/start-pm", json={
                    "task_id": task.task_id, "pm_id": pm_id, "secretary_url": url, "task_data": task_data},
                    timeout=15, retries=0)
                if response is not None:
                    body = response.json()
                    result = body if response.status_code == 200 else body.get("detail")
        if not isinstance(result, dict) or result.get("pm_id") not in (None, pm_id):
            raise RuntimeError("PM 启动回执缺失或编号不匹配")
        if result.get("ok") is True:
            if result.get("pm_id") != pm_id or result.get("task_id") != task.task_id:
                raise RuntimeError("PM 启动回执任务归属不匹配")
        else:
            db.note_pm_start_issue(pm_id, result.get("message", "启动结果待核实"),
                                   uncertain=result.get("uncertain", True))
    except Exception as exc:
        # Read-only confirmation can recover a lost response. Never resend start.
        confirmed = False
        if not local:
            try:
                response = http_get(f"http://{ip}:{port}/role/pm-status", timeout=5, retries=0)
                receipt = response.json() if response is not None and response.status_code == 200 else {}
                confirmed = receipt.get("pm_id") == pm_id and receipt.get("task_id") == task.task_id
            except Exception:
                pass
        if not confirmed:
            db.note_pm_start_issue(pm_id, f"启动结果待核实，不自动重派：{exc}")
    current = db.get_task(task.task_id).to_dict()
    _broadcast(controller, "task_updated", current)
    return current
