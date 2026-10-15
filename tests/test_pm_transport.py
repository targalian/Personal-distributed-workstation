"""Exercise lost dispatch responses, receipt polling and concurrent stale callbacks."""
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests
import uvicorn
from fastapi import FastAPI

from tests.test_pm_lifecycle import pm as pm_fixture
from tests.test_pm_lifecycle import task

pm = pm_fixture


def start_attempt(pm, monkeypatch):
    pm._state.task = task()
    pm._state.plan = {"decomposition": [{"name": "A"}]}
    station = {"device_id": "remote", "ip": "127.0.0.1", "api_port": 1}
    pm._state.task_station["A"] = station
    pm._state.task_agent["A"] = {"agent_id": "remote-agent"}
    posted = []
    def post(url, **kwargs):
        posted.append(kwargs["json"])
        return Mock(status_code=200, json=lambda: {**kwargs["json"], "status": "started"})
    monkeypatch.setattr("lan_mesh.pm_dispatcher.requests.post", post)
    pm._dispatcher.dispatch_subtask(station, {"agent_id": "remote-agent"}, task(), {"name": "A"})
    attempt = pm._state.execution_attempts["A"]
    return {key: attempt[key] for key in ("subtask_id", "parent_task_id", "pm_id", "reporter_id")}, posted


@pytest.mark.parametrize("error", [requests.ConnectionError("response lost"), requests.ReadTimeout("timeout"),
                                  ValueError("invalid response JSON"), None])
def test_uncertain_dispatch_never_falls_back_to_local(pm, monkeypatch, error):
    if error:
        post = Mock(side_effect=error)
    else:
        post = Mock(return_value=Mock(status_code=503))
    monkeypatch.setattr("lan_mesh.pm_dispatcher.requests.post", post)
    pm._state.task = task()
    pm._dispatcher.dispatch_subtask({"ip": "127.0.0.1", "api_port": 1}, {"agent_id": "a"}, task(), {"name": "A"})
    pm.runtime.execute.assert_not_called()
    assert post.call_count == 1
    assert pm._state.execution_attempts["A"]["phase"] == "active"


def test_concurrent_failure_callback_retries_once_and_rejects_old_result(pm, monkeypatch):
    identity, posted = start_attempt(pm, monkeypatch)
    report = {**identity, "task_name": "A", "status": "failed", "message": "failure"}
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: pm.receive_progress_report(report), range(8)))
    assert len(posted) == 2 and pm._state.retry_counts["A"] == 1
    pm.receive_progress_report({**report, "status": "completed", "output": {"summary": "obsolete"}})
    assert not pm._state.subtask_outputs
    current = pm._state.execution_attempts["A"]
    pm.receive_progress_report({**identity, "subtask_id": current["subtask_id"], "task_name": "A",
                                "status": "completed", "output": {"summary": "current"}})
    assert pm._state.subtask_outputs["A"] == {"summary": "current"}


@pytest.mark.parametrize("field", ["subtask_id", "pm_id", "parent_task_id", "reporter_id"])
def test_wrong_identity_cannot_complete_or_retry(pm, monkeypatch, field):
    identity, posted = start_attempt(pm, monkeypatch)
    for status in ("completed", "failed"):
        pm.receive_progress_report({**identity, field: "wrong", "task_name": "A", "status": status,
                                    "output": {"summary": "unrelated"}})
    assert not pm._state.subtask_outputs and not pm._state.retry_counts and len(posted) == 1
    pm.receive_progress_report({"task_name": "A", "status": "failed"})
    assert len(posted) == 1


def test_duplicate_success_during_quality_check_is_consumed_once(pm, monkeypatch):
    identity, _ = start_attempt(pm, monkeypatch)
    entered, release = threading.Event(), threading.Event()
    def verify(*args):
        entered.set()
        assert release.wait(2)
        return {"accepted": True}
    pm._monitor._verify_output_quality = Mock(side_effect=verify)
    report = {**identity, "task_name": "A", "status": "completed", "output": {"summary": "verified"}}
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(pm.receive_progress_report, report)
        try:
            assert entered.wait(1)
            pm.receive_progress_report(report)
        finally:
            release.set()
        first.result(timeout=2)
    pm._monitor._verify_output_quality.assert_called_once()
    assert pm._state.subtask_outputs["A"] == {"summary": "verified"}


def test_restarted_executor_unknown_receipt_stops_without_retry(pm, monkeypatch):
    identity, posted = start_attempt(pm, monkeypatch)
    receipt = {**identity, "name": "A", "status": "unknown", "error": "进程已变化，结果待核实"}
    monkeypatch.setattr("lan_mesh.pm_dispatcher.requests.get", Mock(return_value=Mock(status_code=200, json=lambda: receipt)))
    pm._dispatcher.poll_remote_results()
    assert pm._state.stop_reason == "failed" and "待核实" in pm._state.terminal_failures["A"]
    assert len(posted) == 1 and not pm._state.retry_counts


@pytest.mark.parametrize("status", [404, 500])
def test_missing_receipt_does_not_restart_execution(pm, monkeypatch, status):
    _, posted = start_attempt(pm, monkeypatch)
    monkeypatch.setattr("lan_mesh.pm_dispatcher.requests.get", Mock(return_value=Mock(status_code=status)))
    pm._dispatcher.poll_remote_results()
    pm._dispatcher.poll_remote_results()
    assert len(posted) == 1 and not pm._state.retry_counts


def test_legacy_async_station_requires_upgrade_and_result_verification(pm, monkeypatch):
    pm._state.task = task()
    pm._state.plan = {"decomposition": [{"name": "A"}]}
    monkeypatch.setattr("lan_mesh.pm_dispatcher.requests.post",
                        Mock(return_value=Mock(status_code=200, json=lambda: {"status": "started", "name": "A"})))
    pm._dispatcher.dispatch_subtask({"ip": "127.0.0.1", "api_port": 1}, {"agent_id": "a"}, task(), {"name": "A"})
    assert "升级" in pm._state.terminal_failures["A"]
    assert "核实" in pm._state.terminal_failures["A"]
    pm.runtime.execute.assert_not_called()


def test_receipt_from_other_task_cannot_be_relabelled_as_current(pm, monkeypatch):
    identity, _ = start_attempt(pm, monkeypatch)
    receipt = {**identity, "parent_task_id": "other-task", "name": "A", "status": "completed", "output": {"summary": "other"}}
    pm._dispatcher._receive_receipt(pm._state.execution_attempts["A"], receipt)
    assert not pm._state.subtask_outputs


@pytest.mark.parametrize("worker", [False, True])
@pytest.mark.parametrize("lost_response", [False, True])
def test_real_http_remote_dependency_chain_reaches_delivery(pm, tmp_path, monkeypatch, worker, lost_response):
    """Use actual HTTP/PM threads/SQLite, with only the model runtime replaced."""
    from lan_mesh.station_controller import StationController
    from lan_mesh.station_routes_worker import build_worker_routes
    from lan_mesh.worker_routes_basic import build_basic_routes
    executed = []
    def execute(subtask):
        if subtask["name"] == "B":
            assert subtask["input_data"]["_dependency_outputs"]["A"] == {"summary": "A output"}
        executed.append(subtask["name"])
        return {"status": "completed", "output": {"summary": subtask["name"] + " output"}}
    remote_runtime = Mock(execute=Mock(side_effect=execute))
    app = FastAPI()
    if worker:
        app.include_router(build_basic_routes(Mock(), Mock(), remote_runtime, tmp_path / "worker.sqlite3"))
    else:
        controller = StationController.__new__(StationController)
        controller.db = SimpleNamespace(path=tmp_path / "station.sqlite3")
        controller.state = SimpleNamespace()
        controller.discovery = Mock()
        controller._local_sub_agents = {"remote-agent": {"runtime": remote_runtime}}
        controller._local_pm_agent = None  # The owning PM lives on another node.
        app.include_router(build_worker_routes(controller))
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 3
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started
        pm._dispatcher.get_available_stations = lambda: [{"device_id": "remote", "ip": "127.0.0.1", "api_port": port}]
        pm._dispatcher._create_subagent_on_station = lambda *a, **kw: {"agent_id": "remote-agent"}
        if lost_response:
            actual_post = requests.post
            def lose_receipt(url, **kwargs):
                response = actual_post(url, **kwargs)
                assert response.status_code == 200
                raise requests.ConnectionError("response lost after remote accepted")
            monkeypatch.setattr("lan_mesh.pm_dispatcher.requests.post", lose_receipt)
        pm.start_task(task())
        pm._thread.join(timeout=5)
        pm._progress_thread.join(timeout=5)
        assert not pm._thread.is_alive() and not pm._progress_thread.is_alive()
        assert executed == ["A", "B"]
        assert sum(url.endswith("/deliver") for url, _ in pm.test_calls) == 1
        pm.runtime.execute.assert_not_called()
        assert not pm._state.retry_counts
    finally:
        server.should_exit = True
        thread.join(timeout=3)
        sock.close()
        assert not thread.is_alive()
