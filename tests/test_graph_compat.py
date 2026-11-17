"""Behavioral coverage for plain plans, graph serialization and explicit executors."""

import subprocess
import sys
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from lan_mesh import agent_runtime, graph_compat, runtime_trace
from lan_mesh.protocol import SubTask
from lan_mesh.task import TaskDAG


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_runtime, "CLI_AGENT_ALLOW_SELF_REPO", True)
    return SimpleNamespace(shared_folder=str(tmp_path), _cancel_event=threading.Event(),
                           _handle_cli_agent=Mock(return_value={"status": "completed", "result": "ok"}))


def test_promote_chain_and_roundtrip(tmp_path):
    plain = {"workspace": str(tmp_path), "steps": [
        {"node_id": f"n{i}", "name": f"step {i}", "prompt": f"do {i}",
         "executor": {"type": "harness", "harness": "codex"}, "accept": ["tests pass"]}
        for i in range(5)]}
    dag = graph_compat.promote(plain)
    assert len(dag.subtasks) == 5
    assert len(dag.to_graph_json()["edges"]) == 4
    for i, node in enumerate(dag.subtasks.values()):
        assert node.input_data["cwd"] == str(tmp_path)
        assert node.depends_on == ([f"n{i - 1}"] if i else [])
        assert SubTask.from_dict(node.to_dict()).executor == node.executor
    restored = TaskDAG.from_graph_json(dag.to_graph_json())
    view = graph_compat.linearize(restored)
    assert view["fidelity"] == "linear"
    assert [step["node_id"] for step in view["steps"]] == list(dag.subtasks)
    assert view["steps"][0]["executor"] == plain["steps"][0]["executor"]
    assert view["steps"][0]["accept"] == ["tests pass"]
    assert graph_compat.linearize(graph_compat.promote(view)) == view


def test_cwd_override_and_no_aliasing():
    plain = {"workspace": "root", "steps": [{"cwd": "other", "executor": {"type": "exec"}}]}
    dag = graph_compat.promote(plain)
    node = next(iter(dag.subtasks.values()))
    assert node.input_data["cwd"] == "other"
    node.executor["command"] = ["anything"]
    assert "command" not in plain["steps"][0]["executor"]


def test_linearize_cycle_and_parallel():
    with pytest.raises(ValueError):
        graph_compat.linearize(TaskDAG([
            SubTask(subtask_id="a", depends_on=["b"]),
            SubTask(subtask_id="b", depends_on=["a"])]))
    dag = TaskDAG([SubTask(subtask_id="a"), SubTask(subtask_id="b"),
                   SubTask(subtask_id="c", depends_on=["a", "b"])])
    assert [s["node_id"] for s in graph_compat.linearize(dag)["steps"]] == ["a", "b", "c"]


@pytest.mark.parametrize("executor", [{}, {"type": "llm"}])
def test_legacy_executor(runtime, executor):
    assert graph_compat.execute_node(runtime, {"executor": executor}) is None
    assert graph_compat.execute_node(runtime, {}) is None
    assert SubTask.from_dict({}).executor == {}
    assert TaskDAG.from_graph_json({"nodes": [{"id": "a"}]}).subtasks["a"].executor == {}


@pytest.mark.parametrize("code, status", [(0, "completed"), (2, "failed")])
def test_exec_real_process(runtime, code, status):
    result = graph_compat.execute_node(runtime, {
        "input_data": {"cwd": runtime.shared_folder},
        "executor": {"type": "exec", "command": [sys.executable, "-c",
            f"import sys; print('hello'); print('detail', file=sys.stderr); sys.exit({code})"]}})
    assert set(result) == {"output", "status", "error"}
    assert result["status"] == status
    assert result["output"] == {"stdout": "hello\n", "stderr": "detail\n", "returncode": code}


@pytest.mark.parametrize("kind", ["exec", "harness"])
def test_cwd_rejected(runtime, kind):
    result = graph_compat.execute_node(runtime, {"executor": {"type": kind},
                                               "input_data": {"cwd": "missing-dir"}})
    assert result["status"] == "failed" and result["error"]
    runtime._handle_cli_agent.assert_not_called()


def test_exec_timeout_and_read_only(runtime, monkeypatch):
    run = Mock(side_effect=subprocess.TimeoutExpired("cmd", 1800, output=b"partial", stderr=b"detail"))
    monkeypatch.setattr(graph_compat.subprocess, "run", run)
    node = {"input_data": {"cwd": runtime.shared_folder},
            "executor": {"type": "exec", "argv": ["cmd"], "timeout": 9000}}
    result = graph_compat.execute_node(runtime, node)
    assert result["status"] == "timeout"
    assert result["output"]["stdout"] == "partial"
    assert run.call_args.kwargs["timeout"] == 1800
    run.reset_mock()
    node["executor"]["read_only"] = True
    assert graph_compat.execute_node(runtime, node)["status"] == "failed"
    run.assert_not_called()


@pytest.mark.parametrize("status", ["completed", "failed", "timeout", "cancelled"])
def test_harness_forwarding_and_status(runtime, status):
    runtime._handle_cli_agent.return_value = {"status": status}
    result = graph_compat.execute_node(runtime, {
        "description": "prompt", "input_data": {"cwd": runtime.shared_folder, "requirement": "fallback"},
        "executor": {"type": "harness", "harness": "codex", "budget_usd": 2,
                     "allowed_tools": ["Read"], "timeout": 20}})
    assert result["status"] == status
    assert set(result) == {"output", "status", "error"}
    args = runtime._handle_cli_agent.call_args.args[0]
    assert args["backend"] == "codex" and args["requirement"] == "prompt"
    assert args["budget_usd"] == 2 and args["allowed_tools"] == ["Read"]
    assert args["timeout"] == 20


def test_harness_errors_and_fallback(runtime):
    runtime._handle_cli_agent.side_effect = RuntimeError("backend unavailable")
    node = {"input_data": {"cwd": runtime.shared_folder, "requirement": "fallback"},
            "executor": {"type": "harness", "harness": "missing"}}
    result = graph_compat.execute_node(runtime, node)
    assert result["status"] == "failed" and "unavailable" in result["error"]
    assert runtime._handle_cli_agent.call_args.args[0]["requirement"] == "fallback"
    runtime._handle_cli_agent.side_effect = None
    runtime._handle_cli_agent.return_value = {"error": "not installed"}
    assert graph_compat.execute_node(runtime, node)["error"] == "not installed"


@pytest.mark.parametrize("executor", [{"type": "unknown"}, {"type": "exec", "timeout": -1, "argv": ["cmd"]}])
def test_invalid_executor(runtime, executor):
    result = graph_compat.execute_node(runtime, {"executor": executor, "input_data": {"cwd": runtime.shared_folder}})
    assert result["status"] == "failed" and result["error"]


def test_runtime_dispatch_preserves_envelope_and_trace(tmp_path, monkeypatch):
    runtime = agent_runtime.AgentRuntime("graph-test", str(tmp_path))
    end = Mock()
    monkeypatch.setattr(runtime_trace, "trace_subtask_start", Mock(return_value="trace"))
    monkeypatch.setattr(runtime_trace, "trace_subtask_end", end)
    envelope = {"output": {"stdout": "ok"}, "status": "completed", "error": ""}
    monkeypatch.setattr(graph_compat, "execute_node", Mock(return_value=envelope))
    assert runtime.execute({"executor": {"type": "exec"}}) == envelope
    assert end.call_count == 1
    assert end.call_args.args[2] == "completed"
    monkeypatch.setattr(graph_compat, "execute_node", Mock(return_value=None))
    runtime._handlers["test_core"] = Mock(return_value={"answer": 42})
    assert runtime.execute({"required_skill": "test_core"})["output"] == {"answer": 42}
    runtime._handlers["test_core"].assert_called_once()
