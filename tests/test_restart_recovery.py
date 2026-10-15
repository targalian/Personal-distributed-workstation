"""Preserve remote execution ownership and committed results across Secretary restart."""
import os
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from unittest.mock import Mock

import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lan_mesh.database import Database
from lan_mesh.pm_agent import ProjectManagerAgent
from lan_mesh.pm_control import request_task_control
from lan_mesh.protocol import PMAgent, Task
from lan_mesh.station_routes_pm import build_pm_routes
from lan_mesh.station_routes_tasks import build_task_routes
from tests.test_pm_start import controller as _controller_fixture

controller = _controller_fixture


def bound(controller, task_id="restart-task", status="running", device="remote"):
    task = Task(task_id=task_id, name="restart", input_data={"_brief": {"revision": 3}},
                subtasks=[{"name": "original", "status": "running"}])
    controller.db.save_task(task)
    controller.db.bind_task_pm(PMAgent(pm_id="pm-" + task_id, task_id=task_id, device_id=device, status="executing"))
    controller.db.update_task_status(task_id, status)
    return controller.db.get_task(task_id)


@pytest.mark.parametrize("status", ["running", "monitoring", "planning", "executing", "awaiting_input"])
@pytest.mark.parametrize("device", ["self", "remote"])
def test_reactivation_keeps_execution_and_binding_without_dispatch(controller, status, device):
    before = bound(controller, status=status, device=device)
    controller._local_start_pm = Mock(side_effect=AssertionError("must not restart"))
    assert controller._recover_stale_tasks() == 1
    after = controller.db.get_task(before.task_id)
    assert after.status == before.status and after.pm_agent_id == before.pm_agent_id
    assert after.input_data == before.input_data and after.subtasks == before.subtasks
    assert after.output_data["_pm_recovery"]["state"] == "unknown"
    assert controller.db.get_pm_agent(after.pm_agent_id).status == "unknown"
    controller._local_start_pm.assert_not_called()
    # Repeated activation keeps the first diagnostic and never claims a new PM.
    controller._recover_stale_tasks()
    assert controller.db.get_task(before.task_id).output_data == after.output_data


def test_recovery_has_no_one_hundred_task_cap(controller):
    for index in range(125):
        bound(controller, task_id=f"task-{index}")
    assert controller._recover_stale_tasks() == 125
    assert all(t.output_data["_pm_recovery"]["state"] == "unknown" for t in controller.db.list_tasks(limit=200))


@pytest.mark.parametrize("status", ["pending", "completed", "accepted", "rejected", "failed", "paused", "cancelled"])
def test_terminal_queued_and_stopped_records_are_unchanged(controller, status):
    task = Task(task_id="preserved", status=status, output_data={"evidence": "keep"})
    controller.db.save_task(task)
    before = controller.db.get_task(task.task_id).to_dict()
    assert controller._recover_stale_tasks() == 0
    assert controller.db.get_task(task.task_id).to_dict() == before


@pytest.mark.parametrize("review", [None, True, False])
def test_received_delivery_and_review_are_not_reclassified(controller, review):
    task = bound(controller)
    controller.db.record_task_delivery(task.task_id, task.pm_agent_id, {"deliverable": "actual artifact", "summary": "saved"})
    if review is not None:
        controller.db.review_task_delivery(task.task_id, review, "review note")
    before = controller.db.get_task(task.task_id).to_dict()
    pm_before = controller.db.get_pm_agent(task.pm_agent_id).to_dict()
    assert controller._recover_stale_tasks() == 0
    assert controller.db.get_task(task.task_id).to_dict() == before
    assert controller.db.get_pm_agent(task.pm_agent_id).to_dict() == pm_before


def test_legacy_interrupted_binding_can_still_receive_original_delivery(controller):
    task = bound(controller, status="interrupted")
    task.output_data = {"_interrupted": {"reason": "old assumption", "original_status": "running"}}
    controller.db.save_task(task)
    controller._recover_stale_tasks()
    current = controller.db.get_task(task.task_id)
    assert current.status == "running" and current.pm_agent_id == task.pm_agent_id
    assert current.output_data["_pm_recovery"]["legacy_interruption"] == task.output_data["_interrupted"]
    controller.db.record_task_delivery(task.task_id, task.pm_agent_id, {"deliverable": "original PM finished"})
    assert "_pm_recovery" not in controller.db.get_task(task.task_id).output_data


def test_stale_save_cannot_drop_recovery_or_change_ownership(controller):
    stale = bound(controller)
    controller._recover_stale_tasks()
    stale.status, stale.pm_agent_id = "interrupted", "different-pm"
    controller.db.save_task(stale)
    current = controller.db.get_task(stale.task_id)
    assert current.status == "running" and current.pm_agent_id == "pm-restart-task"
    assert current.output_data["_pm_recovery"]["state"] == "unknown"


def test_original_runtime_callback_clears_recovery_and_unrelated_callback_does_not(controller):
    task = bound(controller)
    other = bound(controller, task_id="other")
    controller._recover_stale_tasks()
    controller.db.update_pm_status(other.pm_agent_id, "executing")
    assert "_pm_recovery" in controller.db.get_task(task.task_id).output_data
    app = FastAPI()
    app.include_router(build_pm_routes(controller))
    response = TestClient(app).post(f"/api/pm/{task.pm_agent_id}/status", json={"status": "monitoring"})
    assert response.status_code == 200
    assert "_pm_recovery" not in controller.db.get_task(task.task_id).output_data


@pytest.mark.parametrize("status", ["running", "monitoring", "planning", "executing", "awaiting_input"])
def test_original_completion_finishes_all_recovered_active_states(controller, status):
    task = bound(controller, status=status)
    controller._recover_stale_tasks()
    app = FastAPI()
    app.include_router(build_task_routes(controller))
    app.include_router(build_pm_routes(controller))
    client = TestClient(app)
    assert client.post(f"/api/pm/{task.pm_agent_id}/deliver", json={
        "task_id": task.task_id, "deliverable": "original result"}).status_code == 200
    assert client.post(f"/api/pm/{task.pm_agent_id}/progress", json={
        "reporter_type": "pm", "status": "completed"}).status_code == 200
    current = controller.db.get_task(task.task_id)
    assert current.status == "completed" and "_pm_recovery" not in current.output_data


def test_recovery_is_atomic_on_write_failure(controller):
    bound(controller, task_id="good")
    bound(controller, task_id="bad")
    conn = controller.db._get_conn()
    conn.execute("""CREATE TRIGGER fail_recovery BEFORE UPDATE ON tasks WHEN NEW.task_id='bad'
        BEGIN SELECT RAISE(ABORT, 'disk failure simulation'); END""")
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        controller._recover_stale_tasks()
    assert all("_pm_recovery" not in task.output_data for task in controller.db.list_tasks())
    assert controller.db.get_pm_agent("pm-good").status == "executing"


def test_unbound_active_task_does_not_gain_false_stop_confirmation(controller):
    controller.db.save_task(Task(task_id="orphan", status="running"))
    controller._recover_stale_tasks()
    answer = request_task_control(controller, "orphan", "cancel")
    assert answer["ok"] and not answer["confirmed"]
    with pytest.raises(ValueError):
        controller.db.delete_task("orphan", require_stopped=True)


def test_unknown_stop_intent_survives_recovery_and_late_runtime_report(controller):
    task = bound(controller)
    controller.db.begin_task_control(task.task_id, "cancel")
    before = controller.db.get_task(task.task_id).to_dict()
    assert controller._recover_stale_tasks() == 0
    controller.db.update_pm_status(task.pm_agent_id, "executing")
    assert controller.db.get_task(task.task_id).to_dict() == before
    assert controller.db.get_pm_agent(task.pm_agent_id).status == "unknown"


@contextmanager
def secretary_server(controller, port=0):
    app = FastAPI()
    app.include_router(build_task_routes(controller))
    app.include_router(build_pm_routes(controller))
    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", port))
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 3
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(.01)
        assert server.started
        yield sock.getsockname()[1]
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        sock.close()
        assert not thread.is_alive()


def test_original_pm_delivers_after_secretary_http_restart_and_separate_recovery_process(controller):
    task = bound(controller)
    entered, release = threading.Event(), threading.Event()
    calls = []
    with secretary_server(controller) as port:
        pm = ProjectManagerAgent(task.pm_agent_id, Mock(), f"http://127.0.0.1:{port}", "remote")
        pm._planner.load_skill = lambda: None
        pm._planner.refine_requirements = lambda task: task
        pm._planner.analyze_with_skill = lambda task: {"pattern": "single"}
        def execute(task):
            calls.append(task["task_id"])
            entered.set()
            assert release.wait(15)
            return {"summary": "original remote execution completed"}
        pm._planner.execute_directly = execute
        pm._record_task_memory = Mock()
        pm._distribute_artifacts = Mock()
        pm._monitor.poll_interval = .05
        pm.start_task(task.to_dict())
        assert entered.wait(3)
    try:
        # The original PM remains alive while a separate process reopens the DB.
        code = "from lan_mesh.database import Database; import sys; print(Database(sys.argv[1]).mark_tasks_for_recovery())"
        env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME")}
        result = subprocess.run([sys.executable, "-c", code, str(controller.db.path)], env=env,
                                capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, result.stderr
        controller.db = Database(str(controller.db.path))
        assert controller.db.get_task(task.task_id).output_data["_pm_recovery"]["state"] == "unknown"
        assert pm.running and not controller._pm_worker_map
        with secretary_server(controller, port):
            release.set()
            pm._thread.join(timeout=10)
            pm._progress_thread.join(timeout=5)
            current = controller.db.get_task(task.task_id)
            assert current.status == "completed"
            assert current.output_data["_delivery"]["deliverable"] == "original remote execution completed"
            assert "_pm_recovery" not in current.output_data
            assert calls == [task.task_id] and current.pm_agent_id == task.pm_agent_id
    finally:
        release.set()
        pm.stop()
        pm._thread.join(timeout=5)
        pm._progress_thread.join(timeout=5)
        assert not pm._thread.is_alive() and not pm._progress_thread.is_alive()
