"""Transactional task resubmission with preserved inputs and durable source provenance."""
import copy
import json
import time
import uuid

from .protocol import Task
from .task_submission import _broadcast, dispatch_task


def task_with_provenance(db, task: Task) -> dict:
    """Read links from durable records, never from client-supplied task fields."""
    data = task.to_dict()
    rows = db._get_conn().execute(
        "SELECT source_task_id,replacement_task_id FROM task_resubmissions "
        "WHERE source_task_id=? OR replacement_task_id=?", (task.task_id, task.task_id)).fetchall()
    links = {}
    for row in rows:
        if row["source_task_id"] == task.task_id:
            links["replacement_task_id"] = row["replacement_task_id"]
        if row["replacement_task_id"] == task.task_id:
            links["source_task_id"] = row["source_task_id"]
    data["resubmission"] = links
    return data


def _validate_source(db, task: Task, acknowledged: bool) -> None:
    output = task.output_data or {}
    control = output.get("_pm_control") or {}
    confirmed = control.get("state") == "confirmed"
    if task.status not in ("failed", "cancelled", "interrupted") or "_delivery" in output:
        raise ValueError("任务已有交付或当前状态不允许重提，请先核对产物与验收结论")
    if control and not confirmed:
        raise ValueError("原节点尚未确认停止，请先核实或重发停止请求")
    unknown = any((output.get(key) or {}).get("state") == "unknown"
                  for key in ("_pm_recovery", "_pm_execution", "_pm_start"))
    if not confirmed and (unknown or task.status in ("cancelled", "interrupted")):
        raise ValueError("原执行或停止结果待核实，不能重新提交")
    if task.pm_agent_id and not confirmed:
        pm = db.get_pm_agent(task.pm_agent_id)
        if pm is None or pm.task_id != task.task_id or pm.status != "failed":
            raise ValueError("原 PM 尚无明确失败记录，请先核实原节点")
    if (task.pm_agent_id or control) and not acknowledged:
        raise ValueError("请先确认旧任务的在途工具已结束，并核对已有产物和费用")


def _check_budget(controller, task: Task) -> None:
    manager = getattr(controller, "project_manager", None)
    if task.project_id and manager and not manager.check_budget(task.project_id):
        raise PermissionError("项目预算已超支或已暂停，无法重新提交任务")


def prepare_resubmission(controller, source_id: str, acknowledged: bool = False) -> tuple[Task, bool]:
    """Atomically validate the source and insert its only direct replacement.

    A pending unbound replacement can be dispatched after a lost response or
    process restart. Deleted replacements remain tombstoned and are not recreated.
    """
    db = controller.db
    conn = db._get_conn()
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        record = conn.execute("SELECT * FROM task_resubmissions WHERE source_task_id=?", (source_id,)).fetchone()
        if record:
            replacement = db.get_task(record["replacement_task_id"])
            if replacement is None:
                raise ValueError(f"替代任务 {record['replacement_task_id']} 已删除，保留重提记录，不重复创建")
            if replacement.status == "pending" and not replacement.pm_agent_id:
                source = db.get_task(source_id)
                if source is None:
                    raise ValueError("原任务已删除，待派发的替代任务需人工核实")
                _validate_source(db, source, bool(record["acknowledged_side_effects"]))
                _check_budget(controller, replacement)
            return replacement, True
        source = db.get_task(source_id)
        if source is None:
            raise KeyError("原任务不存在")
        _validate_source(db, source, acknowledged)
        _check_budget(controller, source)
        replacement = Task(
            task_id=f"task-{uuid.uuid4().hex[:12]}", name=source.name, description=source.description,
            input_data=copy.deepcopy(source.input_data), project_id=source.project_id,
            created_by="user", status="pending")
        conn.execute("INSERT INTO task_resubmissions VALUES (?,?,?,?,?)", (
            source_id, replacement.task_id, time.time(),
            json.dumps(source.to_dict(), ensure_ascii=False), int(acknowledged)))
        db._save_task_fields(replacement)
    return replacement, False


def validate_resubmission_dispatch(controller, task: Task) -> None:
    """Recheck inside PM binding, including dispatch from the ordinary pending queue."""
    db = controller.db
    record = db._get_conn().execute(
        "SELECT * FROM task_resubmissions WHERE replacement_task_id=?", (task.task_id,)).fetchone()
    if record is None:
        return
    source = db.get_task(record["source_task_id"])
    if source is None:
        raise ValueError("原任务已删除，待派发的替代任务需人工核实")
    _validate_source(db, source, bool(record["acknowledged_side_effects"]))
    _check_budget(controller, task)


def resubmit_task(controller, source_id: str, acknowledged: bool = False) -> dict:
    task, duplicate = prepare_resubmission(controller, source_id, acknowledged)
    if not duplicate:
        _broadcast(controller, "task_submitted", task.to_dict())
    if task.status == "pending" and not task.pm_agent_id:
        # dispatch_task's transactional binding allows only one concurrent caller
        # to start a PM, including callers recovering an interrupted preparation.
        dispatch_task(controller, task)
    current = controller.db.get_task(task.task_id)
    if current is None:
        raise ValueError("替代任务已删除，保留重提记录，不重复创建")
    return {"task": task_with_provenance(controller.db, current), "duplicate": duplicate,
            "source_task_id": source_id}
