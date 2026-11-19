"""Offline-first Golden task validation, single-agent execution and planning evaluation.

No runtime is imported unless --run is explicit. Plan mode only analyzes tasks;
it never creates a PM or dispatches a team. Exit codes: pass=0, evaluation=1,
invalid input/configuration=2. --keep preserves isolated temporary workspaces.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
from pathlib import Path, PureWindowsPath
import re
import shutil
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TASKS = Path(__file__).with_name("golden_tasks.json")


def safe_path(workspace: Path, value: str) -> Path:
    """Resolve a relative task path, rejecting escapes and Windows drive paths."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("path must be a nonempty relative string")
    normalized = value.replace("\\", "/")
    if PureWindowsPath(value).drive or normalized.startswith("/") or ":" in value:
        raise ValueError(f"absolute/drive path is forbidden: {value}")
    if ".." in normalized.split("/") or "\x00" in value:
        raise ValueError(f"unsafe path: {value}")
    target = (workspace / normalized).resolve()
    if target == workspace.resolve() or not target.is_relative_to(workspace.resolve()):
        raise ValueError(f"path escapes workspace: {value}")
    return target


def validate_tasks(data: object) -> list[dict]:
    """Validate the complete v1 suite before any workspace or runtime is created."""
    if not isinstance(data, dict) or type(data.get("version")) is not int or data["version"] != 1:
        raise ValueError("suite version must be 1")
    tasks = data.get("tasks")
    if not isinstance(tasks, list) or len(tasks) != 20:
        raise ValueError("suite must contain exactly 20 tasks")
    seen = set()
    for task in tasks:
        if not isinstance(task, dict):
            raise ValueError("each task must be an object")
        for key in ("id", "name", "category", "description", "skill", "difficulty"):
            if not isinstance(task.get(key), str) or not task[key].strip():
                raise ValueError(f"missing/non-string task field: {key}")
        task_id = task["id"]
        if not re.fullmatch(r"G(?:0[1-9]|1[0-9]|20)", task_id) or task_id in seen:
            raise ValueError(f"invalid or duplicate id: {task_id}")
        seen.add(task_id)
        if task["difficulty"] not in {"simple", "moderate", "complex"}:
            raise ValueError(f"{task_id}: invalid difficulty")
        budget = task.get("budget")
        if not isinstance(budget, dict) or any(
            type(budget.get(k)) is not int or budget[k] <= 0
            for k in ("max_steps", "max_tokens")
        ):
            raise ValueError(f"{task_id}: budget requires positive integer limits")
        files = task.get("workspace_files")
        if not isinstance(files, dict):
            raise ValueError(f"{task_id}: workspace_files must be an object")
        for path, content in files.items():
            safe_path(ROOT, path)
            if not isinstance(content, str):
                raise ValueError(f"{task_id}: workspace content must be text")
        checks = task.get("acceptance")
        if not isinstance(checks, list) or not checks:
            raise ValueError(f"{task_id}: acceptance must be nonempty")
        for check in checks:
            if not isinstance(check, dict) or check.get("type") not in {
                "file_exists", "file_contains", "stdout_contains"
            }:
                raise ValueError(f"{task_id}: unknown acceptance type")
            if check["type"] != "stdout_contains":
                safe_path(ROOT, check.get("path"))
            if check["type"] != "file_exists" and (
                not isinstance(check.get("text"), str) or not check["text"]
            ):
                raise ValueError(f"{task_id}: acceptance text must be nonempty")
    return tasks


def load_tasks(path: Path) -> list[dict]:
    """Read and validate a UTF-8 JSON suite."""
    return validate_tasks(json.loads(path.read_text(encoding="utf-8-sig")))


def build_payload(task: dict, workspace: Path) -> dict:
    """Render exactly the payload used by both dry runs and actual execution."""
    prompt = (task["description"] + "\nWork only inside: " + str(workspace)
              + "\nRead the supplied workspace files. Create the requested artifacts."
              + "\nAcceptance checks: " + json.dumps(task["acceptance"], ensure_ascii=False))
    return {"id": task["id"], "name": task["name"], "description": prompt,
            "parent_task_id": "golden-" + task["id"], "required_skill": task["skill"],
            "budget": dict(task["budget"]),
            "input_data": {"requirement": prompt, "description": prompt,
                           "cwd": str(workspace), "project_path": str(workspace)}}


def check_acceptance(checks: list[dict], workspace: Path, stdout: str) -> list[dict]:
    """Evaluate deterministic checks; unreadable files and escapes fail closed."""
    details = []
    for check in checks:
        detail = dict(check, passed=False)
        try:
            if check["type"] == "stdout_contains":
                detail["passed"] = check["text"] in stdout
            else:
                path = safe_path(workspace, check["path"])
                detail["passed"] = path.is_file()
                if check["type"] == "file_contains":
                    detail["passed"] = check["text"] in path.read_text(encoding="utf-8")
        except (OSError, ValueError) as exc:
            detail["error"] = str(exc)
        details.append(detail)
    return details


def create_runtime(workspace: Path) -> object:
    """Lazily construct a runtime only for an explicit real run."""
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from lan_mesh.agent_runtime import AgentRuntime
    return AgentRuntime("golden-eval", str(workspace))


def create_planner(runtime: object) -> object:
    """Construct the planner directly, without a live ProjectManagerAgent."""
    from lan_mesh.pm_planner import PMPlanner
    from lan_mesh.pm_state import PMState
    planner = PMPlanner("golden-plan", runtime, PMState(), SimpleNamespace(secretary_url=""))
    planner.load_skill()
    return planner


def usage_tokens(result: dict) -> int | None:
    """Prefer per-call traces over aggregate usage; incomplete counts stay null."""
    output = result.get("output")
    candidates = [result.get("usage"), output.get("usage") if isinstance(output, dict) else None]
    trace = result.get("runtime_trace", [])
    if isinstance(trace, dict):
        trace = [trace]
    if isinstance(trace, list):
        calls = [r for r in trace if isinstance(r, dict) and r.get("type") == "llm_call"]
        records = calls or trace
        if records:
            totals = [_token_pair(r) for r in records]
            return sum(totals) if all(n is not None for n in totals) else None
    for usage in candidates:
        total = _token_pair(usage)
        if total is not None:
            return total
    return None


def read_runtime_trace(task_id: str) -> list[dict]:
    """Read only this attempt's LLM records from an already imported tracer."""
    tracer = sys.modules.get("lan_mesh.runtime_trace")
    if tracer is None:
        return []
    return [r for r in tracer.read_trace_lines(limit=5000, line_type="llm_call")
            if r.get("task_id") == task_id]


def token_accounting(result: dict, attempted: bool = True) -> tuple[int | None, str]:
    """Count only reported usage or explicit failures before a connection exists."""
    if not attempted:
        return None, "not_attempted"
    tokens = usage_tokens(result)
    if tokens is not None:
        return tokens, "reported"
    trace = result.get("runtime_trace", [])
    if isinstance(trace, dict):
        trace = [trace]
    if not isinstance(trace, list):
        trace = []
    calls = [r for r in trace if isinstance(r, dict) and r.get("type") == "llm_call"]
    records = calls or trace or [result]
    totals = []
    reported = False
    for record in records:
        if not isinstance(record, dict):
            return None, "unknown"
        count = _token_pair(record) if trace else None
        if count is not None:
            reported = True
        elif _connect_failed(str(record.get("error", ""))):
            count = 0
        else:
            return None, "unknown"
        totals.append(count)
    return sum(totals), "reported" if reported else "connect_failed"


def _connect_failed(error: str) -> bool:
    text = error.lower()
    # A read timeout can follow a successful connection and billable generation.
    if any(marker in text for marker in ("read timed out", "readtimeout", "read timeout")):
        return False
    return any(marker in text for marker in (
        "connecttimeout", "connection refused", "name or service not known",
        "failed to establish a new connection", "getaddrinfo failed"))


def summarize_rows(rows: list[dict]) -> dict:
    """Keep legacy totals and add averages over known usage only."""
    failed = [r["id"] for r in rows if r["status"] != "completed"]
    known = [r["tokens"] for r in rows if r["tokens"] is not None]
    summary = {"passed": len(rows) - len(failed), "failed": len(failed),
               "failed_ids": failed, "elapsed_s": sum(r["elapsed_s"] for r in rows),
               "tokens": sum(known) if len(known) == len(rows) else None,
               "known_tokens": sum(known), "unknown_token_tasks": len(rows) - len(known),
               "token_accounting": {reason: 0 for reason in
                                    ("reported", "connect_failed", "unknown", "not_attempted")}}
    for row in rows:
        summary["token_accounting"][row["token_reason"]] += 1
    for field in ("difficulty", "category"):
        groups = {}
        for row in rows:
            group = groups.setdefault(row[field], {"runs": 0, "known": 0, "known_tokens": 0})
            group["runs"] += 1
            if row["token_reason"] in ("reported", "connect_failed"):
                group["known"] += 1
                group["known_tokens"] += row["tokens"]
        for group in groups.values():
            group["avg"] = group["known_tokens"] / group["known"] if group["known"] else None
        summary[f"by_{field}"] = groups
    return summary


def _token_pair(usage):
    if not isinstance(usage, dict) or usage.get("usage_known") is False:
        return None
    pair = [usage.get(k) for k in ("input_tokens", "output_tokens")]
    return sum(pair) if all(type(n) is int and n >= 0 for n in pair) else None


def _stdout(result, captured):
    output = result.get("output", {})
    values = [captured, result.get("stdout", "")]
    if isinstance(output, dict):
        values.extend(output.get(k, "") for k in ("stdout", "result", "content", "summary"))
        values.extend(step.get("result_preview", "") for step in output.get("steps", [])
                      if isinstance(step, dict) and step.get("tool") == "shell_exec")
    elif isinstance(output, str):
        values.append(output)
    return "\n".join(value for value in values if isinstance(value, str))


def evaluate_task(task: dict, mode: str, keep: bool = False) -> dict:
    """Execute one isolated case, preserving failures and cleaning up in finally."""
    workspace = Path(tempfile.mkdtemp(prefix=f"golden-{task['id']}-"))
    started = time.perf_counter()
    row = {"id": task["id"], "status": "failed", "elapsed_s": 0, "tokens": None,
           "token_reason": "not_attempted", "difficulty": task["difficulty"],
           "category": task["category"],
           "acceptance_passed": False if mode == "single" else None,
           "acceptance_details": [], "failures": [], "artifacts": []}
    runtime = None
    result = {}
    attempted = False
    try:
        for path, content in task["workspace_files"].items():
            destination = safe_path(workspace, path)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(content, encoding="utf-8")
        payload = build_payload(task, workspace)
        payload["parent_task_id"] += "-" + uuid.uuid4().hex
        runtime = create_runtime(workspace)
        capture = io.StringIO()
        with contextlib.redirect_stdout(capture):
            if mode == "plan":
                planner = create_planner(runtime)
                attempted = True
                result = planner.analyze_with_skill(payload)
            else:
                attempted = True
                result = runtime.execute(payload)
        if not isinstance(result, dict):
            raise ValueError("runtime/planner must return an object")
        if not result.get("runtime_trace"):
            result["runtime_trace"] = read_runtime_trace(payload["parent_task_id"])
        row["tokens"], row["token_reason"] = token_accounting(result)
        if mode == "plan":
            if result.get("error") or result.get("status", "completed") != "completed":
                raise ValueError(f"planner failed: {result.get('error') or result.get('status')}")
            for key in ("pattern", "team_size", "decomposition"):
                row[key] = result.get(key)
            if (not isinstance(row["pattern"], str) or not row["pattern"]
                    or type(row["team_size"]) is not int or row["team_size"] < 1
                    or not isinstance(row["decomposition"], list)):
                raise ValueError("planner returned an invalid plan")
            row["status"] = "completed"
        else:
            row["runtime_status"] = result.get("status")
            details = check_acceptance(task["acceptance"], workspace, _stdout(result, capture.getvalue()))
            row["acceptance_details"] = details
            row["acceptance_passed"] = all(d["passed"] for d in details)
            row["failures"] = [f"acceptance failed: {d}" for d in details if not d["passed"]]
            if result.get("status") != "completed":
                row["failures"].append(str(result.get("error") or result.get("status") or "missing runtime status"))
            if not row["failures"]:
                row["status"] = "completed"
    except Exception as exc:
        row["failures"].append(f"{type(exc).__name__}: {exc}")
        if row["token_reason"] == "not_attempted":
            evidence = dict(result) if isinstance(result, dict) else {}
            evidence["error"] = row["failures"][-1]
            if attempted:
                evidence["runtime_trace"] = read_runtime_trace(payload["parent_task_id"])
            row["tokens"], row["token_reason"] = token_accounting(evidence, attempted)
    finally:
        row["elapsed_s"] = round(time.perf_counter() - started, 6)
        row["artifacts"] = sorted(p.relative_to(workspace).as_posix()
                                  for p in workspace.rglob("*") if p.is_file())
        if keep:
            row["workspace"] = str(workspace)
        if runtime is not None and hasattr(runtime, "services"):
            try:
                runtime.services.close()
            except Exception as exc:
                row["status"] = "failed"
                row["failures"].append(f"runtime cleanup: {exc}")
        if not keep:
            shutil.rmtree(workspace)
    return row


def main(argv: list[str] | None = None) -> int:
    """CLI entry point; absent --run defaults to a completely offline dry run."""
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--list", action="store_true")
    action.add_argument("--dry-run", action="store_true")
    action.add_argument("--run", action="store_true")
    parser.add_argument("--tasks", type=Path, default=DEFAULT_TASKS)
    parser.add_argument("--ids", help="comma-separated case IDs")
    parser.add_argument("--mode", choices=("single", "plan"), default="single")
    parser.add_argument("--out", type=Path, default=Path("golden-report.json"))
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args(argv)
    try:
        tasks = load_tasks(args.tasks)
        if args.ids is not None:
            ids = [v.strip() for v in args.ids.split(",")]
            unknown = set(ids) - {t["id"] for t in tasks}
            if unknown or len(ids) != len(set(ids)):
                raise ValueError(f"unknown or duplicate IDs: {args.ids}")
            tasks = [t for t in tasks if t["id"] in ids]
        if args.list:
            for task in tasks:
                print(f"{task['id']} {task['category']} {task['difficulty']} {task['name']}")
            return 0
        if not args.run:
            for task in tasks:
                print(json.dumps({"id": task["id"], "workspace_files": task["workspace_files"],
                                  "payload": build_payload(task, Path("<workspace>") / task["id"])},
                                 ensure_ascii=False))
            print(f"[Task] dry-run validated: {len(tasks)} tasks; no runtime created")
            return 0
        report = {"version": 1, "mode": args.mode,
                  "started_at": datetime.now(timezone.utc).isoformat(), "tasks": []}
        for task in tasks:
            print(f"[Task] starting {task['id']} ({args.mode})")
            row = evaluate_task(task, args.mode, args.keep)
            report["tasks"].append(row)
            print(f"[Task] {task['id']}: {row['status']}")
        rows = report["tasks"]
        report["summary"] = summarize_rows(rows)
        args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(report["summary"], ensure_ascii=False))
        counts = report["summary"]["token_accounting"]
        print(f"[Task] token 统计: reported {counts['reported']} / "
              f"connect_failed {counts['connect_failed']} / unknown {counts['unknown']}")
        return 1 if report["summary"]["failed"] else 0
    except (OSError, ValueError, TypeError) as exc:
        print(f"[Task] configuration error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
