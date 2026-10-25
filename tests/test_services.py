"""Behavioral regression for real Yomk routing, isolation and task API compatibility."""

import asyncio
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lan_mesh._vendor.yomkserverpy.YomkServerPy import ResStatus, YomkService
from lan_mesh.database import Database
from lan_mesh.protocol import HostRecord, Task
from lan_mesh.service_runtime import ServiceCall, ServiceError, ServiceRuntime
from lan_mesh.station_routes_tasks import build_task_routes


@pytest.fixture
def controller(tmp_path):
    """An isolated real SQLite controller; no model or network calls."""
    ctl = SimpleNamespace(
        db=Database(str(tmp_path / "services.db")),
        state=SimpleNamespace(device_id="local", device_name="Local", api_port=8080,
                              ws_clients=set()),
        discovery=None, secretary_active=True, project_manager=None, chat_runtime=None,
        bot_gateway=SimpleNamespace(notify=lambda *args: None), _pm_worker_map={},
        chat_handler=None, _local_pm_agent=None,
    )
    yield ctl
    runtime = vars(ctl).get("services")
    if runtime:
        runtime.close()


def make_app(controller: object) -> FastAPI:
    """Mount the production task adapter without auth/network infrastructure."""
    app = FastAPI()
    app.include_router(build_task_routes(controller))
    return app


def test_real_framework_dispatch_and_introspection():
    runtime = ServiceRuntime()
    try:
        service = YomkService(runtime.server)
        service.set_name("/Echo")
        runtime.register(service, {"echo": lambda value: {"value": value}})
        result = runtime.server.request("/Echo/echo", ServiceCall({"value": 17}))
        assert result.status == ResStatus.eOk and result.data == {"value": 17}
        assert {"service": "/Echo", "operations": ["/echo"]} in runtime.describe()
        invalid = runtime.server.request("/Echo/echo", {"value": 17})
        assert invalid.status == ResStatus.eInvalid
        with pytest.raises(ValueError, match="already registered"):
            runtime.register(service, {"echo": lambda value: value})
    finally:
        runtime.close()


def test_sync_offloads_and_async_preserves_event_loop():
    runtime = ServiceRuntime()
    service = YomkService(runtime.server)
    service.set_name("/Threads")

    async def async_thread() -> int:
        return threading.get_ident()

    runtime.register(service, {"sync": threading.get_ident, "async_thread": async_thread})

    async def exercise():
        loop_thread = threading.get_ident()
        assert await runtime.request("/Threads/sync") != loop_thread
        assert await runtime.request("/Threads/async_thread") == loop_thread

    try:
        asyncio.run(exercise())
    finally:
        runtime.close()


def test_runtime_failure_shutdown_and_instance_isolation():
    first, second = ServiceRuntime(), ServiceRuntime()

    async def reject() -> dict:
        raise ServiceError(409, "conflict")

    async def broken() -> dict:
        raise RuntimeError("implementation error")

    async def exercise():
        service = YomkService(first.server)
        service.set_name("/Errors")
        first.register(service, {"reject": reject, "broken": broken})
        with pytest.raises(ServiceError, match="conflict") as error:
            await first.request("/Errors/reject")
        assert error.value.status_code == 409
        with pytest.raises(RuntimeError, match="implementation error"):
            await first.request("/Errors/broken")
        with pytest.raises(ServiceError) as missing:
            await second.request("/Errors/reject")
        assert missing.value.status_code == 404
        first.close()
        first.close()
        with pytest.raises(ServiceError) as closed:
            await first.request("/Errors/reject")
        assert closed.value.status_code == 503
        assert second.describe()

    try:
        asyncio.run(exercise())
    finally:
        first.close()
        second.close()


def test_original_openapi_contract(controller):
    expected = json.loads((Path(__file__).parent / "fixtures/task_api_contract.json")
                          .read_text(encoding="utf-8"))
    assert make_app(controller).openapi() == expected


def test_lifespan_closes_runtime_and_allows_rebuild(controller):
    with TestClient(make_app(controller)) as client:
        original = controller.services
        assert client.get("/api/tasks").status_code == 200
    assert original.closed
    with TestClient(make_app(controller)) as client:
        assert controller.services is not original
        assert client.get("/api/tasks").json() == {"tasks": [], "total": 0}


def test_live_role_and_discovery_dependencies(controller):
    with TestClient(make_app(controller)) as client:
        assert client.post("/api/agents/register", json={"agent_id": "agent-1"}).status_code == 200
        controller.discovery = SimpleNamespace(find_device=lambda _: {"ip": "10.1.2.3"})
        assert client.post("/api/agents/register", json={"agent_id": "agent-2"}).status_code == 200
        assert controller.db.get_agent("agent-2").ip == "10.1.2.3"
        controller.secretary_active = False
        assert client.get("/api/tasks").status_code == 503
        with pytest.raises(ServiceError) as error:
            asyncio.run(controller.services.request("/TaskService/list_tasks"))
        assert error.value.status_code == 503
        controller.secretary_active = True
        assert client.get("/api/tasks").status_code == 200


@pytest.mark.parametrize("path,method,payload,status", [
    ("/api/tasks/missing", "get", None, 404),
    ("/api/tasks/missing/accept", "post", None, 404),
    ("/api/tasks/t/accept", "post", None, 409),
    ("/api/tasks/t/graph", "put", {}, 400),
    ("/api/tasks/t/resume", "post", None, 404),
    ("/api/tasks", "post", None, 422),
    ("/api/tasks?limit=invalid", "get", None, 422),
])
def test_http_failure_contracts(controller, path, method, payload, status):
    controller.db.save_task(Task(task_id="t", name="test"))
    with TestClient(make_app(controller)) as client:
        assert client.request(method, path, json=payload).status_code == status


def test_delivery_roundtrip_and_internal_calls(controller):
    controller.db.save_task(Task(task_id="delivery", name="test", status="completed"))
    app = make_app(controller)
    with TestClient(app) as client:
        result = asyncio.run(controller.services.request(
            "/DeliveryService/receive_pm_delivery", pm_id="pm-1",
            payload={"task_id": "delivery", "deliverable": "artifact", "summary": "done"}))
        assert result["ok"]
        assert client.post("/api/tasks/delivery/accept").status_code == 200
        assert controller.db.get_task("delivery").status == "accepted"
        assert client.post("/api/tasks/delivery/reject", json={"reason": "revise"}).status_code == 200
        task = controller.db.get_task("delivery")
        assert task.status == "rejected"
        assert task.output_data["_delivery"]["reject_reason"] == "revise"


@pytest.mark.parametrize("kind", ["local", "queued", "remote", "missing_id", "network_error", "no_hosts"])
def test_submission_dispatch_paths(controller, monkeypatch, kind):
    from lan_mesh import host_info, station_scheduler as task_submission_service

    loop_threads = []
    controller.chat_runtime = object()
    hosts = [] if kind == "no_hosts" else [HostRecord(
        device_id="local" if kind in {"local", "queued"} else "remote",
        device_name="Worker", hostname="worker", ip="10.1.2.4", api_port=8081,
        online=True, rating_tier="A")]
    controller.db.list_hosts = lambda: hosts
    controller._local_start_pm = lambda *args: ({"ok": True, "pm_id": "local-pm"}
                                               if kind == "local" else {"ok": False})
    def reachable_ip(_):
        loop_threads.append(threading.get_ident())
        return "10.1.2.5"

    monkeypatch.setattr(host_info, "pick_reachable_ip", reachable_ip)
    called = []

    def post(url, **kwargs):
        called.append(kwargs)
        if kind == "network_error":
            raise OSError("offline")
        return SimpleNamespace(status_code=200, json=lambda: {} if kind == "missing_id"
                               else {"pm_id": "remote-pm"})

    monkeypatch.setattr(task_submission_service, "http_post", post)
    with TestClient(make_app(controller)) as client:
        data = client.post("/api/tasks", json={"name": "dispatch"}).json()
        task = controller.db.get_task(data["task_id"])
        assert "_cost_estimate" in task.input_data
        expected = {"local": "running", "remote": "running", "queued": "pending"}
        assert task.status == expected.get(kind, "failed")
        if kind == "queued":
            assert data["queued"] is True and not called
        if kind == "local":
            assert controller._pm_worker_map["local-pm"]["local"]
        if kind in {"remote", "missing_id", "network_error"}:
            assert called[0]["timeout"] == 10
            assert called[0]["json"]["secretary_url"] == "http://10.1.2.5:8080"


def test_budget_rejection_creates_no_task(controller):
    controller.project_manager = SimpleNamespace(check_budget=lambda _: False)
    with TestClient(make_app(controller)) as client:
        assert client.post("/api/tasks", json={"project_id": "over-budget"}).status_code == 402
        assert controller.db.list_tasks() == []


def test_memory_write_and_overview(controller):
    with TestClient(make_app(controller)) as client:
        for success in [True, False]:
            response = client.post("/api/pm/pm-1/task-memory", json={
                "task_name": "memory", "task_type": "analysis", "success": success,
                "collaboration_mode": "single", "duration_secs": 10,
                "task_keywords": ["test"],
            })
            assert response.status_code == 200
        data = client.get("/api/task-memory/overview?limit=1").json()
        assert data["by_type"][0]["success_rate"] == .5
        assert data["by_type"][0]["recommended_mode"] == "single"
        assert len(data["recent"]) == 1


def test_remote_dispatch_does_not_block_task_queries(controller, monkeypatch):
    from lan_mesh import host_info, station_scheduler as task_submission_service
    from starlette.concurrency import run_in_threadpool

    entered, release = threading.Event(), threading.Event()
    controller.db.list_hosts = lambda: [HostRecord(
        device_id="remote", device_name="Worker", hostname="worker",
        ip="10.1.2.4", api_port=8081, online=True)]
    monkeypatch.setattr(host_info, "pick_reachable_ip", lambda _: "10.1.2.5")

    def post(*args, **kwargs):
        entered.set()
        assert release.wait(5), "test did not release simulated network request"
        return SimpleNamespace(status_code=200, json=lambda: {"pm_id": "remote-pm"})

    monkeypatch.setattr(task_submission_service, "http_post", post)
    make_app(controller)

    async def exercise():
        submission = asyncio.create_task(controller.services.request(
            "/TaskSubmissionService/submit_task", payload={"name": "concurrent"}))
        try:
            assert await run_in_threadpool(entered.wait, 3)
            query = await asyncio.wait_for(
                controller.services.request("/TaskService/list_tasks"), timeout=2)
            assert query["total"] == 1 and query["tasks"][0]["status"] == "pending"
        finally:
            release.set()
            result = await submission
        assert result["status"] == "running"

    asyncio.run(exercise())
