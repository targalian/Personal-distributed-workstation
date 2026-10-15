"""Verify stop intent, original-PM identity and acknowledgment under races and transport loss."""
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lan_mesh.database import Database
from lan_mesh.pm_agent import ProjectManagerAgent
from lan_mesh.pm_control import build_pm_control_routes, control_pm, request_task_control
from lan_mesh.protocol import PMAgent, Task
from lan_mesh.station_routes_tasks import build_task_routes
from tests.test_pm_lifecycle import pm as _pm_fixture
from tests.test_pm_start import controller as _controller_fixture
from tests.test_pm_start import live_server

controller = _controller_fixture
pm = _pm_fixture


def bind(controller, device="self", port=12345):
    controller.db.save_task(Task(task_id="task-life", name="control task"))
    assert controller.db.bind_task_pm(PMAgent(pm_id="pm-life", task_id="task-life", device_id=device,
                                            ip="127.0.0.1", api_port=port, status="starting"))
    return controller.db.get_task("task-life")


def request(action="cancel", **changes):
    return {"pm_id": "pm-life", "task_id": "task-life", "device_id": "self", "action": action,
            "request_id": "request-one", **changes}


def inactive_pm():
    pm = ProjectManagerAgent("pm-life", Mock(), "http://unused", "self")
    pm._state.task = {"task_id": "task-life"}
    pm.running = True
    return pm


@pytest.mark.parametrize("field", ["pm_id", "task_id", "device_id", "request_id", "action"])
def test_wrong_or_missing_identity_never_stops_current_pm(field):
    pm = inactive_pm()
    for value in ("", "wrong"):
        if field == "request_id" and value:
            continue
        answer = control_pm(lambda: pm, "self", request(**{field: value}))
        assert not answer["ok"] and pm.running and not pm._state.stop_event.is_set()


def test_replay_and_late_pause_cannot_downgrade_cancel():
    pm = inactive_pm()
    pause = request("pause")
    assert control_pm(lambda: pm, "self", pause)["ok"]
    cancel = request(request_id="second")
    assert control_pm(lambda: pm, "self", cancel)["ok"]
    assert control_pm(lambda: pm, "self", cancel) == {"ok": True, **cancel, "scope": "pm_cooperative"}
    assert not control_pm(lambda: pm, "self", pause)["ok"]
    assert pm._state.stop_reason == "cancelled" and not pm.running
    assert pm.get_status()["control_receipt"]["request_id"] == "second"


@pytest.mark.parametrize("path", ["/role/pm-control", "/role/cancel-pm", "/role/pause-pm", "/role/stop-pm"])
def test_old_paths_require_identity_and_cannot_stop_another_pm(path):
    pm = inactive_pm()
    app = FastAPI()
    app.include_router(build_pm_control_routes(lambda payload: control_pm(lambda: pm, "self", payload)))
    client = TestClient(app)
    assert client.post(path).status_code == 422
    assert client.post(path, json=request(pm_id="old-pm")).status_code == 409
    assert pm.running
    response = client.post(path, json=request())
    assert response.status_code == 200 and response.json()["task_id"] == "task-life"
    assert not pm.running


@pytest.mark.parametrize("action", ["pause", "cancel"])
def test_local_confirmation_is_persistent_and_fences_late_results(controller, action):
    original = bind(controller)
    pm = inactive_pm()
    controller._local_pm_agent = pm
    assert not controller._pm_worker_map
    answer = request_task_control(controller, original.task_id, action)
    assert answer["confirmed"] and answer["control"]["scope"] == "pm_cooperative"
    assert not pm.running and pm._state.stop_event.is_set()
    # An old whole-task write, a status callback or late delivery cannot revive it.
    controller.db.save_task(original)
    controller.db.update_pm_status("pm-life", "executing")
    with pytest.raises(ValueError):
        controller.db.record_task_delivery("task-life", "pm-life", {"deliverable": "late"})
    current = controller.db.get_task("task-life")
    assert current.status == ("paused" if action == "pause" else "cancelled")
    assert current.output_data["_pm_control"] == answer["control"]
    assert controller.db.get_pm_agent("pm-life").status == current.status
    controller.db = Database(str(controller.db.path))
    controller._local_control_pm = Mock(side_effect=AssertionError("confirmed request must not resend"))
    assert request_task_control(controller, "task-life", action)["confirmed"]
    controller._local_control_pm.assert_not_called()


@pytest.mark.parametrize("status", ["completed", "accepted", "rejected", "failed"])
def test_terminal_task_and_review_cannot_be_cancelled(controller, status):
    bind(controller)
    task = controller.db.get_task("task-life")
    task.status = status
    controller.db.save_task(task)
    before = controller.db.get_task(task.task_id).to_dict()
    controller._local_control_pm = Mock()
    assert not request_task_control(controller, task.task_id, "cancel")["ok"]
    assert controller.db.get_task(task.task_id).to_dict() == before
    controller._local_control_pm.assert_not_called()


def test_delivery_wins_before_stop_intent_even_before_completion_status(controller):
    bind(controller)
    controller.db.record_task_delivery("task-life", "pm-life", {"deliverable": "already delivered"})
    assert not request_task_control(controller, "task-life", "pause")["ok"]
    assert controller.db.get_task("task-life").status == "running"


def test_unassigned_task_stop_is_confirmed_without_contact(controller, monkeypatch):
    controller.db.save_task(Task(task_id="queued"))
    post = Mock()
    monkeypatch.setattr("lan_mesh.pm_control.http_post", post)
    answer = request_task_control(controller, "queued", "cancel")
    assert answer["confirmed"] and answer["control"]["scope"] == "not_dispatched"
    assert not controller.db.bind_task_pm(PMAgent(pm_id="too-late", task_id="queued"))
    post.assert_not_called()


def test_notification_failure_does_not_lose_stop_acknowledgment(controller):
    bind(controller)
    controller._local_pm_agent = inactive_pm()
    controller.bot_gateway.notify.side_effect = RuntimeError("notification failed")
    controller._queue_ws_broadcast.side_effect = RuntimeError("websocket failed")
    answer = request_task_control(controller, "task-life", "cancel")
    assert answer["confirmed"]
    assert controller.db.get_task("task-life").output_data["_pm_control"]["state"] == "confirmed"


def test_stop_intent_write_failure_never_signals_pm(controller, monkeypatch):
    bind(controller)
    controller._local_pm_agent = inactive_pm()
    def fail(*args):
        raise OSError("disk full")
    monkeypatch.setattr(controller.db, "begin_task_control", fail)
    with pytest.raises(OSError, match="disk full"):
        request_task_control(controller, "task-life", "cancel")
    assert controller._local_pm_agent.running


@pytest.mark.parametrize("case", ["offline", "false-ok", "wrong-pm", "wrong-task", "wrong-device", "wrong-request", "wrong-action", "old-node"])
def test_unconfirmed_control_is_visible_and_retry_uses_same_request(controller, monkeypatch, case):
    bind(controller, device="remote")
    sent = []
    def post(url, **kwargs):
        assert url.endswith("/role/pm-control")
        payload = kwargs["json"]
        assert controller.db.get_task("task-life").status == "cancelled"
        assert kwargs["retries"] == 0 and kwargs["allow_redirects"] is False
        sent.append(payload)
        if case == "offline":
            return None
        if case == "old-node":
            return Mock(status_code=404)
        body = {"ok": True, **payload, "scope": "pm_cooperative"}
        field = {"false-ok": "ok", "wrong-pm": "pm_id", "wrong-task": "task_id", "wrong-device": "device_id",
                 "wrong-request": "request_id", "wrong-action": "action"}[case]
        body[field] = False if field == "ok" else "wrong"
        return Mock(status_code=200, json=lambda: body)
    monkeypatch.setattr("lan_mesh.pm_control.http_post", post)
    monkeypatch.setattr("lan_mesh.pm_control.http_get", lambda *args, **kwargs: None)
    first = request_task_control(controller, "task-life", "cancel")
    assert first["ok"] and not first["confirmed"] and first["control"]["state"] == "unknown"
    assert controller.db.get_pm_agent("pm-life").status == "unknown"
    assert len(sent) == 1
    second = request_task_control(controller, "task-life", "cancel")
    assert len(sent) == 2 and sent[0] == sent[1]
    assert second["control"]["request_id"] == first["control"]["request_id"]


def test_missing_memory_does_not_cancel_replacement_local_pm(controller):
    bind(controller)
    replacement = inactive_pm()
    replacement.pm_id = "other-pm"
    replacement._state.task = {"task_id": "other-task"}
    controller._local_pm_agent = replacement
    result = request_task_control(controller, "task-life", "cancel")
    assert result["ok"] and not result["confirmed"] and replacement.running


def test_late_pause_ack_cannot_replace_cancel_and_confirmed_cannot_regress(controller):
    bind(controller)
    pause = controller.db.begin_task_control("task-life", "pause").output_data["_pm_control"]
    cancel = controller.db.begin_task_control("task-life", "cancel").output_data["_pm_control"]
    controller.db.finish_task_control("task-life", pause["request_id"], True, "late pause")
    assert controller.db.get_task("task-life").output_data["_pm_control"] == cancel
    controller.db.finish_task_control("task-life", cancel["request_id"], True, "confirmed")
    controller.db.finish_task_control("task-life", cancel["request_id"], False, "late failure")
    assert controller.db.get_task("task-life").output_data["_pm_control"]["state"] == "confirmed"


def test_delivery_and_stop_intent_race_has_only_one_winner(controller):
    bind(controller)
    barrier = threading.Barrier(2)
    def deliver():
        barrier.wait()
        try:
            controller.db.record_task_delivery("task-life", "pm-life", {"deliverable": "race"})
            return "delivered"
        except ValueError:
            return "rejected"
    def stop():
        barrier.wait()
        try:
            controller.db.begin_task_control("task-life", "cancel")
            return "stopped"
        except ValueError:
            return "rejected"
    with ThreadPoolExecutor(max_workers=2) as pool:
        left, right = pool.submit(deliver), pool.submit(stop)
        results = [left.result(), right.result()]
    assert results.count("rejected") == 1


def test_task_api_returns_202_and_keeps_unconfirmed_task_on_delete(controller):
    bind(controller)
    controller._local_pm_agent = None
    app = FastAPI()
    app.include_router(build_task_routes(controller))
    client = TestClient(app)
    response = client.post("/api/tasks/task-life/cancel")
    assert response.status_code == 202 and response.json()["confirmed"] is False
    assert client.delete("/api/tasks/task-life").status_code == 409
    assert controller.db.get_task("task-life") is not None
    with pytest.raises(ValueError):
        controller.db.delete_task("task-life", require_stopped=True)
    controller._local_pm_agent = inactive_pm()
    assert client.post("/api/tasks/task-life/cancel").status_code == 200
    assert client.delete("/api/tasks/task-life").status_code == 200


@pytest.mark.parametrize("mode,action,lost", [("station", "cancel", False), ("station", "pause", True),
                                             ("worker", "cancel", True), ("worker", "pause", False)])
def test_real_http_control_during_blocked_runtime_and_lost_ack(controller, pm, tmp_path, monkeypatch, mode, action, lost):
    from lan_mesh.station_controller import StationController
    from lan_mesh.station_routes_worker import build_worker_routes
    from lan_mesh.worker import WorkerAgent
    from lan_mesh.worker_routes_pm import build_pm_routes
    pm.device_id = "remote"
    entered, release = threading.Event(), threading.Event()
    def execute(payload):
        entered.set()
        assert release.wait(5)
        return {"status": "completed", "output": {"summary": "late output"}}
    pm.runtime.execute.side_effect = execute
    app = FastAPI()
    if mode == "station":
        remote = StationController.__new__(StationController)
        remote.state = SimpleNamespace(device_id="remote")
        remote.db = Database(str(tmp_path / "remote.sqlite3"))
        remote.discovery = Mock()
        remote._local_pm_agent = pm
        app.include_router(build_worker_routes(remote))
    else:
        remote = WorkerAgent.__new__(WorkerAgent)
        remote.state = SimpleNamespace(device_id="remote", pm_agent=pm)
        remote.data_dir = tmp_path / "worker"
        app.include_router(build_pm_routes(remote))
    with live_server(app) as port:
        task = bind(controller, device="remote", port=port)
        pm.start_task(task.to_dict())
        try:
            assert entered.wait(3)
            from lan_mesh.pm_control import http_post as real_post
            posts = []
            def post(url, **kwargs):
                posts.append(kwargs)
                response = real_post(url, **kwargs)
                assert response.status_code == 200
                return None if lost else response
            monkeypatch.setattr("lan_mesh.pm_control.http_post", post)
            answer = request_task_control(controller, "task-life", action)
            assert answer["confirmed"] and len(posts) == 1
            assert pm._state.stop_event.is_set() and not pm.running
            assert pm._thread.is_alive()  # Confirmation is cooperative, not a killed external call.
        finally:
            release.set()
            pm._thread.join(timeout=3)
            pm._progress_thread.join(timeout=3)
        assert pm.runtime.execute.call_count == 1
        assert not any(url.endswith('/deliver') for url, _ in pm.test_calls)
