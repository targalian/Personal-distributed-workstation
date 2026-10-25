"""Concurrency and integration regressions for shared PM ownership and scheduling."""

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lan_mesh.database import Database
from lan_mesh.pm_lifecycle_service import call_pm, get_pm_service
from lan_mesh.pm_control_service import call_pm_control
from lan_mesh.protocol import HostRecord, Task
from lan_mesh.service_runtime import get_service_runtime
from lan_mesh.station_controller import StationController
from lan_mesh.station_routes_tasks import build_task_routes
from lan_mesh.task_scheduling_service import call_schedule, get_scheduling_service


@pytest.fixture
def ctl(tmp_path, monkeypatch):
    class Controller:
        _local_start_pm = StationController._local_start_pm
        _local_cancel_pm = StationController._local_cancel_pm
        _local_stop_pm = StationController._local_stop_pm
        _local_pause_pm = StationController._local_pause_pm
        _local_inject_input = StationController._local_inject_input
        _local_pm_status = StationController._local_pm_status
        _dispatch_queued_task = StationController._dispatch_queued_task
        _register_local_pm = StationController._register_local_pm
        _pick_task_host = StationController._pick_task_host
        _federation_forward_task = StationController._federation_forward_task
        submit_task_from_chat = StationController.submit_task_from_chat

    controller = Controller()
    controller.db = Database(str(tmp_path / "pm.db"))
    controller.state = SimpleNamespace(device_id="self", device_name="Station", api_port=8080,
                                       ws_clients=set(), shared_folder=SimpleNamespace(path=tmp_path))
    controller._local_pm_agent = None
    controller._local_sub_agents = {}
    controller._pm_worker_map = {}
    controller.chat_runtime = object()
    controller.secretary_active = True
    controller.project_manager = None
    controller.discovery = None
    controller.chat_handler = None
    controller.events = []
    controller._queue_ws_broadcast = lambda kind, data: controller.events.append((kind, data))
    controller._auto_attach_pm_thread = lambda *args: None
    controller.bot_gateway = SimpleNamespace(notify=lambda *args: None)
    from lan_mesh import host_info
    monkeypatch.setattr(host_info, "pick_reachable_ip", lambda _: "10.1.1.1")
    yield controller
    runtime = vars(controller).get("services")
    if runtime:
        runtime.close()


@pytest.fixture
def fake_pm(monkeypatch):
    class PM:
        instances = []

        def __init__(self, **kwargs):
            self.pm_id = kwargs["pm_id"]
            self._running = False
            self.cancelled = 0
            self.paused = 0
            self.inputs = []
            self.instances.append(self)

        def start_task(self, data):
            self._running = True

        def resume_from_snapshot(self, snapshot):
            self._running = True
            return True

        def cancel(self):
            self.cancelled += 1
            self._running = False

        def pause(self):
            self.paused += 1
            self._running = False

        def receive_input(self, payload):
            self.inputs.append(payload)

        def get_status(self):
            return {"running": self._running, "pm_id": self.pm_id}

    from lan_mesh import pm_agent
    monkeypatch.setattr(pm_agent, "ProjectManagerAgent", PM)
    return PM


def test_concurrent_starts_have_one_owner(ctl, fake_pm, monkeypatch):
    barrier = threading.Barrier(12)
    entered, second, release = threading.Event(), threading.Event(), threading.Event()
    original = fake_pm.__init__

    def slow_constructor(self, **kwargs):
        original(self, **kwargs)
        if entered.is_set():
            second.set()
        entered.set()
        assert release.wait(3)

    monkeypatch.setattr(fake_pm, "__init__", slow_constructor)

    def start(i):
        barrier.wait(timeout=3)
        return ctl._local_start_pm(f"task-{i}", "http://secretary", {"name": str(i)})

    with ThreadPoolExecutor(12) as pool:
        futures = [pool.submit(start, i) for i in range(12)]
        try:
            assert entered.wait(2)
            assert not second.wait(.1), "another constructor entered before the first owned its slot"
        finally:
            release.set()
        results = [future.result(timeout=2) for future in futures]
    assert sum(bool(result["ok"]) for result in results) == 1
    assert len(fake_pm.instances) == 1
    assert ctl._local_pm_agent is fake_pm.instances[0]


def test_same_task_start_is_idempotent(ctl, fake_pm):
    first = ctl._local_start_pm("same", "http://secretary", {"name": "same"})
    second = ctl._local_start_pm("same", "http://secretary", {"name": "same"})
    assert first == second and len(fake_pm.instances) == 1


def test_start_and_resume_share_one_slot(ctl, fake_pm):
    ctl.db.save_task(Task(task_id="resume", name="resume"))
    ctl.db.get_pm_snapshot_by_task = lambda _: {"pm_id": "saved-pm"}
    barrier = threading.Barrier(2)

    def launch(resume):
        barrier.wait(timeout=3)
        return (call_pm(ctl, "resume", task_id="resume") if resume else
                ctl._local_start_pm("new", "http://secretary", {"name": "new"}))

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(launch, [True, False]))
    assert sum(bool(result["ok"]) for result in results) == 1
    assert len(fake_pm.instances) == 1


def test_resume_restores_registration(ctl, fake_pm):
    ctl.db.save_task(Task(task_id="resume", name="resume", project_id="project"))
    ctl.db.get_pm_snapshot_by_task = lambda _: {"pm_id": "saved-pm"}
    result = call_pm(ctl, "resume", task_id="resume")
    assert result["ok"]
    assert ctl._pm_worker_map["saved-pm"]["task_id"] == "resume"
    assert ctl.db.get_pm_agent("saved-pm").project_id == "project"


def test_cancelled_threads_retire_before_slot_reuse(ctl, fake_pm):
    entered, release = threading.Event(), threading.Event()

    def running():
        entered.set()
        release.wait(5)

    ctl._local_start_pm("old", "http://secretary", {"name": "old"})
    old = ctl._local_pm_agent
    old._thread = threading.Thread(target=running)
    old._thread.start()
    try:
        assert entered.wait(2)
        assert ctl._local_cancel_pm()["ok"]
        assert ctl._local_pm_agent is None
        assert not ctl._local_start_pm("new", "http://secretary", {"name": "new"})["ok"]
    finally:
        release.set()
        old._thread.join(2)
    assert ctl._local_start_pm("new", "http://secretary", {"name": "new"})["ok"]


def test_cancel_callback_can_enter_lifecycle_without_deadlock(ctl, fake_pm):
    ctl._local_start_pm("old", "http://secretary", {"name": "old"})
    owner = get_pm_service(ctl)

    def cancel():
        ctl._local_pm_agent = None
        with ThreadPoolExecutor(1) as pool:
            result = pool.submit(ctl._local_start_pm, "new", "http://secretary", {"name": "new"})
            assert result.result(timeout=2)["ok"] is False

    ctl._local_pm_agent.cancel = cancel
    assert owner.cancel()["ok"]


def test_failed_start_releases_slot(ctl, fake_pm, monkeypatch):
    def broken(self, data):
        self._running = True
        raise RuntimeError("launch failed")

    original = fake_pm.start_task
    monkeypatch.setattr(fake_pm, "start_task", broken)
    result = ctl._local_start_pm("bad", "http://secretary", {"name": "bad"})
    assert not result["ok"] and "launch failed" in result["message"]
    assert fake_pm.instances[0].cancelled == 1
    assert ctl._local_pm_agent is None
    monkeypatch.setattr(fake_pm, "start_task", original)
    assert ctl._local_start_pm("good", "http://secretary", {"name": "good"})["ok"]


def test_stale_controls_do_not_touch_new_owner(ctl, fake_pm):
    ctl._local_start_pm("new", "http://secretary", {"name": "new"})
    active = ctl._local_pm_agent
    old = Task(task_id="old", name="old", status="running", pm_agent_id="old-pm")
    ctl.db.save_task(old)
    ctl._pm_worker_map["old-pm"] = {"local": True}
    assert not call_pm_control(ctl, "pause_task", task_id="old")["ok"]
    assert not call_pm_control(ctl, "inject_input_to_pm", pm_id="old-pm", input_data={})["ok"]
    assert call_pm_control(ctl, "cancel_task", task_id="old")["ok"]
    assert active.cancelled == active.paused == 0 and active.inputs == []
    assert ctl._local_pm_agent is active and active._running
    assert ctl.db.get_task("old").status == "cancelled"


def test_failed_cancel_preserves_task_and_owner(ctl, fake_pm):
    ctl._local_start_pm("t", "http://secretary", {"name": "t"})
    active = ctl._local_pm_agent
    ctl.db.save_task(Task(task_id="t", name="t", status="running", pm_agent_id=active.pm_id))
    ctl._pm_worker_map[active.pm_id] = {"local": True}

    def broken():
        raise RuntimeError("cancel failed")

    active.cancel = broken
    assert not call_pm_control(ctl, "cancel_task", task_id="t")["ok"]
    assert ctl.db.get_task("t").status == "running"
    assert ctl._local_pm_agent is active


def test_http_and_chat_share_lan_first_policy(ctl, monkeypatch):
    from lan_mesh import station_scheduler
    ctl.chat_runtime = None
    for device, source, tier in [("lan", "lan", "B"), ("fed", "fed", "S")]:
        ctl.db.upsert_host(HostRecord(device_id=device, device_name=device, hostname=device,
                                    ip="10.1.1.2", api_port=8081, online=True, source=source,
                                    role="secretary", rating_tier=tier))
    requests = []

    def post(url, **kwargs):
        requests.append(url)
        return SimpleNamespace(status_code=200, json=lambda: {"pm_id": f"remote-{len(requests)}"})

    monkeypatch.setattr(station_scheduler, "http_post", post)
    app = FastAPI()
    app.include_router(build_task_routes(ctl))
    with TestClient(app) as client:
        http = client.post("/api/tasks", json={"name": "http", "project_id": "pid"}).json()
        chat = ctl.submit_task_from_chat("chat", "description", project_id="pid")
        assert http["status"] == chat["status"] == "running"
        assert all("/role/start-pm" in url for url in requests)
        assert len(requests) == 2
        for result in [http, chat]:
            assert ctl.db.get_pm_agent(result["pm_agent_id"]).project_id == "pid"


def test_concurrent_remote_claim_sends_once(ctl, fake_pm, monkeypatch):
    from lan_mesh import station_scheduler
    entered, release = threading.Event(), threading.Event()
    ctl.db.save_task(Task(task_id="claim", name="claim"))
    worker = HostRecord(device_id="remote", device_name="Worker", ip="10.1.1.2", api_port=8081)
    calls = []

    def post(url, **kwargs):
        calls.append(url)
        entered.set()
        assert release.wait(3)
        return SimpleNamespace(status_code=200, json=lambda: {"pm_id": "remote-pm"})

    monkeypatch.setattr(station_scheduler, "http_post", post)
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(call_schedule, ctl, "dispatch_to_worker",
                            task=ctl.db.get_task("claim"), worker_host=worker)
        try:
            assert entered.wait(2)
            assert call_schedule(ctl, "dispatch_to_worker", task=ctl.db.get_task("claim"), worker_host=worker) is False
            assert call_schedule(ctl, "dispatch_local", task=ctl.db.get_task("claim")) is False
        finally:
            release.set()
        assert first.result(timeout=2)
    assert len(calls) == 1
    assert not get_scheduling_service(ctl)._claims


@pytest.mark.parametrize("action", ["stop", "pause", "cancel"])
def test_worker_identity_guard_and_legacy_body(ctl, fake_pm, action):
    from lan_mesh.station_routes_worker import build_worker_routes
    ctl._local_start_pm("new", "http://secretary", {"name": "new"})
    active = ctl._local_pm_agent
    app = FastAPI()
    app.include_router(build_worker_routes(ctl))
    with TestClient(app) as client:
        assert client.post(f"/role/{action}-pm", json={"pm_id": "old-pm"}).status_code == 409
        assert active.cancelled == active.paused == 0
        assert client.post(f"/role/{action}-pm").status_code == 200


def test_sync_runtime_cannot_drop_async_work(ctl):
    app = FastAPI()
    app.include_router(build_task_routes(ctl))
    with pytest.raises(TypeError, match="await"):
        get_service_runtime(ctl).request_sync("/TaskService/list_tasks")
    assert asyncio.run(ctl.services.request("/TaskService/list_tasks"))["total"] == 0


def test_remote_injection_checks_identity_and_keeps_payload(ctl, fake_pm):
    from lan_mesh.station_routes_worker import build_worker_routes
    ctl._local_start_pm("t", "http://secretary", {"name": "t"})
    active = ctl._local_pm_agent
    app = FastAPI()
    app.include_router(build_worker_routes(ctl))
    with TestClient(app) as client:
        payload = {"response": "boss input"}
        assert client.post("/pm/inject-input", json=payload,
                           headers={"X-PM-ID": "old-pm"}).status_code == 409
        assert not active.inputs
        assert client.post("/pm/inject-input", json=payload,
                           headers={"X-PM-ID": active.pm_id}).status_code == 200
        assert active.inputs == [payload]
