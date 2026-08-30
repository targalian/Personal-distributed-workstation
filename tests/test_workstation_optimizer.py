"""Tests for the resident workstation optimization workflow."""

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from lan_mesh.database import Database
from lan_mesh.station_routes_optimization import build_optimization_routes
from lan_mesh.workstation_optimizer import WorkstationOptimizationManager


class FakeShadowManager:
    def __init__(self) -> None:
        self.submitted: list[dict] = []
        self.records: dict[str, dict] = {}

    def submit(self, task: str, simulate: bool = False) -> dict:
        run_id = f"shadow-{len(self.submitted) + 1}"
        self.submitted.append({"run_id": run_id, "task": task})
        self.records[run_id] = {
            "run_id": run_id, "status": "READY_FOR_REVIEW",
            "verdict": "READY_FOR_REVIEW",
            "diff": {"diff_file": "changes.patch", "bytes": 128},
        }
        return {"run_id": run_id, "status": "queued"}

    def get_run(self, run_id: str) -> dict | None:
        return self.records.get(run_id)


def test_database_persists_workstation_optimization_items(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "station.sqlite3"))
    now = 123.0
    item = {
        "id": "opt-test", "source": "boss", "title": "Speed up dashboard",
        "description": "Reduce first-load time", "priority": "high",
        "status": "queued", "created_at": now, "updated_at": now,
        "result": {"verdict": "READY_FOR_REVIEW"},
    }

    db.save_optimization_item(item)
    loaded = db.get_optimization_item("opt-test")

    assert loaded is not None
    assert loaded["source"] == "boss"
    assert loaded["result"]["verdict"] == "READY_FOR_REVIEW"


def test_optimization_manager_sources_decisions_and_shadow_execution(
        tmp_path: Path) -> None:
    db = Database(str(tmp_path / "station.sqlite3"))
    shadow = FakeShadowManager()
    events: list[tuple[str, dict]] = []
    manager = WorkstationOptimizationManager(
        db, shadow, broadcast=lambda event, data: events.append((event, data)))

    boss_item = manager.submit("boss", "Reduce startup time", "Split init work")
    agent_item = manager.submit(
        "agent", "Model routing TTFB feedback", "Use first-token latency")
    bottleneck_item = manager.submit(
        "bottleneck", "SQLite lock", "Queue concurrent writes")

    assert boss_item["status"] == "queued"
    assert agent_item["status"] == "candidate"
    assert bottleneck_item["status"] == "waiting_boss"
    approved = manager.decide(agent_item["id"], "approve", "Keep tests green")
    assert approved["status"] == "queued"
    assert approved["decision_reply"] == "Keep tests green"

    manager._process_once()
    manager._process_once()
    completed = manager.get_item(agent_item["id"])

    assert completed is not None
    assert completed["status"] == "completed"
    assert completed["result"]["diff_file"] == "changes.patch"
    assert shadow.submitted[0]["run_id"] == completed["shadow_run_id"]
    assert any(event == "workstation_optimization_completed" for event, _ in events)

    summary = manager.summary()
    assert summary["queue_count"] == 0
    assert summary["running_item"]["id"] == boss_item["id"]
    manager.stop_guardian()
    assert summary["waiting_boss_count"] == 1


def test_optimization_api_contract() -> None:
    class FakeManager:
        def summary(self) -> dict:
            return {
                "guardian_running": True, "queue_count": 1,
                "waiting_boss_count": 1, "running_item": None,
                "recent_completed": None,
            }

        def list_items(self, status: str = "", limit: int = 100) -> list[dict]:
            return [{
                "id": "opt-api", "source": "boss", "title": "API item",
                "description": "test", "priority": "normal",
                "status": "queued", "created_at": 1, "updated_at": 1,
                "decision_reply": "", "shadow_run_id": "", "result": {},
            }]

        def submit(self, source: str, title: str, description: str,
                   priority: str) -> dict:
            return {
                "id": "opt-api", "source": source, "title": title,
                "description": description, "priority": priority,
                "status": "queued", "created_at": 1, "updated_at": 1,
                "decision_reply": "", "shadow_run_id": "", "result": {},
            }

        def decide(self, item_id: str, decision: str, reply: str) -> dict:
            return {"id": item_id, "status": "rejected", "decision_reply": reply}

    class FakeController:
        workstation_optimizer = FakeManager()

    app = FastAPI()
    app.include_router(build_optimization_routes(FakeController()))
    client = TestClient(app)

    summary = client.get("/api/workstation-optimization/summary")
    items = client.get("/api/workstation-optimization/items")
    created = client.post("/api/workstation-optimization/items", json={
        "source": "boss", "title": "API item", "description": "test"})
    decided = client.post(
        "/api/workstation-optimization/items/opt-api/decision",
        json={"decision": "reject", "reply": "not now"})

    assert summary.status_code == 200
    assert summary.json()["queue_count"] == 1
    assert items.json()["items"][0]["id"] == "opt-api"
    assert created.status_code == 201
    assert created.json()["item"]["status"] == "queued"
    assert decided.json()["item"]["status"] == "rejected"


def test_secretary_detects_and_creates_optimization_requests() -> None:
    from lan_mesh.chat_handler import ChatHandler

    class FakeManager:
        def submit(self, source: str, title: str, description: str,
                   priority: str) -> dict:
            return {
                "id": "opt-chat", "source": source, "title": title,
                "description": description, "priority": priority,
                "status": "queued", "created_at": 1, "updated_at": 1,
            }

        def summary(self) -> dict:
            return {
                "guardian_running": True, "queue_count": 0,
                "waiting_boss_count": 0, "running_item": None,
                "recent_completed": None,
            }

    class FakeController:
        workstation_optimizer = FakeManager()

    handler = object.__new__(ChatHandler)
    handler.controller = FakeController()

    reply = ChatHandler._action_workstation_optimization(
        handler, "优化工作站：降低首屏加载时间")

    assert ChatHandler._detect_action(handler, "工作站优化状态") == (
        "workstation_optimization")
    assert "opt-chat" in reply
    assert "已排队" in reply
