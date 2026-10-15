"""Verify resubmission preserves requirements and never duplicates a replacement or PM."""
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import pytest

from lan_mesh.database import Database
from lan_mesh.protocol import PMAgent, Task
from lan_mesh.task_resubmission import prepare_resubmission, resubmit_task, task_with_provenance
from lan_mesh.task_submission import dispatch_task
from tests.test_pm_start import client_for, instant_factory
from tests.test_pm_start import controller as _controller_fixture

controller = _controller_fixture


def source_task(controller, **changes):
    fields = dict(task_id="original", name="确认后的需求", description="原始需求全文",
                  status="failed", project_id="project-1",
                  input_data={"_brief": {"revision": 7, "task_id": "original", "snapshot": "frozen"},
                              "_priority": "high", "files": ["input.csv"]},
                  output_data={"error": "no host"}, subtasks=[{"status": "failed"}])
    fields.update(changes)
    task = Task(**fields)
    controller.db.save_task(task)
    return task


def install_executor(controller, monkeypatch):
    client = client_for(controller)
    created = []
    monkeypatch.setattr("lan_mesh.pm_start.ProjectManagerAgent", instant_factory(controller, client, "completed", created))
    return client, created


def test_api_preserves_frozen_inputs_and_returns_existing_after_response_loss(controller, monkeypatch):
    source = source_task(controller)
    client, created = install_executor(controller, monkeypatch)
    response = client.post("/api/tasks/original/resubmit", json={"name": "must not replace source"})
    assert response.status_code == 200
    first = response.json()
    replacement = first["task"]
    assert not first["duplicate"]
    assert replacement["task_id"] != source.task_id
    assert replacement["name"] == source.name
    assert replacement["description"] == source.description
    assert replacement["input_data"] == source.input_data
    assert replacement["project_id"] == source.project_id
    assert replacement["resubmission"] == {"source_task_id": source.task_id}
    assert client.get("/api/tasks/original").json()["resubmission"] == {"replacement_task_id": replacement["task_id"]}
    again = client.post("/api/tasks/original/resubmit").json()
    assert again["duplicate"] and again["task"]["task_id"] == replacement["task_id"]
    assert len(created) == 1
    assert controller.db.get_task(source.task_id).to_dict() == source.to_dict()
    assert len(client.get("/api/tasks").json()["tasks"]) == 2


def test_concurrent_api_requests_start_exactly_one_pm(controller, monkeypatch):
    source_task(controller)
    client, created = install_executor(controller, monkeypatch)
    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(lambda _: client.post("/api/tasks/original/resubmit"), range(12)))
    assert all(r.status_code == 200 for r in responses)
    assert len({r.json()["task"]["task_id"] for r in responses}) == 1
    assert sum(not r.json()["duplicate"] for r in responses) == 1
    assert len(created) == 1 and len(controller.db.list_pm_agents()) == 1


@pytest.mark.parametrize("changes", [
    {"status": "running"}, {"status": "pending"}, {"status": "paused"},
    {"status": "completed"}, {"status": "accepted"}, {"status": "rejected"},
    {"status": "cancelled"}, {"status": "interrupted"},
    {"output_data": {"_delivery": {"deliverable": "already saved"}}},
    {"output_data": {"_pm_control": {"state": "pending"}}},
    {"output_data": {"_pm_control": {"state": "unknown"}}},
    {"output_data": {"_pm_recovery": {"state": "unknown"}}},
    {"output_data": {"_pm_execution": {"state": "unknown"}}},
    {"output_data": {"_pm_start": {"state": "unknown"}}},
    {"pm_agent_id": "missing-pm"},
])
def test_unsafe_source_rejected_even_if_client_acknowledges(controller, changes):
    source_task(controller, **changes)
    result = client_for(controller).post("/api/tasks/original/resubmit", json={"acknowledge_side_effects": True})
    assert result.status_code == 409
    assert len(controller.db.list_tasks()) == 1
    assert controller.db._get_conn().execute("SELECT COUNT(*) FROM task_resubmissions").fetchone()[0] == 0


@pytest.mark.parametrize("pm_status", ["executing", "unknown", "completed"])
def test_failed_task_with_live_or_inconsistent_pm_rejected(controller, pm_status):
    source_task(controller, pm_agent_id="old-pm")
    controller.db.upsert_pm_agent(PMAgent(pm_id="old-pm", task_id="original", status=pm_status))
    assert client_for(controller).post("/api/tasks/original/resubmit", json={"acknowledge_side_effects": True}).status_code == 409


@pytest.mark.parametrize("stopped", [False, True])
def test_executed_source_requires_acknowledgement_and_new_task_has_no_old_runtime(controller, stopped):
    output = {"_pm_control": {"state": "confirmed"}, "_pm_recovery": {"state": "unknown"}} if stopped else {}
    source_task(controller, pm_agent_id="old-pm", status="cancelled" if stopped else "failed", output_data=output)
    controller.db.upsert_pm_agent(PMAgent(pm_id="old-pm", task_id="original", status="failed"))
    with pytest.raises(ValueError, match="在途工具"):
        prepare_resubmission(controller, "original")
    task, duplicate = prepare_resubmission(controller, "original", True)
    assert not duplicate
    assert task.status == "pending" and not task.pm_agent_id and not task.subtasks and not task.output_data


def test_prepared_replacement_survives_database_reopen_and_dispatches_once(controller, monkeypatch):
    source_task(controller)
    task, _ = prepare_resubmission(controller, "original")
    controller.db = Database(str(controller.db.path))
    _, created = install_executor(controller, monkeypatch)
    result = resubmit_task(controller, "original")
    assert result["duplicate"] and result["task"]["task_id"] == task.task_id
    assert result["task"]["status"] == "completed" and len(created) == 1


def test_process_exits_after_preparation_then_next_process_dispatches_same_task(controller, monkeypatch):
    source_task(controller)
    script = """
import sys
from types import SimpleNamespace
from lan_mesh.database import Database
from lan_mesh.task_resubmission import prepare_resubmission
Database.backup = lambda self: ''
db = Database(sys.argv[1])
task, duplicate = prepare_resubmission(SimpleNamespace(db=db), 'original')
print(task.task_id)
"""
    result = subprocess.run([sys.executable, "-c", script, str(controller.db.path)],
                            capture_output=True, text=True, timeout=15, check=True)
    replacement_id = result.stdout.strip().splitlines()[-1]
    _, created = install_executor(controller, monkeypatch)
    current = resubmit_task(controller, "original")
    assert current["duplicate"] and current["task"]["task_id"] == replacement_id
    assert len(created) == 1


def test_unknown_start_is_not_restarted_by_repeated_resubmission(controller):
    source_task(controller)
    controller._local_start_pm = Mock(side_effect=TimeoutError("response lost"))
    first = resubmit_task(controller, "original")
    assert first["task"]["output_data"]["_pm_start"]["state"] == "unknown"
    second = resubmit_task(controller, "original")
    assert second["duplicate"] and second["task"]["task_id"] == first["task"]["task_id"]
    controller._local_start_pm.assert_called_once()


def test_deleted_source_does_not_erase_existing_replacement_links(controller, monkeypatch):
    source_task(controller)
    _, created = install_executor(controller, monkeypatch)
    first = resubmit_task(controller, "original")
    controller.db.delete_task("original")
    second = resubmit_task(controller, "original")
    assert second["duplicate"] and second["task"]["resubmission"] == {"source_task_id": "original"}
    assert second["task"]["task_id"] == first["task"]["task_id"] and len(created) == 1


def test_budget_blocks_creation_and_later_queue_dispatch(controller, monkeypatch):
    source_task(controller)
    manager = Mock()
    controller.project_manager = manager
    manager.check_budget.return_value = False
    client, created = install_executor(controller, monkeypatch)
    assert client.post("/api/tasks/original/resubmit").status_code == 402
    assert len(controller.db.list_tasks()) == 1
    manager.check_budget.return_value = True
    replacement, _ = prepare_resubmission(controller, "original")
    manager.check_budget.return_value = False
    assert client.post("/api/tasks/original/resubmit").status_code == 402
    with pytest.raises(PermissionError):
        dispatch_task(controller, replacement)
    assert not created and not controller.db.get_task(replacement.task_id).pm_agent_id
    manager.check_budget.return_value = True
    assert client.post("/api/tasks/original/resubmit").status_code == 200
    manager.check_budget.return_value = False
    # An existing terminal result is readable even after the budget is exhausted.
    assert client.post("/api/tasks/original/resubmit").status_code == 200
    assert len(created) == 1


def test_late_source_delivery_blocks_pending_replacement_at_binding(controller, monkeypatch):
    source_task(controller, pm_agent_id="old-pm")
    controller.db.upsert_pm_agent(PMAgent(pm_id="old-pm", task_id="original", status="failed"))
    replacement, _ = prepare_resubmission(controller, "original", True)
    controller.db.record_task_delivery("original", "old-pm", {"deliverable": "late artifact"})
    _, created = install_executor(controller, monkeypatch)
    with pytest.raises(ValueError, match="已有交付"):
        dispatch_task(controller, replacement)
    assert not created and not controller.db.list_pm_agents(status="starting")


def test_transaction_rolls_back_mapping_when_task_write_fails(controller, monkeypatch):
    source_task(controller)
    original = controller.db._save_task_fields
    def fail_after_write(task):
        original(task)
        raise OSError("disk write failed")
    monkeypatch.setattr(controller.db, "_save_task_fields", fail_after_write)
    with pytest.raises(OSError):
        prepare_resubmission(controller, "original")
    assert len(controller.db.list_tasks()) == 1
    assert controller.db._get_conn().execute("SELECT COUNT(*) FROM task_resubmissions").fetchone()[0] == 0


def test_deleted_replacement_never_recreated_and_source_snapshot_survives(controller):
    source = source_task(controller)
    replacement, _ = prepare_resubmission(controller, "original")
    controller.db.delete_task(replacement.task_id)
    controller.db.delete_task(source.task_id)
    controller.db = Database(str(controller.db.path))
    result = client_for(controller).post("/api/tasks/original/resubmit")
    assert result.status_code == 409 and "已删除" in result.json()["detail"]
    record = controller.db._get_conn().execute("SELECT * FROM task_resubmissions").fetchone()
    assert json.loads(record["source_snapshot"]) == source.to_dict()
    assert not controller.db.list_tasks()


def test_retry_chain_has_both_links_and_original_request_never_creates_sibling(controller):
    source_task(controller)
    second, _ = prepare_resubmission(controller, "original")
    controller.db.update_task_status(second.task_id, "failed")
    third, _ = prepare_resubmission(controller, second.task_id)
    links = task_with_provenance(controller.db, second)["resubmission"]
    assert links == {"source_task_id": "original", "replacement_task_id": third.task_id}
    repeated, duplicate = prepare_resubmission(controller, "original")
    assert duplicate and repeated.task_id == second.task_id
    assert third.input_data == second.input_data


def test_schema_v4_upgrade_preserves_tasks_and_is_repeatable(controller):
    source = source_task(controller)
    conn = controller.db._get_conn()
    with conn:
        conn.execute("DROP TABLE task_resubmissions")
        conn.execute("UPDATE schema_meta SET value='4' WHERE key='version'")
    for _ in range(2):
        controller.db = Database(str(controller.db.path))
        assert controller.db._get_schema_version(controller.db._get_conn()) == 5
        assert controller.db.get_task(source.task_id).to_dict() == source.to_dict()
    prepare_resubmission(controller, source.task_id)


def test_api_requires_secretary_and_valid_acknowledgement(controller):
    client = client_for(controller)
    assert client.post("/api/tasks/missing/resubmit").status_code == 404
    source_task(controller)
    assert client.post("/api/tasks/original/resubmit", json={"acknowledge_side_effects": "false"}).status_code == 422
    controller.secretary_active = False
    assert client.post("/api/tasks/original/resubmit").status_code == 503
