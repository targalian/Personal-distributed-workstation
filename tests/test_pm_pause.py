"""Verify paused execution evidence is durable without scheduling, retry or delivery."""

import sqlite3
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import pytest

from lan_mesh.pm_checkpoint import PMCheckpoints, read_checkpoint
from lan_mesh.pm_control import control_pm, request_task_control
from lan_mesh.pm_inspection import inspect_pm_start
from lan_mesh.pm_pause import retain_paused_result
from lan_mesh.pm_resume import validate_snapshot
from tests.test_pm_checkpoint import attach, saved
from tests.test_pm_control import bind, request
from tests.test_pm_lifecycle import join_task, task
from tests.test_pm_lifecycle import pm as _pm_fixture
from tests.test_pm_start import controller as _controller_fixture

pm = _pm_fixture
controller = _controller_fixture


@pytest.mark.parametrize("kind", ["planning", "direct", "subtask", "aggregation"])
@pytest.mark.parametrize("action", ["pause", "cancel"])
def test_late_result_saved_only_while_paused_without_new_effects(pm, tmp_path, kind, action):
    attach(pm, tmp_path)
    entered, release = threading.Event(), threading.Event()

    def blocked(result):
        entered.set()
        assert release.wait(3)
        return result

    if kind == "planning":
        pm._planner.analyze_with_skill = lambda _: blocked({"pattern": "single"})
    elif kind == "direct":
        pm._planner.analyze_with_skill = lambda _: {"pattern": "single"}
        pm.runtime.execute.side_effect = lambda _: blocked({"status": "completed", "output": {"summary": "原输出"}})
    elif kind == "subtask":
        pm.runtime.execute.side_effect = lambda _: blocked({"status": "completed", "output": {"summary": "原输出"}})
    else:
        pm.runtime._call_llm_with_routing.side_effect = lambda *_: blocked({"content": "原聚合"})
    pm.start_task(task())
    try:
        assert entered.wait(2)
        getattr(pm, action)()
        before = saved(pm)
        pause_point = before.get("pause", {}).get("source_phase")
        calls = pm.runtime.execute.call_count
    finally:
        release.set()
    join_task(pm)
    after = saved(pm)
    assert after["stopped"] and after["phase"] == ("paused" if action == "pause" else "cancelled")
    assert pm.runtime.execute.call_count == calls
    assert not any(url.endswith("/deliver") for url, _ in pm.test_calls)
    assert after["subtask_outputs"] == before["subtask_outputs"]
    if action == "cancel":
        assert after == before
    else:
        assert (
            pause_point
            == {
                "planning": "planning",
                "direct": "executing_single",
                "subtask": "dispatching",
                "aggregation": "aggregating",
            }[kind]
        )
        if kind == "planning":
            assert after["pause"]["returned"]["planning"] == {"pattern": "single"}
            assert not after["plan"]
        elif kind == "direct":
            assert after["direct_execution"]["state"] == "returned"
            assert after["direct_execution"]["result"]["summary"] == "原输出"
        elif kind == "subtask":
            assert after["execution_attempts"]["A"]["received_result"]["output"] == {"summary": "原输出"}
            assert after["execution_attempts"]["A"]["phase"] == "processing"
            assert "B" not in after["execution_attempts"]
        else:
            assert after["pause"]["returned"]["aggregation"] == {"content": "原聚合"}
            assert "_aggregated" not in after["subtask_outputs"]
        with pytest.raises(ValueError, match="停止"):
            validate_snapshot(after, pm.pm_id, "task-life")


def prepared(pm, tmp_path):
    attach(pm, tmp_path)
    pm._state.task = task()
    pm._state.plan = {"pattern": "orchestrator", "decomposition": [{"name": "A"}]}
    pm._state.phase = "monitoring"
    pm._dispatcher._begin_attempt("A", task(), "worker", {"device_id": "remote"})
    pm.pause()
    attempt = pm._state.execution_attempts["A"]
    return {
        "subtask_id": attempt["subtask_id"],
        "parent_task_id": "task-life",
        "pm_id": "pm-life",
        "reporter_id": "worker",
        "task_name": "A",
        "status": "completed",
        "output": {"summary": "保留原结果"},
    }


@pytest.mark.parametrize("field", ["subtask_id", "parent_task_id", "pm_id", "reporter_id", "task_name"])
def test_wrong_paused_result_identity_never_changes_checkpoint(pm, tmp_path, field):
    report = prepared(pm, tmp_path)
    report[field] = "foreign"
    before = saved(pm)
    pm.receive_progress_report(report)
    assert saved(pm) == before
    assert not pm._state.subtask_outputs


def test_duplicate_and_conflicting_paused_results_do_not_overwrite(pm, tmp_path):
    report = prepared(pm, tmp_path)
    verify = Mock(side_effect=AssertionError("paused must not verify or dispatch"))
    pm._monitor._verify_output_quality = verify
    pm._dispatcher.try_dispatch_pending = verify
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(lambda _: pm.receive_progress_report(report), range(6)))
    before = read_checkpoint(pm._checkpoint_store.path, pm.pm_id, "task-life", "self")
    assert before["snapshot"]["execution_attempts"]["A"]["received_result"] == report
    pm.receive_progress_report({**report, "status": "failed", "message": "conflicting"})
    pm.receive_progress_report(report)
    assert read_checkpoint(pm._checkpoint_store.path, pm.pm_id, "task-life", "self") == before
    verify.assert_not_called()


@pytest.mark.parametrize("status", ["failed", "unknown"])
def test_paused_failure_is_evidence_and_does_not_trigger_retry(pm, tmp_path, status):
    report = prepared(pm, tmp_path)
    report.update(status=status, message="在途返回异常")
    pm._monitor.handle_subagent_failure = Mock()
    pm.receive_progress_report(report)
    assert saved(pm)["execution_attempts"]["A"]["received_result"]["status"] == status
    pm._monitor.handle_subagent_failure.assert_not_called()
    pm.runtime.execute.assert_not_called()


def test_pause_then_cancel_never_accepts_late_result(pm, tmp_path):
    report = prepared(pm, tmp_path)
    pm.cancel()
    before = saved(pm)
    pm.receive_progress_report(report)
    assert saved(pm) == before and before["stop_reason"] == "cancelled"


def test_shutdown_and_repeated_pause_preserve_original_continuation(pm, tmp_path):
    prepared(pm, tmp_path)
    original = saved(pm)["pause"]
    pm.pause()
    pm.stop()
    assert saved(pm)["pause"] == original
    assert saved(pm)["stop_reason"] == "paused"


def test_late_result_disk_failure_preserves_last_snapshot(pm, tmp_path):
    report = prepared(pm, tmp_path)
    before = saved(pm)
    with sqlite3.connect(pm._checkpoint_store.path) as conn:
        conn.execute(
            "CREATE TRIGGER fail_write BEFORE INSERT ON pm_checkpoints BEGIN SELECT RAISE(ABORT,'disk full'); END"
        )
    assert not retain_paused_result(pm, "subtask", report)
    assert pm._checkpoint_error and saved(pm) == before
    assert not retain_paused_result(pm, "subtask", report)
    assert not pm._state.subtask_outputs


def test_stop_confirmation_requires_checkpoint_and_durable_receipt(pm, controller, tmp_path):
    bind(controller)
    controller._local_pm_agent = pm
    pm._state.task = task()
    path = controller.db.path.with_name(controller.db.path.stem + "-pm-starts.sqlite3")
    pm._checkpoint_store = PMCheckpoints(path)
    pm.checkpoint("planning")
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TRIGGER fail_write BEFORE INSERT ON pm_checkpoints BEGIN SELECT RAISE(ABORT,'disk full'); END"
        )
    result = request_task_control(controller, "task-life", "pause")
    assert result["ok"] and not result["confirmed"]
    assert pm._state.stop_event.is_set() and not pm.get_status()["control_receipt"]
    assert not saved(pm)["stopped"]
    assert controller.db.get_task("task-life").output_data["_pm_control"]["state"] != "confirmed"


def test_lost_stop_ack_is_recovered_from_disk_after_instance_disappears(pm, controller):
    bind(controller)
    pm._state.task = task()
    path = controller.db.path.with_name(controller.db.path.stem + "-pm-starts.sqlite3")
    pm._checkpoint_store = PMCheckpoints(path)
    pm.checkpoint("planning")
    controller._local_pm_agent = pm

    def lose_ack(payload):
        assert control_pm(lambda: pm, "self", payload)["ok"]
        controller._local_pm_agent = None
        return {"ok": False, "message": "response lost"}

    controller._local_control_pm = lose_ack
    result = request_task_control(controller, "task-life", "pause")
    assert result["confirmed"]
    before = path.read_bytes()
    evidence = inspect_pm_start(path, "pm-life", "task-life", "self", {})
    assert evidence["control_receipt"]["request_id"] == result["control"]["request_id"]
    assert path.read_bytes() == before
    assert evidence["state"] != "running"
    assert saved(pm)["pause"]["source_phase"] == "planning"


def test_duplicate_control_is_read_only_and_preserves_original_phase(pm, tmp_path):
    attach(pm, tmp_path)
    pm._state.task = task()
    pm.checkpoint("planning")
    first = control_pm(lambda: pm, "self", request("pause"))
    before = pm._checkpoint_store.path.read_bytes()
    assert control_pm(lambda: pm, "self", request("pause")) == first
    assert pm._checkpoint_store.path.read_bytes() == before
    assert saved(pm)["control_receipt"] == first


@pytest.mark.parametrize("mode", ["single", "orchestrator"])
def test_real_process_exit_preserves_paused_result_and_stop_receipt(tmp_path, mode):
    path = tmp_path / "paused.sqlite3"
    script = """
import os,sys,time
from pathlib import Path
from unittest.mock import Mock
from lan_mesh.pm_agent import ProjectManagerAgent
from lan_mesh.pm_checkpoint import PMCheckpoints
from lan_mesh.pm_control import control_pm
from lan_mesh.pm_lease import acquire_pm_lease
import lan_mesh.pm_agent as module
module.http_post=Mock()
pm=ProjectManagerAgent('pm',Mock(),'http://unused','node')
pm._checkpoint_store=PMCheckpoints(Path(sys.argv[1]),lease=acquire_pm_lease(Path(sys.argv[1])))
pm._planner.load_skill=Mock();pm._planner.refine_requirements=lambda task:task
pm._planner.analyze_with_skill=lambda task:{'pattern':sys.argv[2],'decomposition':[{'name':'A'}]}
pm._dispatcher.get_available_stations=lambda:[]
pm._distribute_artifacts=Mock()
original=pm.checkpoint
def checkpoint(phase=''):
    ok=original(phase)
    st=pm._state
    if ok and st.stop_reason=='paused' and (st.direct_execution.get('state')=='returned' or st.execution_attempts.get('A',{}).get('received_result')):
        os._exit(23)
    return ok
pm.checkpoint=checkpoint;pm._state.persist=checkpoint
def execute(task):
    assert control_pm(lambda:pm,'node',{'pm_id':'pm','task_id':'task','device_id':'node','request_id':'pause-request','action':'pause'})['ok']
    return {'status':'completed','output':{'summary':'退出前的原工具结果'}}
pm.runtime.execute.side_effect=execute
pm.start_task({'task_id':'task','name':'A','description':'原要求','input_data':{}})
time.sleep(10)
"""
    proc = subprocess.run([sys.executable, "-c", script, str(path), mode], capture_output=True, timeout=15)
    assert proc.returncode == 23, proc.stderr.decode()
    record = read_checkpoint(path, "pm", "task", "node")
    snapshot = record["snapshot"]
    assert snapshot["stopped"] and snapshot["phase"] == "paused" and not snapshot["delivery"]
    assert not snapshot["subtask_outputs"]
    if mode == "single":
        assert snapshot["direct_execution"]["result"]["summary"] == "退出前的原工具结果"
    else:
        assert snapshot["execution_attempts"]["A"]["received_result"]["output"]["summary"] == "退出前的原工具结果"
    assert inspect_pm_start(path, "pm", "task", "node", {})["control_receipt"]["request_id"] == "pause-request"
    with pytest.raises(ValueError, match="停止"):
        validate_snapshot(snapshot, "pm", "task")


@pytest.mark.parametrize("field", ["pm_id", "task_id", "device_id", "action", "request_id", "scope", "ok"])
def test_invalid_persisted_stop_receipt_is_not_exposed(pm, tmp_path, field):
    attach(pm, tmp_path)
    pm._state.task = task()
    pm.checkpoint("planning")
    assert control_pm(lambda: pm, "self", request("pause"))["ok"]
    pm._state.control_receipt[field] = False if field == "ok" else ""
    assert pm.checkpoint()
    before = pm._checkpoint_store.path.read_bytes()
    evidence = inspect_pm_start(pm._checkpoint_store.path, "pm-life", "task-life", "self", {})
    assert not evidence.get("control_receipt")
    assert pm._checkpoint_store.path.read_bytes() == before


def test_corrupt_checkpoint_keeps_start_receipt_query_available_without_stop_ack(pm, tmp_path):
    from lan_mesh.execution_receipts import ExecutionReceipts

    attach(pm, tmp_path)
    pm._state.task = task()
    pm.checkpoint("planning")
    path = pm._checkpoint_store.path
    ExecutionReceipts(path).claim({"subtask_id": "pm-life", "pm_id": "pm-life", "parent_task_id": "task-life"})
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE pm_checkpoints SET snapshot='broken'")
    before = path.read_bytes()
    evidence = inspect_pm_start(path, "pm-life", "task-life", "self", {})
    assert evidence["startup_recorded"] and evidence["checkpoint"]["state"] == "unavailable"
    assert not evidence.get("control_receipt") and path.read_bytes() == before


def test_restoring_paused_state_does_not_clear_stop_signal(pm, tmp_path):
    from lan_mesh.pm_agent import ProjectManagerAgent

    report = prepared(pm, tmp_path)
    pm.receive_progress_report(report)
    restored = ProjectManagerAgent("pm-life", Mock(), "http://unused", "self")
    restored._state.restore(saved(pm))
    assert restored._state.stop_event.is_set() and not restored.execution_allowed()
    assert restored._state.pause == pm._state.pause
    restored.runtime.execute.assert_not_called()


@pytest.mark.parametrize("mode", ["station", "worker"])
def test_real_http_stop_confirmation_after_node_loses_instance(controller, tmp_path, mode):
    from types import SimpleNamespace

    import requests
    from fastapi import FastAPI

    from lan_mesh.database import Database
    from lan_mesh.pm_agent import ProjectManagerAgent
    from lan_mesh.station_controller import StationController
    from lan_mesh.station_routes_tasks import build_task_routes
    from lan_mesh.station_routes_worker import build_worker_routes
    from lan_mesh.worker import WorkerAgent
    from lan_mesh.worker_routes_pm import build_pm_routes
    from tests.test_pm_start import live_server

    pm = ProjectManagerAgent("pm-life", Mock(), "http://unused", "remote")
    pm._state.task = task()
    pm.running = True
    node_app = FastAPI()
    if mode == "station":
        node = StationController.__new__(StationController)
        node.db = SimpleNamespace(path=tmp_path / "remote.sqlite3")
        node.state = SimpleNamespace(device_id="remote", device_name="remote")
        node.discovery = Mock()
        node._local_pm_agent = pm
        path = tmp_path / "remote-pm-starts.sqlite3"

        def lose_instance(payload):
            receipt = control_pm(lambda: node._local_pm_agent, "remote", payload)
            if receipt["ok"]:
                node._local_pm_agent = None
            return {"ok": False, "message": "模拟确认丢失与实例退出"}

        node._local_control_pm = lose_instance
        node_app.include_router(build_worker_routes(node))
    else:
        node = WorkerAgent.__new__(WorkerAgent)
        node.data_dir = tmp_path
        node.state = SimpleNamespace(device_id="remote", device_name="remote", pm_agent=pm)
        path = tmp_path / "worker-pm-starts.sqlite3"

        def lose_instance(payload):
            receipt = control_pm(lambda: node.state.pm_agent, "remote", payload)
            if receipt["ok"]:
                node.state.pm_agent = None
            return {"ok": False, "message": "模拟确认丢失与实例退出"}

        node.control_pm = lose_instance
        node_app.include_router(build_pm_routes(node))
    pm._checkpoint_store = PMCheckpoints(path)
    assert pm.checkpoint("monitoring")
    attempt = pm._dispatcher._begin_attempt("A", task(), "child", {})
    secretary = FastAPI()
    secretary.include_router(build_task_routes(controller))
    with live_server(node_app) as port, live_server(secretary) as secretary_port:
        bind(controller, device="remote", port=port)
        response = requests.post(f"http://127.0.0.1:{secretary_port}/api/tasks/task-life/pause", timeout=8)
        assert response.status_code == 200 and response.json()["confirmed"], response.text
        controller.db = Database(str(controller.db.path))
        assert controller.db.get_task("task-life").output_data["_pm_control"]["state"] == "confirmed"
        evidence = requests.get(
            f"http://127.0.0.1:{port}/role/pm-starts/pm-life", params={"task_id": "task-life"}, timeout=5
        ).json()
        assert evidence["control_receipt"]["request_id"] == response.json()["control"]["request_id"]
        assert evidence["control_receipt"]["action"] == "pause"
        # A tool thread holding the old PM may still return after its node loses the active reference.
        pm.receive_subtask_result(
            "A", "completed", {"summary": "原工具晚到结果"}, "child", execution_id=attempt["subtask_id"]
        )
        snapshot = read_checkpoint(path, "pm-life", "task-life", "remote")["snapshot"]
        assert snapshot["execution_attempts"]["A"]["received_result"]["output"]["summary"] == "原工具晚到结果"
        assert not snapshot["subtask_outputs"] and not snapshot["delivery"]
        before = path.read_bytes()
        repeated = requests.post(f"http://127.0.0.1:{secretary_port}/api/tasks/task-life/pause", timeout=8)
        assert repeated.status_code == 200 and repeated.json()["confirmed"] and path.read_bytes() == before
        # A cancellation must not reuse a pause receipt when the old PM has exited.
        cancelled = requests.post(f"http://127.0.0.1:{secretary_port}/api/tasks/task-life/cancel", timeout=8)
        assert cancelled.status_code == 202 and not cancelled.json()["confirmed"]
        assert controller.db.get_task("task-life").status == "cancelled"
    pm.stop()


@pytest.mark.parametrize("kind", ["mismatched_start", "mismatched_current", "scalar_snapshot", "invalid_receipt"])
def test_inconsistent_inspection_never_exposes_stop_confirmation(pm, tmp_path, kind):
    from lan_mesh.execution_receipts import ExecutionReceipts

    attach(pm, tmp_path)
    pm._state.task = task()
    pm.checkpoint("planning")
    assert control_pm(lambda: pm, "self", request("pause"))["ok"]
    path = pm._checkpoint_store.path
    current = {}
    if kind == "mismatched_start":
        ExecutionReceipts(path).claim({"subtask_id": "pm-life", "pm_id": "pm-life", "parent_task_id": "another-task"})
    elif kind == "mismatched_current":
        current = {"pm_id": "pm-life", "task_id": "another-task", "device_id": "self", "running": True}
    elif kind == "scalar_snapshot":
        with sqlite3.connect(path) as conn:
            conn.execute("UPDATE pm_checkpoints SET snapshot='1'")
    else:
        pm._state.control_receipt = []
        assert pm.checkpoint()
    before = path.read_bytes()
    evidence = inspect_pm_start(path, "pm-life", "task-life", "self", current)
    assert not evidence.get("control_receipt") and path.read_bytes() == before
