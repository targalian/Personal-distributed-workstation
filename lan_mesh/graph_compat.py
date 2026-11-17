"""Plain step plans and executor dispatch over the existing TaskDAG engine."""

from __future__ import annotations

from copy import deepcopy
import math
import subprocess
import uuid

from . import agent_runtime
from .protocol import SubTask
from .task import TaskDAG

EXECUTOR_TYPES = {"llm", "harness", "exec"}


def executor_of(subtask: dict) -> dict:
    """Return an independent executor specification, defaulting legacy nodes to LLM."""
    executor = deepcopy(subtask.get("executor") or {})
    if not isinstance(executor, dict):
        raise ValueError("executor must be a dict")
    executor.setdefault("type", "llm")
    if executor["type"] not in EXECUTOR_TYPES:
        raise ValueError(f"Unknown executor type: {executor['type']}")
    return executor


def promote(plain: dict) -> TaskDAG:
    """Promote ordered steps to a chain; workspace supplies missing step cwd only."""
    nodes = []
    for index, step in enumerate(plain.get("steps", []), 1):
        inputs = deepcopy(step.get("input_data") or {})
        inputs["cwd"] = step.get("cwd", inputs.get("cwd", plain.get("workspace", "")))
        if "accept" in step:
            inputs["accept"] = deepcopy(step["accept"])
        node = SubTask(
            subtask_id=step.get("node_id") or uuid.uuid4().hex,
            name=step.get("name", f"Step {index}"),
            description=step.get("prompt", step.get("description", "")),
            required_skill=step.get("required_skill", ""),
            input_data=inputs, executor=executor_of(step),
            depends_on=[nodes[-1].subtask_id] if nodes else [],
        )
        if any(existing.subtask_id == node.subtask_id for existing in nodes):
            raise ValueError(f"Duplicate node_id: {node.subtask_id}")
        nodes.append(node)
    return TaskDAG(nodes)


def linearize(dag: TaskDAG) -> dict:
    """Export a topological view; linear fidelity does not preserve branches or conditions."""
    order = dag.topological_sort()
    if len(order) != len(dag.subtasks):
        raise ValueError("Cannot linearize a cyclic graph or dangling dependency")
    steps = []
    for index, node_id in enumerate(order, 1):
        node = dag.subtasks[node_id]
        steps.append({
            "step": index, "node_id": node_id, "name": node.name,
            "executor": executor_of(node.to_dict()),
            "cwd": node.input_data.get("cwd", ""), "prompt": node.description,
            "accept": deepcopy(node.input_data.get("accept", [])),
        })
    return {"fidelity": "linear", "steps": steps}


def _result(output: dict, status: str, error: str = "") -> dict:
    return {"output": output, "status": status, "error": error}


def _timeout(executor: dict) -> float:
    timeout = float(executor.get("timeout", 1800))
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("executor timeout must be positive and finite")
    return min(timeout, 1800)


def _execute_exec(executor: dict, cwd: str) -> dict:
    if executor.get("read_only") is True:
        return _result({}, "failed", "exec read_only cannot be enforced; execution refused")
    command = executor.get("command", executor.get("argv"))
    if not isinstance(command, (str, list)) or not command:
        raise ValueError("exec requires a nonempty command or argv")
    try:
        proc = subprocess.run(command, cwd=cwd, timeout=_timeout(executor),
                              capture_output=True, text=True, encoding="utf-8", errors="replace",
                              shell=False)
    except subprocess.TimeoutExpired as exc:
        def decoded(value):
            return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value or ""
        return _result({"stdout": decoded(exc.stdout), "stderr": decoded(exc.stderr),
                        "returncode": None}, "timeout", str(exc))
    output = {"stdout": proc.stdout, "stderr": proc.stderr, "returncode": proc.returncode}
    return _result(output, "completed" if proc.returncode == 0 else "failed",
                   "" if proc.returncode == 0 else proc.stderr or f"Command exited with {proc.returncode}")


def execute_node(runtime: object, subtask: dict) -> dict | None:
    """Dispatch explicit executors and return one envelope; None preserves legacy LLM handling."""
    try:
        executor = executor_of(subtask)
        if executor["type"] == "llm":
            return None
        if getattr(runtime, "_cancel_event", None) and runtime._cancel_event.is_set():
            return _result({}, "cancelled", "Execution cancelled")
        inputs = deepcopy(subtask.get("input_data") or {})
        cwd = inputs.get("cwd", "")
        roots = [getattr(runtime, "shared_folder", ""), *agent_runtime.CLI_AGENT_ALLOWED_ROOTS]
        reject = agent_runtime.validate_cli_agent_cwd(cwd, roots)
        if reject:
            return _result({}, "failed", reject)
        if executor["type"] == "exec":
            result = _execute_exec(executor, cwd)
        else:
            inputs.update(cwd=cwd, backend=executor.get("harness", ""),
                          requirement=subtask.get("description") or inputs.get("requirement", ""),
                          timeout=_timeout(executor))
            for key in ("budget_usd", "allowed_tools"):
                if key in executor:
                    inputs[key] = deepcopy(executor[key])
            output = runtime._handle_cli_agent(inputs)
            if not isinstance(output, dict):
                raise ValueError("harness result must be a dict")
            status, error = agent_runtime.infer_result_status(output)
            if output.get("status") == "timeout" or output.get("timed_out"):
                status = "timeout"
            result = _result(output, status, str(error or ""))
        print(f"[AgentRuntime] Executor {executor['type']}: {result['status']}")
        return result
    except Exception as exc:
        print(f"[AgentRuntime] Executor failed: {exc}")
        return _result({}, "failed", str(exc))
