"""Verify original-node inspection survives restart and never redispatches or rewrites results."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lan_mesh.execution_receipts import ExecutionReceipts
from lan_mesh.pm_inspection import inspect_pm_start, inspect_task_pm
from lan_mesh.protocol import PMAgent, Task
from lan_mesh.station_routes_tasks import build_task_routes
from tests.test_pm_start import controller as _controller_fixture
from tests.test_pm_start import live_server

controller = _controller_fixture


def record(path, result=None):
    store = ExecutionReceipts(path, owner="old-process")
    store.claim({"subtask_id": "pm-test", "pm_id": "pm-test", "parent_task_id": "task-test"})
    if result is not None:
        store.finish("pm-test", {"status": "completed", "output": result})
    return store


def bind(controller, device="remote", port=12345):
    controller.db.save_task(Task(task_id="task-test", name="unknown"))
    assert controller.db.bind_task_pm(PMAgent(pm_id="pm-test", task_id="task-test", device_id=device,
                                            hostname="原节点", ip="127.0.0.1", api_port=port, status="starting"))
    controller.db.note_pm_start_issue("pm-test", "启动响应丢失")
    return controller.db.get_task("task-test")


@pytest.mark.parametrize("result,state", [
    (None, "unknown"), ({"ok": True, "uncertain": False}, "unknown"),
    ({"ok": False, "uncertain": True}, "unknown"), ({"ok": False, "uncertain": False}, "start_failed"),
])
def test_old_process_receipt_is_not_a_running_or_completed_pm(tmp_path, result, state):
    path = tmp_path / "starts.sqlite3"
    store = record(path, result)
    before = path.read_bytes()
    answer = inspect_pm_start(path, "pm-test", "task-test", "remote", {})
    assert answer["state"] == state and answer["startup_recorded"]
    assert path.read_bytes() == before
    assert "output" not in answer and "process_owner" not in answer
    assert store.get("pm-test") is not None


def test_missing_database_is_not_created_and_missing_id_is_not_claimed(tmp_path):
    path = tmp_path / "missing" / "starts.sqlite3"
    assert inspect_pm_start(path, "pm-test", "task-test", "remote", {})["state"] == "not_found"
    assert not path.parent.exists()
    record(path)
    assert inspect_pm_start(path, "other", "other-task", "remote", {})["state"] == "not_found"
    assert ExecutionReceipts(path).get("other") is None


@pytest.mark.parametrize("running,state", [(True, "running"), (False, "inactive")])
def test_current_pm_observation_is_bound_to_all_identities(tmp_path, running, state):
    path = tmp_path / "starts.sqlite3"
    record(path, {"ok": True})
    current = {"pm_id": "pm-test", "task_id": "task-test", "device_id": "remote", "running": running}
    assert inspect_pm_start(path, "pm-test", "task-test", "remote", current)["state"] == state
    for field in ("task_id", "device_id"):
        assert inspect_pm_start(path, "pm-test", "task-test", "remote", {**current, field: "other"})["state"] == "mismatch"
    assert inspect_pm_start(path, "pm-test", "different-task", "remote", {})["state"] == "mismatch"
    assert inspect_pm_start(path, "pm-test", "task-test", "remote", {**current, "pm_id": "other"})["state"] == "unknown"


@pytest.mark.parametrize("mode", ["station", "worker"])
def test_real_original_node_http_read_survives_both_processes_losing_memory(controller, tmp_path, monkeypatch, mode):
    from lan_mesh.database import Database
    from lan_mesh.station_controller import StationController
    from lan_mesh.station_routes_worker import build_worker_routes
    from lan_mesh.worker import WorkerAgent
    from lan_mesh.worker_routes_pm import build_pm_routes
    app = FastAPI()
    if mode == "station":
        remote = StationController.__new__(StationController)
        remote.db = Database(str(tmp_path / "remote.sqlite3"))
        remote.state = SimpleNamespace(device_id="remote")
        remote.discovery = Mock()
        remote._local_pm_agent = None
        receipt_path = remote.db.path.with_name("remote-pm-starts.sqlite3")
        app.include_router(build_worker_routes(remote))
    else:
        remote = WorkerAgent.__new__(WorkerAgent)
        remote.state = SimpleNamespace(device_id="remote", pm_agent=None)
        remote.data_dir = tmp_path / "worker"
        receipt_path = remote.data_dir / "worker-pm-starts.sqlite3"
        app.include_router(build_pm_routes(remote))
    record(receipt_path, {"ok": True, "uncertain": False, "process_owner": "old-process"})
    factory = Mock(side_effect=AssertionError("inspection must not start PM"))
    monkeypatch.setattr("lan_mesh.pm_start.ProjectManagerAgent", factory)
    with live_server(app) as port:
        bind(controller, port=port)
        assert not controller._pm_worker_map
        before = controller.db.get_task("task-test").to_dict()
        answer = inspect_task_pm(controller, "task-test")
        assert answer["state"] == "unknown" and answer["startup_recorded"]
        assert answer["node"]["port"] == port
        assert controller.db.get_task("task-test").to_dict() == before
        factory.assert_not_called()


def test_local_query_and_terminal_review_are_read_only(controller, monkeypatch):
    bind(controller, device="self")
    record(controller.db.path.with_name(controller.db.path.stem + "-pm-starts.sqlite3"), {"ok": True})
    controller.db.record_task_delivery("task-test", "pm-test", {"deliverable": "reviewed", "summary": "result"})
    controller.db.review_task_delivery("task-test", True)
    before = controller.db.get_task("task-test").to_dict()
    before_pm = controller.db.get_pm_agent("pm-test").to_dict()
    network = Mock(side_effect=AssertionError("local inspection must not use network"))
    monkeypatch.setattr("lan_mesh.pm_inspection.http_get", network)
    controller._local_pm_agent = SimpleNamespace(get_status=lambda: {
        "pm_id": "pm-test", "task_id": "task-test", "device_id": "self", "running": False})
    app = FastAPI()
    app.include_router(build_task_routes(controller))
    client = TestClient(app)
    for _ in range(2):
        answer = client.get("/api/tasks/task-test/execution-check")
        assert answer.status_code == 200 and answer.json()["state"] == "inactive"
    assert controller.db.get_task("task-test").to_dict() == before
    assert controller.db.get_pm_agent("pm-test").to_dict() == before_pm
    network.assert_not_called()
    assert client.get("/api/tasks/missing/execution-check").status_code == 404
    controller.secretary_active = False
    assert client.get("/api/tasks/task-test/execution-check").status_code == 503


@pytest.mark.parametrize("response,state", [
    (None, "unavailable"), (Mock(status_code=404), "unsupported"), (Mock(status_code=401), "unavailable"),
    (Mock(status_code=302), "unavailable"), (Mock(status_code=500), "unavailable"),
    (Mock(status_code=200, json=lambda: []), "unavailable"),
    (Mock(status_code=200, json=Mock(side_effect=ValueError("invalid JSON"))), "unavailable"),
])
def test_unreachable_old_or_invalid_node_stays_uncertain(controller, monkeypatch, response, state):
    bind(controller)
    before = controller.db.get_task("task-test").to_dict()
    get = Mock(return_value=response)
    monkeypatch.setattr("lan_mesh.pm_inspection.http_get", get)
    answer = inspect_task_pm(controller, "task-test")
    assert answer["state"] == state
    assert controller.db.get_task("task-test").to_dict() == before
    assert get.call_count == 1 and get.call_args.kwargs["allow_redirects"] is False
    assert get.call_args.kwargs["retries"] == 0 and get.call_args.kwargs["params"] == {"task_id": "task-test"}


@pytest.mark.parametrize("field", ["device_id", "pm_id", "task_id", "format"])
def test_response_identity_mismatch_never_confirms_execution(controller, monkeypatch, field):
    bind(controller)
    evidence = {"format": 1, "pm_id": "pm-test", "task_id": "task-test", "device_id": "remote", "state": "running"}
    evidence[field] = "other"
    monkeypatch.setattr("lan_mesh.pm_inspection.http_get", lambda *a, **k: Mock(status_code=200, json=lambda: evidence))
    assert inspect_task_pm(controller, "task-test")["state"] == "mismatch"
    assert controller.db.get_pm_agent("pm-test").status == "unknown"


@pytest.mark.parametrize("change", ["cancel", "delete", "rebind"])
def test_task_changed_during_query_does_not_show_stale_confirmation(controller, monkeypatch, change):
    bind(controller)
    def get(*args, **kwargs):
        task = controller.db.get_task("task-test")
        if change == "delete":
            controller.db.delete_task(task.task_id)
        else:
            if change == "cancel":
                task.status = "cancelled"
            else:
                task.pm_agent_id = "other-pm"
            controller.db.save_task(task)
        return Mock(status_code=200, json=lambda: {"format": 1, "pm_id": "pm-test", "task_id": "task-test",
                                                  "device_id": "remote", "state": "running"})
    monkeypatch.setattr("lan_mesh.pm_inspection.http_get", get)
    assert inspect_task_pm(controller, "task-test")["state"] == "changed"


def test_corrupt_receipt_is_not_repaired_or_exposed(controller):
    bind(controller, device="self")
    path = controller.db.path.with_name(controller.db.path.stem + "-pm-starts.sqlite3")
    path.write_bytes(b"corrupted-secret-data")
    answer = inspect_task_pm(controller, "task-test")
    assert answer["state"] == "unavailable" and "secret-data" not in str(answer)
    assert path.read_bytes() == b"corrupted-secret-data"


def test_unbound_task_does_not_contact_a_node(controller, monkeypatch):
    controller.db.save_task(Task(task_id="unbound"))
    get = Mock()
    monkeypatch.setattr("lan_mesh.pm_inspection.http_get", get)
    assert inspect_task_pm(controller, "unbound")["state"] == "unbound"
    get.assert_not_called()
