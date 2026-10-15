"""Verify task/PM registration precedes execution and startup retries never create another PM."""
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests
import uvicorn
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lan_mesh.database import Database
from lan_mesh.pm_start import start_pm_once
from lan_mesh.protocol import PMAgent, Task
from lan_mesh.station_controller import StationController
from lan_mesh.station_routes_pm import build_pm_routes
from lan_mesh.station_routes_tasks import build_task_routes


@pytest.fixture
def controller(tmp_path, monkeypatch):
    monkeypatch.setattr(Database, "backup", lambda self: "")
    ctl = StationController.__new__(StationController)
    ctl.db = Database(str(tmp_path / "tasks.sqlite3"))
    ctl.state = SimpleNamespace(device_id="self", device_name="local", api_port=12345, ws_clients=set())
    ctl.secretary_active = True
    ctl.chat_runtime = Mock()
    ctl.chat_handler = None
    ctl.project_manager = None
    ctl.discovery = Mock()
    ctl.bot_gateway = Mock()
    ctl._local_pm_agent = None
    ctl._pm_worker_map = {}
    ctl._queue_ws_broadcast = Mock()
    return ctl


def client_for(controller):
    app = FastAPI()
    app.include_router(build_task_routes(controller))
    app.include_router(build_pm_routes(controller))
    return TestClient(app)


def instant_factory(controller, client, outcome, created):
    class InstantPM:
        def __init__(self, pm_id, *args):
            self.pm_id = pm_id
            self.running = False
            created.append(self)
        def start_task(self, task):
            self.task = task
            assert controller.db.get_task(task["task_id"]).pm_agent_id == self.pm_id
            assert controller.db.get_pm_agent(self.pm_id).task_id == task["task_id"]
            assert self.pm_id in controller._pm_worker_map
            assert client.post(f"/api/pm/{self.pm_id}/status", json={"status": "planning"}).status_code == 200
            if outcome != "failed":
                receipt = client.post(f"/api/pm/{self.pm_id}/deliver", json={
                    "task_id": task["task_id"], "deliverable": "真实接口保存的产物", "summary": "产物"})
                assert receipt.status_code == 200
            assert client.post(f"/api/pm/{self.pm_id}/status", json={"status": "failed" if outcome == "failed" else "completed"}).status_code == 200
            assert client.post(f"/api/pm/{self.pm_id}/progress", json={
                "status": "failed" if outcome == "failed" else "completed", "reporter_type": "pm"}).status_code == 200
            if outcome == "accepted":
                assert client.post(f"/api/tasks/{task['task_id']}/accept").status_code == 200
    return InstantPM


@pytest.mark.parametrize("outcome", ["completed", "failed", "accepted"])
@pytest.mark.parametrize("via_api", [False, True])
def test_fast_terminal_result_survives_startup_response(controller, monkeypatch, outcome, via_api):
    client = client_for(controller)
    created = []
    monkeypatch.setattr("lan_mesh.pm_start.ProjectManagerAgent", instant_factory(controller, client, outcome, created))
    if via_api:
        result = client.post("/api/tasks", json={"name": "快速任务", "description": "验证绑定", "project_id": "project-1"}).json()
        assert result["project_id"] == "project-1"
    else:
        result = controller.submit_task_from_chat("快速任务", "验证绑定")
    assert len(created) == 1 and result["status"] == outcome
    assert controller.db.get_pm_agent(result["pm_agent_id"]).status == ("completed" if outcome == "accepted" else outcome)
    assert controller.db.get_task(result["task_id"]).status == outcome


def test_concurrent_same_task_submission_creates_only_one_pm(controller, monkeypatch):
    client = client_for(controller)
    created = []
    monkeypatch.setattr("lan_mesh.pm_start.ProjectManagerAgent", instant_factory(controller, client, "completed", created))
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: controller.submit_task_from_chat("same", "same", task_id="same-id"), range(8)))
    assert len(created) == 1
    assert controller.db.get_task("same-id").status == "completed"
    assert len(controller.db.list_pm_agents()) == 1


def test_registration_transaction_failure_does_not_start_pm(controller, monkeypatch):
    original = controller.db._save_pm_agent_fields
    def fail(pm):
        original(pm)
        raise OSError("write failed")
    monkeypatch.setattr(controller.db, "_save_pm_agent_fields", fail)
    factory = Mock()
    monkeypatch.setattr("lan_mesh.pm_start.ProjectManagerAgent", factory)
    with pytest.raises(OSError):
        controller.submit_task_from_chat("write", "write", task_id="write-id")
    factory.assert_not_called()
    assert controller.db.get_task("write-id").pm_agent_id == ""
    assert not controller.db.list_pm_agents()


def test_concurrent_pending_dispatch_claims_one_owner(controller, monkeypatch):
    from lan_mesh.task_submission import dispatch_task
    client = client_for(controller)
    created = []
    monkeypatch.setattr("lan_mesh.pm_start.ProjectManagerAgent", instant_factory(controller, client, "completed", created))
    task = Task(task_id="queue-id", name="queued")
    controller.db.save_task(task)
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: dispatch_task(controller, task), range(8)))
    assert len(created) == 1
    assert controller.db.get_task("queue-id").status == "completed"


def test_notification_failure_does_not_leave_registered_pm_unstarted(controller, monkeypatch):
    client = client_for(controller)
    created = []
    monkeypatch.setattr("lan_mesh.pm_start.ProjectManagerAgent", instant_factory(controller, client, "completed", created))
    controller._queue_ws_broadcast.side_effect = RuntimeError("notification unavailable")
    controller.bot_gateway.notify.side_effect = RuntimeError("notification unavailable")
    assert controller.submit_task_from_chat("notify", "notify")["status"] == "completed"
    assert len(created) == 1


def gate_args(tmp_path):
    data = {"task_id": "t", "pm_agent_id": "pm-t", "status": "running", "name": "task"}
    holder = SimpleNamespace(pm=None)
    return dict(pm_id="pm-t", task_id="t", secretary_url="http://secretary", task_data=data,
                runtime=Mock(), device_id="worker", device_name="worker", receipt_path=tmp_path / "starts.sqlite3",
                current=lambda: holder.pm, assign=lambda pm: setattr(holder, "pm", pm), lookup=lambda _: data)


def test_concurrent_start_replay_and_changed_payload(controller, tmp_path, monkeypatch):
    args = gate_args(tmp_path)
    factory = Mock(return_value=SimpleNamespace(running=False, start_task=Mock()))
    monkeypatch.setattr("lan_mesh.pm_start.ProjectManagerAgent", factory)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: start_pm_once(**args), range(8)))
    assert all(result["ok"] for result in results)
    assert sum(result.get("duplicate", False) for result in results) == 7
    assert factory.call_count == 1
    conflict = start_pm_once(**{**args, "task_data": {**args["task_data"], "name": "changed"}})
    assert not conflict["ok"] and conflict["uncertain"]
    assert factory.call_count == 1


@pytest.mark.parametrize("status", ["cancelled", "paused", "completed"])
def test_stopped_task_cannot_start_from_old_snapshot(tmp_path, monkeypatch, status):
    args = gate_args(tmp_path)
    args["lookup"] = lambda _: {**args["task_data"], "status": status}
    factory = Mock()
    monkeypatch.setattr("lan_mesh.pm_start.ProjectManagerAgent", factory)
    assert not start_pm_once(**args)["ok"]
    factory.assert_not_called()


def test_restart_and_partial_start_error_never_relaunch(tmp_path, monkeypatch):
    args = gate_args(tmp_path)
    factory = Mock(return_value=SimpleNamespace(running=False, start_task=Mock(side_effect=RuntimeError("thread started then failed"))))
    monkeypatch.setattr("lan_mesh.pm_start.ProjectManagerAgent", factory)
    assert start_pm_once(**args)["uncertain"]
    assert start_pm_once(**args)["duplicate"]
    monkeypatch.setattr("lan_mesh.pm_start.PROCESS_OWNER", "new-process")
    result = start_pm_once(**args)
    assert not result["ok"] and result["uncertain"]
    assert factory.call_count == 1


def test_replayed_start_after_another_pm_does_not_replace_active_pm(tmp_path, monkeypatch):
    args = gate_args(tmp_path)
    factory = Mock(return_value=SimpleNamespace(running=False, start_task=Mock()))
    monkeypatch.setattr("lan_mesh.pm_start.ProjectManagerAgent", factory)
    assert start_pm_once(**args)["ok"]
    active = SimpleNamespace(running=True)
    args["assign"](active)
    assert start_pm_once(**args)["duplicate"]
    assert args["current"]() is active and factory.call_count == 1


def test_preassigned_id_must_match_authoritative_owner(tmp_path, monkeypatch):
    args = gate_args(tmp_path)
    args["lookup"] = lambda _: {**args["task_data"], "pm_agent_id": "different-pm"}
    factory = Mock()
    monkeypatch.setattr("lan_mesh.pm_start.ProjectManagerAgent", factory)
    assert not start_pm_once(**args)["ok"]
    factory.assert_not_called()


@pytest.mark.parametrize("confirmed", [False, True])
def test_remote_start_response_loss_never_resends(controller, monkeypatch, confirmed):
    controller.chat_runtime = None
    host = SimpleNamespace(online=True, device_id="remote", ip="127.0.0.1", api_port=9,
                           rating_tier="S", device_name="remote", hostname="remote")
    monkeypatch.setattr(controller.db, "list_hosts", lambda: [host])
    monkeypatch.setattr("lan_mesh.task_submission.pick_reachable_ip", lambda _: "127.0.0.1")
    captured = []
    def post(url, **kwargs):
        captured.append(kwargs)
        data = kwargs["json"]
        assert controller.db.get_task(data["task_id"]).pm_agent_id == data["pm_id"]
        assert controller.db.get_pm_agent(data["pm_id"]) is not None
        raise requests.ConnectionError("response lost")
    monkeypatch.setattr("lan_mesh.task_submission.http_post", post)
    def get(*args, **kwargs):
        data = captured[0]["json"]
        return Mock(status_code=200, json=lambda: {"pm_id": data["pm_id"] if confirmed else "other", "task_id": data["task_id"]})
    monkeypatch.setattr("lan_mesh.task_submission.http_get", get)
    result = controller.submit_task_from_chat("remote", "remote")
    assert len(captured) == 1 and captured[0]["retries"] == 0
    assert result["status"] == "running"
    assert ("_pm_start" in result["output_data"]) is not confirmed
    if not confirmed:
        controller.db.update_pm_status(result["pm_agent_id"], "planning")
        assert "_pm_start" not in controller.db.get_task(result["task_id"]).output_data


def test_host_offline_keeps_ownership_and_does_not_migrate(controller):
    task = Task(task_id="lost", name="lost", status="pending")
    controller.db.save_task(task)
    assert controller.db.bind_task_pm(PMAgent(pm_id="pm-lost", task_id="lost", status="starting"))
    controller.db.update_pm_status("pm-lost", "executing")
    controller._pm_worker_map["pm-lost"] = {"device_id": "gone", "task_id": "lost"}
    controller._migrate_orphaned_pms(["gone"])
    current = controller.db.get_task("lost")
    assert current.pm_agent_id == "pm-lost" and current.status == "running"
    assert "不自动迁移" in current.output_data["_pm_execution"]["message"]
    assert controller.db.get_pm_agent("pm-lost").status == "unknown"
    assert controller._local_pm_agent is None


def test_unbound_task_rejects_delivery(controller):
    controller.db.save_task(Task(task_id="unbound", name="unbound"))
    with pytest.raises(ValueError, match="PM"):
        controller.db.record_task_delivery("unbound", "invented-pm", {"deliverable": "unowned"})


@contextmanager
def live_server(app):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 3
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started
        yield sock.getsockname()[1]
    finally:
        server.should_exit = True
        thread.join(timeout=3)
        sock.close()
        assert not thread.is_alive()


@pytest.mark.parametrize("mode,lost_response", [("local", False), ("station", False), ("station", True), ("worker", False), ("worker", True)])
def test_real_http_submission_and_pm_threads_complete_without_registration_race(controller, monkeypatch, tmp_path, mode, lost_response):
    from lan_mesh.pm_agent import ProjectManagerAgent
    from lan_mesh.pm_planner import PMPlanner
    monkeypatch.setattr(PMPlanner, "load_skill", lambda self: None)
    monkeypatch.setattr(PMPlanner, "refine_requirements", lambda self, task: task)
    monkeypatch.setattr(PMPlanner, "analyze_with_skill", lambda self, task: {"pattern": "single"})
    monkeypatch.setattr(PMPlanner, "execute_directly", lambda self, task: {"summary": "完整交付产物"})
    monkeypatch.setattr(ProjectManagerAgent, "_distribute_artifacts", lambda *args: None)
    monkeypatch.setattr(ProjectManagerAgent, "_record_task_memory", lambda *args: None)
    real_pm = ProjectManagerAgent
    def create_pm(*args, **kwargs):
        pm = real_pm(*args, **kwargs)
        pm._monitor.poll_interval = 0.01
        return pm
    monkeypatch.setattr("lan_mesh.pm_start.ProjectManagerAgent", create_pm)
    app = FastAPI()
    app.include_router(build_task_routes(controller))
    app.include_router(build_pm_routes(controller))
    with live_server(app) as port:
        controller.state.api_port = port
        remote_app = FastAPI()
        remote = None
        if mode == "worker":
            from lan_mesh.worker import WorkerAgent
            from lan_mesh.worker_routes_pm import build_pm_routes as build_worker_pm_routes
            remote = WorkerAgent.__new__(WorkerAgent)
            remote.state = SimpleNamespace(device_id="remote", device_name="remote", pm_agent=None, agent_runtime=Mock())
            remote.data_dir = tmp_path / "remote"
            remote_app.include_router(build_worker_pm_routes(remote))
        elif mode == "station":
            from lan_mesh.station_routes_worker import build_worker_routes
            remote = StationController.__new__(StationController)
            remote.state = SimpleNamespace(device_id="remote", device_name="remote", api_port=1, ws_clients=set())
            remote.db = Database(str(tmp_path / "remote.sqlite3"))
            remote.discovery = Mock()
            remote.chat_runtime = Mock()
            remote._local_pm_agent = None
            remote_app.include_router(build_worker_routes(remote))
            monkeypatch.setattr("lan_mesh.model_resources.set_report_target_global", lambda *args, **kwargs: None)
        with live_server(remote_app) as remote_port:
            if remote:
                controller.chat_runtime = None
                host = SimpleNamespace(online=True, device_id="remote", ip="127.0.0.1", api_port=remote_port,
                                       rating_tier="S", device_name="remote", hostname="remote")
                monkeypatch.setattr(controller.db, "list_hosts", lambda: [host])
                monkeypatch.setattr("lan_mesh.task_submission.pick_reachable_ip", lambda _: "127.0.0.1")
            sent = []
            if lost_response:
                from lan_mesh.task_submission import http_post as real_post
                def lose_response(url, **kwargs):
                    sent.append(kwargs)
                    response = real_post(url, **kwargs)
                    assert response.status_code == 200
                    raise requests.ConnectionError("response lost after PM started")
                monkeypatch.setattr("lan_mesh.task_submission.http_post", lose_response)
            response = requests.post(f"http://127.0.0.1:{port}/api/tasks", json={"name": "threaded", "description": "real HTTP"}, timeout=5)
            assert response.status_code == 200
            pm = remote.state.pm_agent if mode == "worker" else remote._local_pm_agent if remote else controller._local_pm_agent
            try:
                pm._thread.join(timeout=5)
                pm._progress_thread.join(timeout=5)
                assert not pm._thread.is_alive() and not pm._progress_thread.is_alive()
                task = controller.db.get_task(response.json()["task_id"])
                assert task.status == "completed" and task.output_data["_delivery"]["deliverable"] == "完整交付产物"
                assert controller.db.get_pm_agent(task.pm_agent_id).status == "completed"
                assert "_pm_start" not in task.output_data
                if lost_response:
                    assert len(sent) == 1
            finally:
                pm.stop()
                pm._thread.join(timeout=2)
                pm._progress_thread.join(timeout=2)
