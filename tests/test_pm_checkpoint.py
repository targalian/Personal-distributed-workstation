"""Verify durable PM boundaries, write-failure fencing and read-only restart evidence."""
import json
import sqlite3
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import pytest

from lan_mesh.execution_receipts import ExecutionReceipts
from lan_mesh.pm_checkpoint import PMCheckpoints, inspect_checkpoint, read_checkpoint
from lan_mesh.pm_inspection import inspect_pm_start
from lan_mesh.pm_state import PMState
from tests.test_pm_lifecycle import join_task, task
from tests.test_pm_lifecycle import pm as _pm_fixture

pm = _pm_fixture


def attach(pm, tmp_path):
    path = tmp_path / "starts.sqlite3"
    pm._checkpoint_store = PMCheckpoints(path)
    return path


def saved(pm):
    return read_checkpoint(pm._checkpoint_store.path, pm.pm_id, "task-life", "self")["snapshot"]


def state():
    st = PMState()
    st.task = {"task_id": "task", "input_data": {"private": "do not expose"}}
    st.plan = {"decomposition": [{"name": "A"}, {"name": "B"}]}
    return st


def test_store_rejects_other_process_stale_revision_and_identity(tmp_path):
    path = tmp_path / "pm.sqlite3"
    first, second = PMCheckpoints(path, "first"), PMCheckpoints(path, "second")
    snapshot = state().snapshot()
    assert first.save("pm", "task", "node", snapshot, 0) == 1
    for store, task_id, device, revision in [(second, "task", "node", 1), (first, "task", "node", 0),
                                             (first, "task", "other", 1)]:
        with pytest.raises(ValueError):
            store.save("pm", task_id, device, snapshot, revision)
    assert read_checkpoint(path, "pm", "task", "node")["revision"] == 1
    with pytest.raises(ValueError):
        read_checkpoint(path, "pm", "other", "node")
    with pytest.raises(ValueError):
        first.save("missing", "task", "node", snapshot, 1)


def test_corrupt_and_legacy_checkpoint_read_never_creates_or_mutates(tmp_path):
    path = tmp_path / "missing" / "pm.sqlite3"
    assert inspect_checkpoint(path, "pm", "task", "node") == {"state": "missing"}
    assert not path.parent.exists()
    ExecutionReceipts(path).claim({"subtask_id": "pm"})
    before = path.read_bytes()
    assert inspect_checkpoint(path, "pm", "task", "node")["state"] == "missing"
    assert path.read_bytes() == before
    store = PMCheckpoints(path)
    store.save("pm", "task", "node", state().snapshot(), 0)
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE pm_checkpoints SET snapshot='not json'")
    before = path.read_bytes()
    assert inspect_checkpoint(path, "pm", "task", "node")["state"] == "unavailable"
    assert path.read_bytes() == before


def test_serialization_or_sql_failure_keeps_last_snapshot(tmp_path):
    path = tmp_path / "pm.sqlite3"
    store = PMCheckpoints(path)
    snapshot = state().snapshot()
    store.save("pm", "task", "node", snapshot, 0)
    with pytest.raises(TypeError):
        store.save("pm", "task", "node", {**snapshot, "bad": object()}, 1)
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TRIGGER fail_write BEFORE INSERT ON pm_checkpoints BEGIN SELECT RAISE(ABORT, 'disk failed'); END")
    with pytest.raises(sqlite3.DatabaseError):
        store.save("pm", "task", "node", {**snapshot, "phase": "new"}, 1)
    assert read_checkpoint(path, "pm", "task", "node")["snapshot"] == snapshot


def test_snapshot_deep_copy_and_summary_do_not_expose_content(tmp_path):
    path = tmp_path / "pm.sqlite3"
    st = state()
    st.subtask_outputs["A"] = {"secret": "private output"}
    st.execution_attempts["B"] = {"phase": "active", "subtask_id": "execution-B"}
    st.phase = "monitoring"
    snapshot = st.snapshot()
    st.subtask_outputs["A"]["secret"] = "changed"
    assert snapshot["subtask_outputs"]["A"]["secret"] == "private output"
    PMCheckpoints(path).save("pm", "task", "node", snapshot, 0)
    summary = inspect_checkpoint(path, "pm", "task", "node")
    assert summary["completed"] == 1 and summary["uncertain"] == 1 and summary["unstarted"] == 0
    assert summary["resume_supported"] is False
    assert "private" not in json.dumps(summary) and "do not expose" not in json.dumps(summary)


def test_dependency_output_is_durable_before_next_execution_and_delivery(pm, tmp_path):
    attach(pm, tmp_path)
    observed = []
    def execute(subtask):
        snapshot = saved(pm)
        attempt = snapshot["execution_attempts"][subtask["name"]]
        assert attempt["phase"] == "active" and attempt["subtask_id"] == subtask["subtask_id"]
        assert attempt["request"] == subtask
        if subtask["name"] == "B":
            assert snapshot["subtask_outputs"]["A"] == {"summary": "A done"}
        observed.append(subtask["name"])
        return {"status": "completed", "output": {"summary": subtask["name"] + " done"}}
    pm.runtime.execute.side_effect = execute
    pm.start_task(task())
    join_task(pm)
    final = saved(pm)
    assert observed == ["A", "B"]
    assert final["phase"] == "completed"
    assert final["delivery"]["state"] == "acknowledged"
    assert final["delivery"]["payload"]["deliverable"] == "完整交付产物"
    assert final["execution_attempts"]["A"]["received_result"]["output"] == {"summary": "A done"}


def test_single_execution_identity_and_result_are_durable(pm, tmp_path):
    attach(pm, tmp_path)
    pm._planner.analyze_with_skill = lambda task: {"pattern": "single"}
    def execute(subtask):
        snapshot = saved(pm)
        assert snapshot["direct_execution"]["state"] == "active"
        assert snapshot["direct_execution"]["execution_id"] == subtask["subtask_id"]
        assert snapshot["direct_execution"]["request"] == subtask
        summary = inspect_checkpoint(pm._checkpoint_store.path, pm.pm_id, "task-life", "self")
        assert summary["uncertain"] == 1 and summary["unstarted"] == 0
        return {"status": "completed", "output": {"summary": "single done"}}
    pm.runtime.execute.side_effect = execute
    pm.start_task(task())
    join_task(pm)
    assert saved(pm)["direct_execution"]["result"]["summary"] == "single done"
    assert saved(pm)["delivery"]["state"] == "acknowledged"


@pytest.mark.parametrize("stage", ["starting", "planned", "attempt", "result", "delivery"])
def test_write_failure_stops_future_work_and_keeps_previous_evidence(pm, tmp_path, stage):
    attach(pm, tmp_path)
    real_save = pm._checkpoint_store.save
    def fail_at_boundary(pm_id, task_id, device_id, snapshot, revision):
        attempts = snapshot["execution_attempts"]
        match = (snapshot["phase"] == stage or (stage == "attempt" and bool(attempts))
                 or (stage == "result" and bool(snapshot["subtask_outputs"]))
                 or (stage == "delivery" and bool(snapshot["delivery"])))
        if match:
            raise OSError("disk full")
        return real_save(pm_id, task_id, device_id, snapshot, revision)
    pm._checkpoint_store.save = fail_at_boundary
    if stage == "starting":
        with pytest.raises(RuntimeError, match="检查点"):
            pm.start_task(task())
        assert pm._thread is None
    else:
        pm.start_task(task())
        join_task(pm)
    assert pm._checkpoint_error and pm._state.stop_event.is_set() and not pm.running
    assert not any(url.endswith('/deliver') for url, _ in pm.test_calls)
    expected = {"starting": 0, "planned": 0, "attempt": 0, "result": 1, "delivery": 2}
    assert pm.runtime.execute.call_count == expected[stage]


def test_remote_request_is_persisted_before_post(pm, tmp_path, monkeypatch):
    attach(pm, tmp_path)
    pm._state.task = task()
    pm._state.plan = {"decomposition": [{"name": "A"}]}
    calls = []
    def post(url, **kwargs):
        attempt = saved(pm)["execution_attempts"]["A"]
        assert attempt["request"] == kwargs["json"]
        calls.append(url)
        raise TimeoutError("response lost")
    monkeypatch.setattr("lan_mesh.pm_dispatcher.requests.post", post)
    pm._dispatcher.dispatch_subtask({"ip": "127.0.0.1", "api_port": 12345}, {"agent_id": "worker"}, task(), {"name": "A"})
    assert len(calls) == 1 and saved(pm)["execution_attempts"]["A"]["phase"] == "active"


def test_stop_during_execution_is_saved_and_late_result_does_not_overwrite(pm, tmp_path):
    attach(pm, tmp_path)
    entered, release = threading.Event(), threading.Event()
    def execute(subtask):
        entered.set()
        assert release.wait(3)
        return {"status": "completed", "output": {"summary": "late"}}
    pm.runtime.execute.side_effect = execute
    pm.start_task(task())
    try:
        assert entered.wait(2)
        pm.cancel()
        snapshot = saved(pm)
        assert snapshot["phase"] == "cancelled" and snapshot["stopped"]
        assert snapshot["execution_attempts"]["A"]["phase"] == "active"
    finally:
        release.set()
    join_task(pm)
    assert not saved(pm)["subtask_outputs"]
    assert pm.runtime.execute.call_count == 1


def test_delivery_ack_loss_retains_exact_payload_without_claiming_ack(pm, tmp_path, monkeypatch):
    attach(pm, tmp_path)
    pm._state.task = task()
    sent = []
    def post(url, **kwargs):
        if url.endswith('/deliver'):
            sent.append(kwargs["json"])
            assert saved(pm)["delivery"]["state"] == "sending"
            assert saved(pm)["delivery"]["payload"] == kwargs["json"]
            raise TimeoutError("ack lost after commit")
        return Mock(status_code=200)
    monkeypatch.setattr("lan_mesh.pm_agent.http_post", post)
    assert not pm.deliver_result("name", "description", "artifact", [])
    assert len(sent) == 1
    assert saved(pm)["delivery"]["state"] == "sending"


def test_process_crash_keeps_direct_execution_unknown_and_start_is_not_replayed(tmp_path, monkeypatch):
    path = tmp_path / "starts.sqlite3"
    script = """
import os, sys, time
from pathlib import Path
from unittest.mock import Mock
from lan_mesh.pm_agent import ProjectManagerAgent
from lan_mesh.pm_start import start_pm_once
import lan_mesh.pm_agent as module
module.http_post = Mock()
original = ProjectManagerAgent.__init__
def init(self, *args):
    original(self, *args)
    self._planner.load_skill = Mock()
    self._planner.refine_requirements = lambda t: t
    self._planner.analyze_with_skill = lambda t: {'pattern': 'single'}
ProjectManagerAgent.__init__ = init
runtime = Mock()
runtime.execute.side_effect = lambda task: os._exit(23)
task = {'task_id': 'task', 'pm_agent_id': 'pm', 'status': 'running', 'name': 'crash', 'input_data': {}}
start_pm_once(pm_id='pm', task_id='task', secretary_url='http://unused', task_data=task,
    runtime=runtime, device_id='node', device_name='node', receipt_path=Path(sys.argv[1]),
    current=lambda: None, assign=lambda pm: None, lookup=lambda task_id: task)
time.sleep(10)
"""
    result = subprocess.run([sys.executable, "-c", script, str(path)], capture_output=True, timeout=15)
    assert result.returncode == 23
    checkpoint = read_checkpoint(path, "pm", "task", "node")
    assert checkpoint["snapshot"]["direct_execution"]["state"] == "active"
    evidence = inspect_pm_start(path, "pm", "task", "node", {})
    assert evidence["state"] == "unknown" and evidence["checkpoint"]["uncertain"] == 1
    assert "snapshot" not in evidence["checkpoint"]
    from lan_mesh.pm_start import start_pm_once
    factory = Mock()
    monkeypatch.setattr("lan_mesh.pm_start.ProjectManagerAgent", factory)
    original = checkpoint["snapshot"]["task"]
    result = start_pm_once(pm_id="pm", task_id="task", secretary_url="http://unused", task_data=original,
                           runtime=Mock(), device_id="node", device_name="node", receipt_path=path,
                           current=lambda: None, assign=Mock(), lookup=lambda _: original)
    assert not result["ok"] and result["uncertain"]
    factory.assert_not_called()


def test_retry_keeps_old_execution_identity_and_error_before_new_attempt(pm, tmp_path):
    attach(pm, tmp_path)
    calls = []
    def execute(subtask):
        if not calls:
            calls.append(subtask["subtask_id"])
            return {"status": "failed", "error": "first attempt failed"}
        if len(calls) == 1:
            previous = saved(pm)["execution_history"][calls[0]]
            assert previous["phase"] == "done"
            assert previous["received_result"]["status"] == "failed"
            assert subtask["subtask_id"] != calls[0]
        calls.append(subtask["subtask_id"])
        return {"status": "completed", "output": {"summary": "done"}}
    pm.runtime.execute.side_effect = execute
    pm.start_task(task())
    join_task(pm)
    assert len(calls) == 3
    assert saved(pm)["retry_counts"]["A"] == 1


def test_clarification_is_saved_before_wait_and_response_is_persisted(pm, tmp_path):
    attach(pm, tmp_path)
    pm._state.task = task()
    requested = threading.Event()
    pm.report_status = lambda *args, **kwargs: requested.set()
    result = []
    thread = threading.Thread(target=lambda: result.append(pm.request_clarification("使用哪个方案？", ["A", "B"], 2)))
    thread.start()
    try:
        assert requested.wait(1)
        assert saved(pm)["clarification_request"]["question"] == "使用哪个方案？"
        pm.receive_input({"response": "方案A"})
    finally:
        thread.join(timeout=3)
    assert not thread.is_alive() and result[0]["response"] == "方案A"
    assert saved(pm)["clarification_response"]["response"] == "方案A"


def test_stopped_phase_cannot_regress_when_old_worker_reaches_monitoring(pm, tmp_path):
    attach(pm, tmp_path)
    pm._state.task = task()
    pm.cancel()
    assert not pm.checkpoint("monitoring")
    assert saved(pm)["phase"] == "cancelled"


@pytest.mark.parametrize("remote", [False, True])
def test_stop_between_claim_and_request_prevents_execution(pm, tmp_path, monkeypatch, remote):
    attach(pm, tmp_path)
    pm._state.task = task()
    begin = pm._dispatcher._begin_attempt
    def stop_after_claim(*args):
        attempt = begin(*args)
        pm.cancel()
        return attempt
    pm._dispatcher._begin_attempt = stop_after_claim
    post = Mock()
    monkeypatch.setattr("lan_mesh.pm_dispatcher.requests.post", post)
    station = {"ip": "127.0.0.1", "api_port": 12345} if remote else {}
    pm._dispatcher.dispatch_subtask(station, {"agent_id": "worker"}, task(), {"name": "A"})
    post.assert_not_called()
    pm.runtime.execute.assert_not_called()
    assert saved(pm)["phase"] == "cancelled"


def test_concurrent_attempt_snapshots_keep_every_identity(pm, tmp_path):
    attach(pm, tmp_path)
    pm._state.task = task()
    with ThreadPoolExecutor(max_workers=8) as pool:
        attempts = list(pool.map(lambda n: pm._dispatcher._begin_attempt(str(n), task(), str(n), {}), range(24)))
    snapshot = saved(pm)
    assert len(snapshot["execution_attempts"]) == 24
    assert {a["subtask_id"] for a in attempts} == {a["subtask_id"] for a in snapshot["execution_attempts"].values()}
