"""Bounded PM replanning, dependency rewiring and failure fallback regressions."""

import json
import threading
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from lan_mesh.pm_monitor import PMMonitor
from lan_mesh.pm_dispatcher import PMDispatcher
from lan_mesh.pm_planner import PMPlanner
from lan_mesh.pm_state import PMState


def sub(name, deps=()):
    return dict(name=name, skill="code_generation", description=f"Implement {name}",
                depends_on=list(deps))


@pytest.fixture
def setup_pm(monkeypatch):
    state = PMState(task={"task_id": "task", "description": "Keep scope"})
    state.plan = {"decomposition": [sub("A"), sub("B", ["A"]), sub("C", ["B"])]}
    state.retry_counts["B"] = state.max_retries
    state.task_station["B"] = {"device_id": "station", "ip": "127.0.0.1", "api_port": 1}
    state.task_agent["B"] = {"agent_id": "old"}
    state.subagents["old"] = {"current_task": "B", "status": "failed"}
    state.pending_subtasks["C"] = {
        "sub": deepcopy(state.plan["decomposition"][2]), "station": {}, "agent_info": {}}
    runtime = SimpleNamespace(_call_llm_with_routing=Mock(return_value={
        "content": json.dumps([sub("B1"), sub("B2")])}))
    agent = SimpleNamespace(running=True, sync_subtasks=Mock(), report_status=Mock(),
                            report_progress=Mock())
    dispatcher = Mock()
    dispatcher.get_available_stations.return_value = []
    dispatcher._create_subagent_on_station.side_effect = lambda *a, **kw: {
        "agent_id": kw["preferred_agent_id"]}
    monitor = PMMonitor("pm", runtime, "http://unused", state, agent, dispatcher)
    monitor._report_escalation = Mock()
    monkeypatch.setattr("lan_mesh.runtime_trace.trace_task_event", Mock())
    return state, runtime, agent, dispatcher, monitor


def test_splice_rewires_chain_and_dispatches_only_ready(setup_pm):
    st, runtime, agent, dispatcher, monitor = setup_pm
    st.subtask_outputs["A"] = "completed evidence"
    monitor.handle_subagent_failure("B", "step too large")
    tasks = st.plan["decomposition"]
    assert [s["name"] for s in tasks] == ["A", "B1", "B2", "C"]
    assert [s["depends_on"] for s in tasks] == [[], ["A"], ["B1"], ["B2"]]
    assert st.pending_subtasks["C"]["sub"]["depends_on"] == ["B2"]
    assert tasks[1]["status"] == tasks[2]["status"] == "pending"
    assert st.retry_counts["B1"] == st.retry_counts["B2"] == 0
    assert st.replan_counts == {"B": 1}
    assert "old" not in st.subagents
    assert dispatcher.dispatch_subtask.call_args.args[3]["name"] == "B1"
    assert dispatcher.dispatch_subtask.call_count == 1
    assert st.subtask_outputs == {"A": "completed evidence"}
    monitor.receive_progress_report(dict(task_name="B1", status="completed", output="ok"))
    assert dispatcher.dispatch_subtask.call_args.args[3]["name"] == "B2"
    from lan_mesh import runtime_trace
    assert any(c.args[1] == "subtask_replan" for c in runtime_trace.trace_task_event.call_args_list)


def test_no_dispatch_until_original_dependencies_complete(setup_pm):
    st, _, _, dispatcher, monitor = setup_pm
    monitor.handle_subagent_failure("B", "boom")
    dispatcher.dispatch_subtask.assert_not_called()
    assert {"B1", "B2", "C"} == set(st.pending_subtasks)
    monitor.receive_progress_report(dict(task_name="A", status="completed", output="ok"))
    assert dispatcher.dispatch_subtask.call_args.args[3]["name"] == "B1"


def test_same_station_retries_happen_before_replanning(setup_pm):
    st, runtime, _, dispatcher, monitor = setup_pm
    st.retry_counts.clear()
    monitor.handle_subagent_failure("B", "boom")
    runtime._call_llm_with_routing.assert_not_called()
    assert st.replan_counts == {}
    dispatcher.get_available_stations.assert_not_called()
    dispatcher.execute_subtask_locally.assert_not_called()
    assert st.retry_counts["B"] == 1
    assert dispatcher.dispatch_subtask.call_args.args[3]["name"] == "B"


def test_other_station_retry_happens_before_replanning(setup_pm):
    st, runtime, agent, dispatcher, monitor = setup_pm
    agent._planner = SimpleNamespace(replan_failed_subtask=Mock())
    original = deepcopy(st.plan)
    other = {"device_id": "other", "ip": "127.0.0.2", "api_port": 2}
    dispatcher.get_available_stations.return_value = [st.task_station["B"], other]
    monitor.handle_subagent_failure("B", "boom")
    agent._planner.replan_failed_subtask.assert_not_called()
    runtime._call_llm_with_routing.assert_not_called()
    assert st.replan_counts == {}
    assert st.plan == original
    assert st.retry_counts["B"] == st.max_retries + 1
    assert st.task_station["B"] == other
    dispatcher.dispatch_subtask.assert_called_once_with(
        other, st.task_agent["B"], st.task, st.plan["decomposition"][1], plan=st.plan)
    dispatcher.execute_subtask_locally.assert_not_called()


@pytest.mark.parametrize("valid", [True, False])
def test_replanning_precedes_local_takeover_when_no_other_station(setup_pm, valid):
    st, runtime, _, dispatcher, monitor = setup_pm
    st.subtask_outputs["A"] = "completed evidence"
    dispatcher.get_available_stations.return_value = [st.task_station["B"]]
    events = []

    def respond(*args):
        dispatcher.get_available_stations.assert_called_once_with()
        dispatcher.execute_subtask_locally.assert_not_called()
        events.append("replan")
        return {"content": json.dumps([sub("replacement")]) if valid else "[]"}

    runtime._call_llm_with_routing.side_effect = respond
    dispatcher.execute_subtask_locally.side_effect = lambda *args: events.append("local")
    monitor.handle_subagent_failure("B", "boom")
    runtime._call_llm_with_routing.assert_called_once()
    assert st.replan_counts == {"B": 1}
    assert events == (["replan"] if valid else ["replan", "local"])
    if valid:
        assert dispatcher.dispatch_subtask.call_args.args[3]["name"] == "replacement"
        monitor._report_escalation.assert_not_called()
    else:
        dispatcher.dispatch_subtask.assert_not_called()
        assert dispatcher.execute_subtask_locally.call_args.args[1]["name"] == "B"
        monitor._report_escalation.assert_called_once()


def test_lineage_cannot_replan_again_and_records_error(setup_pm):
    st, runtime, _, _, monitor = setup_pm
    monitor.handle_subagent_failure("B", "first")
    st.retry_counts["B1"] = st.max_retries
    monitor.handle_subagent_failure("B1", "specific new failure")
    assert runtime._call_llm_with_routing.call_count == 1
    assert st.replan_counts == {"B": 1}
    assert "specific new failure" in st.plan["decomposition"][1]["description"]


def test_five_total_attempts_then_sixth_falls_back(setup_pm):
    st, runtime, _, dispatcher, monitor = setup_pm
    st.plan = {"decomposition": [sub(f"job{i}") for i in range(6)]}
    runtime._call_llm_with_routing.side_effect = [
        {"content": json.dumps([sub(f"replacement{i}")])} for i in range(5)]
    for i in range(6):
        st.retry_counts[f"job{i}"] = st.max_retries
        monitor.handle_subagent_failure(f"job{i}", "boom")
    assert runtime._call_llm_with_routing.call_count == 5
    assert sum(st.replan_counts.values()) == 5
    assert st.plan["decomposition"][-1]["name"] == "job5"
    assert "boom" in st.plan["decomposition"][-1]["description"]
    assert dispatcher.execute_subtask_locally.call_args.args[1]["name"] == "job5"


@pytest.mark.parametrize("content", ["not JSON", "[]", "{}", "null",
    json.dumps([sub(str(i)) for i in range(4)]),
    json.dumps([sub("B1"), sub("B1")]), json.dumps([sub("A")]),
    json.dumps([sub("B")]), json.dumps([{"name": "x"}]),
    json.dumps([sub("x") | {"depends_on": "A"}]),
    json.dumps([sub("x") | {"skill": " "}])])
def test_invalid_candidates_leave_plan_unchanged_and_fall_back(setup_pm, content):
    st, runtime, _, dispatcher, monitor = setup_pm
    original = deepcopy(st.plan)
    runtime._call_llm_with_routing.return_value = {"content": content}
    monitor.handle_subagent_failure("B", "boom")
    assert st.plan == original
    assert st.replan_counts == {"B": 1}
    assert dispatcher.execute_subtask_locally.call_args.args[1]["name"] == "B"


def test_llm_exception_falls_back_without_changing_plan(setup_pm):
    st, runtime, _, dispatcher, monitor = setup_pm
    original = deepcopy(st.plan)
    runtime._call_llm_with_routing.side_effect = RuntimeError("offline")
    monitor.handle_subagent_failure("B", "boom")
    assert st.plan == original
    dispatcher.execute_subtask_locally.assert_called_once()
    from lan_mesh import runtime_trace
    assert any(c.args[1] == "subtask_replan" for c in runtime_trace.trace_task_event.call_args_list)


def test_completed_outputs_are_truncated_and_planner_does_not_mutate(setup_pm):
    st, runtime, agent, _, _ = setup_pm
    planner = PMPlanner("pm", runtime, st, agent)
    original = deepcopy(st.plan)
    planner.replan_failed_subtask(st.task, st.plan, "B", "boom", {"A": "x" * 700})
    prompt = runtime._call_llm_with_routing.call_args.args[0]
    context = json.loads(prompt.split("\n", 1)[1])
    assert context["completed_outputs"] == {"A": "x" * 500}
    assert context["failed_subtask"]["name"] == "B"
    assert context["error"] == "boom"
    assert st.plan == original


@pytest.mark.parametrize("change", ["cancel", "complete", "edit"])
def test_llm_runs_unlocked_and_stale_response_does_not_overwrite(setup_pm, change):
    st, runtime, agent, dispatcher, monitor = setup_pm
    original = deepcopy(st.plan)

    def respond(*args):
        acquired = threading.Event()

        def update():
            with st.lock:
                if change == "cancel":
                    agent.running = False
                elif change == "complete":
                    st.subtask_outputs["B"] = "late successful result"
                else:
                    st.plan["decomposition"][0]["description"] = "new scope"
                acquired.set()

        thread = threading.Thread(target=update)
        thread.start()
        assert acquired.wait(2), "LLM must run outside the shared state lock"
        thread.join()
        return {"content": json.dumps([sub("replacement")])}

    runtime._call_llm_with_routing.side_effect = respond
    monitor.handle_subagent_failure("B", "boom")
    if change != "edit":
        assert st.plan == original
    assert [s["name"] for s in st.plan["decomposition"]] == ["A", "B", "C"]
    dispatcher.dispatch_subtask.assert_not_called()
    dispatcher.execute_subtask_locally.assert_not_called()


def test_snapshot_roundtrip_and_legacy_default():
    st = PMState(replan_counts={"B": 1, "D": 1})
    assert PMState.from_snapshot(st.to_snapshot()).replan_counts == st.replan_counts
    assert PMState.from_snapshot({}).replan_counts == {}


def test_local_replacement_chain_uses_completed_dependency_outputs(setup_pm):
    st, _, agent, dispatcher, monitor = setup_pm
    st.task_station.clear()
    st.subtask_outputs["A"] = "evidence"
    monitor.handle_subagent_failure("B", "boom")
    assert dispatcher.execute_subtask_locally.call_args.args[1]["name"] == "B1"
    assert dispatcher.execute_subtask_locally.call_args.args[0]["input_data"][
        "_dependency_outputs"] == {"A": "evidence"}
    monitor.receive_progress_report(dict(task_name="B1", status="completed", output="ok"))
    assert dispatcher.execute_subtask_locally.call_args.args[1]["name"] == "B2"


def test_replacement_chain_integrates_with_real_pending_dispatcher(setup_pm):
    st, runtime, agent, _, monitor = setup_pm
    st.task_station.clear()
    st.subtask_outputs["A"] = "done"
    dispatcher = PMDispatcher("pm", runtime, "http://unused", "local", st, agent)
    monitor._dispatcher = dispatcher
    observed = []

    def finish(task, item):
        observed.append(item["name"])
        assert all(dep in st.subtask_outputs for dep in item["depends_on"])
        monitor.receive_progress_report(dict(task_name=item["name"], status="completed", output="ok"))

    dispatcher.execute_subtask_locally = finish
    dispatcher.dispatch_subtask = lambda station, info, task, item, **kw: finish(task, item)
    monitor.handle_subagent_failure("B", "boom")
    assert observed == ["B1", "B2", "C"]
    assert st.pending_subtasks == {}
    assert st.subtask_outputs == {"A": "done", "B1": "ok", "B2": "ok", "C": "ok"}


def test_duplicate_failure_while_llm_running_does_not_dispatch_old_task(setup_pm):
    st, runtime, _, dispatcher, monitor = setup_pm

    def respond(*args):
        monitor.handle_subagent_failure("B", "duplicate")
        dispatcher.execute_subtask_locally.assert_not_called()
        return {"content": json.dumps([sub("replacement")])}

    runtime._call_llm_with_routing.side_effect = respond
    monitor.handle_subagent_failure("B", "boom")
    assert runtime._call_llm_with_routing.call_count == 1
    assert st.replan_counts == {"B": 1}
