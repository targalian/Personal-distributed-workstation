"""Verify durable execution claims, restart ambiguity and Station/Worker HTTP receipts."""
import json
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lan_mesh.execution_receipts import ExecutionReceipts
from lan_mesh.execution_routes import build_execution_routes


def payload(**changes):
    return {"subtask_id": "attempt-1", "pm_id": "pm-origin", "parent_task_id": "task-1",
            "reporter_id": "agent-1", "name": "A", "input_data": {"requirement": "test"}, **changes}


def test_concurrent_claims_across_connections_execute_once(tmp_path):
    path = tmp_path / "receipts.sqlite3"
    stores = [ExecutionReceipts(path) for _ in range(8)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda store: store.claim(payload()), stores))
    assert sum(claimed for claimed, _ in results) == 1
    with pytest.raises(ValueError, match="不同请求"):
        stores[0].claim(payload(input_data={"requirement": "changed"}))
    stores[0].finish("attempt-1", {"status": "completed", "output": {"code": "original"}})
    stores[1].finish("attempt-1", {"status": "failed"})
    reloaded = ExecutionReceipts(path, owner="new-process")
    claimed, receipt = reloaded.claim(payload())
    assert not claimed and receipt["output"] == {"code": "original"}


def test_restart_never_reexecutes_unfinished_claim(tmp_path):
    path = tmp_path / "receipts.sqlite3"
    old = ExecutionReceipts(path, owner="old-process")
    assert old.claim(payload())[0]
    new = ExecutionReceipts(path, owner="new-process")
    claimed, receipt = new.claim(payload())
    assert not claimed and receipt["status"] == "unknown"
    assert "待核实" in receipt["error"]
    new.finish("attempt-1", {"status": "completed"})
    assert new.get("attempt-1")["status"] == "unknown"


@pytest.mark.parametrize("completed", [False, True])
def test_receipts_survive_executor_process_exit(tmp_path, completed):
    path = tmp_path / "receipts.sqlite3"
    script = """
import json, sys
from pathlib import Path
from lan_mesh.execution_receipts import ExecutionReceipts
store = ExecutionReceipts(Path(sys.argv[1]))
assert store.claim(json.loads(sys.argv[2]))[0]
if sys.argv[3] == 'yes':
    store.finish('attempt-1', {'status': 'completed', 'output': {'summary': 'persisted'}})
"""
    subprocess.run([sys.executable, "-c", script, str(path), json.dumps(payload()), "yes" if completed else "no"],
                   check=True, timeout=10)
    store = ExecutionReceipts(path)
    claimed, receipt = store.claim(payload())
    assert not claimed
    assert receipt["status"] == ("completed" if completed else "unknown")


@pytest.mark.parametrize("synchronous", [False, True])
def test_http_duplicate_during_execution_returns_receipt(tmp_path, synchronous):
    entered, release = threading.Event(), threading.Event()
    calls = []
    def execute(task):
        calls.append(task)
        entered.set()
        assert release.wait(3)
        return {"status": "completed", "output": {"summary": "actual result"}}
    app = FastAPI()
    app.include_router(build_execution_routes(execute, tmp_path / "receipts.sqlite3", synchronous))
    with TestClient(app) as client, ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(client.post, "/tasks/execute", json=payload())
        try:
            assert entered.wait(2)
            duplicate = client.post("/tasks/execute", json=payload())
            assert duplicate.status_code == 200
            assert duplicate.json()["duplicate"] and duplicate.json()["status"] == "started"
            assert client.post("/tasks/execute", json=payload(name="changed")).status_code == 409
        finally:
            release.set()
        assert first.result(timeout=3).status_code == 200
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            receipt = client.get("/tasks/executions/attempt-1").json()
            if receipt["status"] == "completed":
                break
            time.sleep(0.01)
        assert receipt["status"] == "completed" and receipt["pm_id"] == "pm-origin"
        assert receipt["output"]["summary"] == "actual result"
        assert client.post("/tasks/execute", json=payload()).json()["duplicate"]
        assert len(calls) == 1
        assert client.get("/tasks/executions/missing").status_code == 404


def test_station_execution_does_not_report_to_unrelated_local_pm(tmp_path):
    from lan_mesh.station_controller import StationController
    from lan_mesh.station_routes_worker import build_worker_routes
    controller = StationController.__new__(StationController)
    controller.db = SimpleNamespace(path=tmp_path / "station.sqlite3")
    controller.state = SimpleNamespace()
    controller.discovery = Mock()
    controller._local_sub_agents = {"agent-1": {"runtime": Mock()}}
    runtime = controller._local_sub_agents["agent-1"]["runtime"]
    runtime.execute.return_value = {"status": "completed", "output": {"summary": "remote output"}}
    controller._local_pm_agent = Mock(pm_id="unrelated-pm", running=True)
    app = FastAPI()
    app.include_router(build_worker_routes(controller))
    with TestClient(app) as client:
        assert client.post("/tasks/execute", json=payload()).json()["status"] == "started"
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            receipt = client.get("/tasks/executions/attempt-1").json()
            if receipt["status"] == "completed":
                break
            time.sleep(0.01)
        assert receipt["output"] == {"summary": "remote output"}
        controller._local_pm_agent.receive_subtask_result.assert_not_called()


def test_worker_sync_execution_has_queryable_receipt(tmp_path):
    from lan_mesh.worker_routes_basic import build_basic_routes
    runtime = Mock()
    runtime.execute.return_value = {"status": "completed", "output": {"summary": "worker output"}}
    app = FastAPI()
    app.include_router(build_basic_routes(Mock(), Mock(), runtime, tmp_path / "worker.sqlite3"))
    with TestClient(app) as client:
        receipt = client.post("/tasks/execute", json=payload()).json()
        assert receipt["status"] == "completed"
        assert client.get("/tasks/executions/attempt-1").json()["output"] == receipt["output"]
        assert client.post("/tasks/execute", json=payload()).json()["duplicate"]
    runtime.execute.assert_called_once()


@pytest.mark.parametrize("changes", [{"subtask_id": "../x"}, {"subtask_id": 1}, {"name": ""}, {"input_data": None}])
def test_invalid_requests_do_not_execute(tmp_path, changes):
    execute = Mock()
    app = FastAPI()
    app.include_router(build_execution_routes(execute, tmp_path / "receipts.sqlite3"))
    with TestClient(app) as client:
        assert client.post("/tasks/execute", json=payload(**changes)).status_code == 400
    execute.assert_not_called()


def test_receipt_write_failure_does_not_allow_reexecution(tmp_path, monkeypatch):
    execute = Mock(return_value={"status": "completed", "output": {"summary": "tool ran"}})
    app = FastAPI()
    app.include_router(build_execution_routes(execute, tmp_path / "receipts.sqlite3", synchronous=True))
    monkeypatch.setattr(ExecutionReceipts, "finish", Mock(side_effect=OSError("disk full")))
    with TestClient(app) as client:
        assert client.post("/tasks/execute", json=payload()).json()["status"] == "started"
        assert client.post("/tasks/execute", json=payload()).json()["duplicate"]
    execute.assert_called_once()


@pytest.mark.parametrize("result", [RuntimeError("runtime failed"),
                                   {"status": "completed", "output": {"code": "[LLM 调用失败] offline"}}])
def test_runtime_errors_are_persisted_as_failed_receipts(tmp_path, result):
    execute = Mock(side_effect=result) if isinstance(result, Exception) else Mock(return_value=result)
    app = FastAPI()
    app.include_router(build_execution_routes(execute, tmp_path / "receipts.sqlite3", synchronous=True))
    with TestClient(app) as client:
        receipt = client.post("/tasks/execute", json=payload()).json()
        assert receipt["status"] == "failed" and receipt["error"]
        assert client.get("/tasks/executions/attempt-1").json()["error"] == receipt["error"]
        client.post("/tasks/execute", json=payload())
    execute.assert_called_once()


def test_failed_claim_write_prevents_execution(tmp_path, monkeypatch):
    execute = Mock()
    app = FastAPI()
    app.include_router(build_execution_routes(execute, tmp_path / "receipts.sqlite3"))
    monkeypatch.setattr(ExecutionReceipts, "claim", Mock(side_effect=OSError("disk full")))
    with TestClient(app, raise_server_exceptions=False) as client:
        assert client.post("/tasks/execute", json=payload()).status_code == 500
    execute.assert_not_called()
