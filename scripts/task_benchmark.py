"""Offline task benchmark catalog, record validation, and comparison utility."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TASKS = ROOT / "benchmarks" / "tasks.json"
RECORD_FIELDS = set("system_version model task_id input_hash acceptance_hash run_id attempt duration_ms usage human_intervention acceptance_evidence recovery_conclusion fixture".split())
TASK_FIELDS = set("task_id input acceptance input_hash acceptance_hash acceptance_criteria".split())


def digest(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def _finite(value: Any) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("NaN/infinity is forbidden")
    if isinstance(value, dict):
        for item in value.values():
            _finite(item)
    elif isinstance(value, list):
        for item in value:
            _finite(item)


def _object(value: Any, fields: set[str], label: str) -> dict:
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError(f"{label} requires exactly: {', '.join(sorted(fields))}")
    return value


def _text(value: Any, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a nonempty string")


def _number(value: Any, label: str, integer: bool = False) -> None:
    allowed = (int,) if integer else (int, float)
    if type(value) not in allowed or value < 0:
        raise ValueError(f"{label} must be a nonnegative {'integer' if integer else 'number'}")
    _finite(value)


def _hashes(task: dict[str, Any]) -> tuple[str, str]:
    return digest(task["input"]), digest({"acceptance": task["acceptance"], "acceptance_criteria": task["acceptance_criteria"]})


def _catalog(tasks: Any) -> None:
    if not isinstance(tasks, dict) or not tasks:
        raise ValueError("task catalog must not be empty")
    _finite(tasks)
    for tid, task in tasks.items():
        _object(task, TASK_FIELDS, "task")
        _text(tid, "task_id")
        if task["task_id"] != tid:
            raise ValueError("catalog key must match task_id")
        criteria = task["acceptance_criteria"]
        if not isinstance(criteria, list) or not criteria:
            raise ValueError("acceptance_criteria must be a nonempty list")
        for criterion in criteria:
            _text(criterion, "criterion")
        if (task["input_hash"], task["acceptance_hash"]) != _hashes(task):
            raise ValueError(f"catalog hash mismatch: {tid}")


def load_tasks(path: Path = DEFAULT_TASKS) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    _object(data, {"schema_version", "fixture_notice", "tasks"}, "catalog")
    if type(data["schema_version"]) is not int or data["schema_version"] != 1:
        raise ValueError("unsupported catalog schema_version")
    _text(data["fixture_notice"], "fixture_notice")
    if not isinstance(data["tasks"], list):
        raise ValueError("tasks must be a list")
    tasks = {}
    for task in data["tasks"]:
        _object(task, TASK_FIELDS, "task")
        tid = task["task_id"]
        _text(tid, "task_id")
        if tid in tasks:
            raise ValueError("duplicate task_id")
        tasks[tid] = task
    _catalog(tasks)
    return tasks


def template(tasks: dict[str, Any]) -> dict[str, Any]:
    _catalog(tasks)
    task = next(iter(tasks.values()))
    return {
        "system_version": "fixture-system-0", "model": {"name": "fixture-model", "parameters": {}},
        "task_id": task["task_id"], "input_hash": task["input_hash"], "acceptance_hash": task["acceptance_hash"],
        "run_id": "run-REPLACE", "attempt": 1, "duration_ms": None,
        "usage": {"input_tokens": None, "output_tokens": None, "currency": None, "cost": None},
        "human_intervention": False, "fixture": True,
        "acceptance_evidence": {"status": "unverified", "refs": [], "verification": None},
        "recovery_conclusion": "not_assessed",
    }


def _evidence(evidence: Any) -> None:
    e = _object(evidence, {"status", "refs", "verification"}, "acceptance_evidence")
    if e["status"] not in ("unverified", "verified", "rejected"):
        raise ValueError("invalid acceptance evidence status")
    if not isinstance(e["refs"], list):
        raise ValueError("evidence refs must be a list")
    for ref in e["refs"]:
        _text(ref, "evidence ref")
    if e["verification"] is not None:
        metadata = _object(e["verification"], {"method", "verifier", "verified_at"}, "verification")
        for key, value in metadata.items():
            _text(value, key)
    if e["status"] == "verified" and (not e["refs"] or e["verification"] is None):
        raise ValueError("verified requires independent refs and verification metadata")


def _record(rec: Any, tasks: dict[str, Any]) -> None:
    _object(rec, RECORD_FIELDS, "record")
    _finite(rec)
    for key in ("system_version", "run_id", "task_id", "input_hash", "acceptance_hash"):
        _text(rec[key], key)
    task = tasks.get(rec["task_id"])
    if task is None:
        raise ValueError("unknown task_id")
    if any(rec[key] != task[key] for key in ("input_hash", "acceptance_hash")):
        raise ValueError("task hash mismatch")
    model = _object(rec["model"], {"name", "parameters"}, "model")
    _text(model["name"], "model.name")
    if not isinstance(model["parameters"], dict):
        raise ValueError("model.parameters must be object")
    _number(rec["attempt"], "attempt", integer=True)
    if rec["attempt"] < 1:
        raise ValueError("attempt must be >=1")
    if rec["duration_ms"] is not None:
        _number(rec["duration_ms"], "duration_ms")
    usage = _object(rec["usage"], {"input_tokens", "output_tokens", "currency", "cost"}, "usage")
    for key in ("input_tokens", "output_tokens", "cost"):
        if usage[key] is not None:
            _number(usage[key], key, integer=key != "cost")
    if usage["currency"] is not None:
        _text(usage["currency"], "currency")
    if usage["cost"] is not None and usage["currency"] is None:
        raise ValueError("known cost requires currency")
    for key in ("human_intervention", "fixture"):
        if type(rec[key]) is not bool:
            raise ValueError(f"{key} must be bool")
    _evidence(rec["acceptance_evidence"])
    if rec["recovery_conclusion"] not in ("not_assessed", "succeeded", "failed"):
        raise ValueError("invalid recovery_conclusion")


def validate(records: Any, tasks: dict[str, Any]) -> dict[str, Any]:
    errors, seen = [], set()
    try:
        _catalog(tasks)
    except (ValueError, TypeError) as exc:
        return {"valid": False, "records": 0, "errors": [str(exc)]}
    if not isinstance(records, list) or not records:
        return {"valid": False, "records": 0, "errors": ["records must be a nonempty list"]}
    for index, rec in enumerate(records):
        try:
            _record(rec, tasks)
            if rec["run_id"] in seen:
                raise ValueError("duplicate run_id")
            seen.add(rec["run_id"])
        except (ValueError, TypeError) as exc:
            errors.append(f"{index}: {exc}")
    return {"valid": not errors, "records": len(records), "errors": errors}


def _summarize(group: list[dict]) -> dict:
    first, count = group[0], len(group)
    costs = [r["usage"]["cost"] for r in group if r["usage"]["cost"] is not None]
    currencies = {r["usage"]["currency"] for r in group if r["usage"]["cost"] is not None}
    times = [r["duration_ms"] for r in group if r["duration_ms"] is not None]
    completed = sum(r["acceptance_evidence"]["status"] == "verified" for r in group)
    assisted = sum(r["human_intervention"] for r in group)
    recovery = [r for r in group if r["recovery_conclusion"] != "not_assessed"]
    recovered = sum(r["recovery_conclusion"] == "succeeded" for r in recovery)
    same_currency = len(currencies) == 1
    row = {key: first[key] for key in ("system_version", "task_id", "model", "input_hash", "acceptance_hash", "fixture")}
    row.update(sample_count=count, completed_count=completed, completion_rate=completed/count,
        duration_ms={"count": len(times), "mean": statistics.mean(times) if times else None,
                     "min": min(times) if times else None, "max": max(times) if times else None},
        human_intervention_count=assisted, human_intervention_rate=assisted/count,
        recovery_assessed_count=len(recovery), recovery_success_count=recovered,
        recovery_success_rate=recovered/len(recovery) if recovery else None,
        cost_known_coverage=len(costs)/count, missing_cost=count-len(costs),
        currency=next(iter(currencies)) if same_currency else "mixed" if currencies else None,
        cost_known_total=sum(costs) if same_currency else None,
        cost_total=sum(costs) if same_currency and len(costs) == count else None,
        comparable=same_currency and len(costs) == count,
        reason="mixed currencies" if len(currencies) > 1 else "missing cost" if len(costs) < count else None)
    return row


def compare(records: Any, tasks: dict[str, Any]) -> dict[str, Any]:
    check = validate(records, tasks)
    if not check["valid"]:
        return {"valid": False, "errors": check["errors"], "groups": []}
    groups = {}
    for record in records:
        key = tuple(record[k] for k in ("system_version", "task_id", "input_hash", "acceptance_hash", "fixture"))
        key += (digest(record["model"]),)
        groups.setdefault(key, []).append(record)
    return {"valid": True, "groups": [_summarize(group) for group in groups.values()],
            "fixture_warning": "Fixture records are not real model scores or real rates.",
            "evidence_boundary": "Imported verification claims are structurally checked; referenced evidence is not opened or independently verified.",
            "comparison_rule": "Compare systems only for equal task/input/model/acceptance hashes and fixture flag. comparable reports cost completeness only."}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("template", "validate", "compare"):
        command = sub.add_parser(name)
        command.add_argument("--tasks", type=Path, default=DEFAULT_TASKS)
        command.add_argument("--input", type=Path, required=name != "template")
        command.add_argument("--output", type=Path)
    try:
        args = parser.parse_args(argv)
        tasks = load_tasks(args.tasks)
        if args.cmd == "template":
            result = template(tasks)
        else:
            data = json.loads(args.input.read_text(encoding="utf-8"))
            records = data
            if isinstance(data, dict):
                records = [data] if "run_id" in data else data.get("records")
            result = validate(records, tasks) if args.cmd == "validate" else compare(records, tasks)
        text = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        if args.output:
            args.output.write_text(text, encoding="utf-8")
        else:
            print(text, end="")
        return 0 if args.cmd == "template" or result.get("valid", False) else 2
    except (OSError, ValueError, TypeError, OverflowError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
