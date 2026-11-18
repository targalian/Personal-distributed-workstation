"""Golden evaluation schema, offline CLI and isolated runtime contract tests."""

import json
from pathlib import Path
import socket
from types import SimpleNamespace

import pytest

from scripts import golden_eval as golden


@pytest.fixture
def suite() -> dict:
    return json.loads(golden.DEFAULT_TASKS.read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Golden tests must not use the network")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


def test_suite_composition(suite):
    tasks = golden.validate_tasks(suite)
    assert {t["id"] for t in tasks} == {f"G{i:02}" for i in range(1, 21)}
    assert {c: sum(t["category"] == c for t in tasks) for c in
            ("doc", "code", "data", "collaboration")} == {
                "doc": 5, "code": 8, "data": 4, "collaboration": 3}
    assert sum(t["difficulty"] == "complex" for t in tasks) >= 3


@pytest.mark.parametrize("args", [["--dry-run"], [], ["--list"]])
def test_offline_cli(args, monkeypatch, capsys):
    def forbidden(*args):
        raise AssertionError("offline mode constructed a runtime")
    monkeypatch.setattr(golden, "create_runtime", forbidden)
    monkeypatch.setattr(golden, "create_planner", forbidden)
    monkeypatch.setattr(golden.tempfile, "mkdtemp", forbidden)
    assert golden.main(args) == 0
    output = capsys.readouterr().out
    for i in range(1, 21):
        assert f"G{i:02}" in output
    if "--list" not in args:
        assert '"requirement"' in output and '"acceptance' not in output
        assert "Acceptance checks:" in output


@pytest.mark.parametrize("mutation", [
    lambda s: s.update(version=2),
    lambda s: s.update(version=True),
    lambda s: s["tasks"].pop(),
    lambda s: s["tasks"][1].update(id="G01"),
    lambda s: s["tasks"][0].update(id="bad"),
    lambda s: s["tasks"][0].pop("name"),
    lambda s: s["tasks"][0].update(description=""),
    lambda s: s["tasks"][0].update(difficulty="hard"),
    lambda s: s["tasks"][0].update(budget={"max_steps": True, "max_tokens": 1}),
    lambda s: s["tasks"][0].update(budget={"max_steps": 1, "max_tokens": -1}),
    lambda s: s["tasks"][0].update(workspace_files={"../bad": "x"}),
    lambda s: s["tasks"][0].update(workspace_files={"ok": 3}),
    lambda s: s["tasks"][0].update(acceptance=[]),
    lambda s: s["tasks"][0].update(acceptance=[{"type": "execute"}]),
    lambda s: s["tasks"][0].update(acceptance=[{"type": "file_exists", "path": "../x"}]),
    lambda s: s["tasks"][0].update(acceptance=[{"type": "stdout_contains", "text": ""}]),
])
def test_invalid_schema(suite, mutation):
    mutation(suite)
    with pytest.raises(ValueError):
        golden.validate_tasks(suite)


@pytest.mark.parametrize("path", ["../x", "a/../../x", "a\\..\\x", "/tmp/x",
                                    "C:\\x", "C:x", "\\\\server\\x", "a:stream", "", "."])
def test_path_escape(tmp_path, path):
    with pytest.raises(ValueError):
        golden.safe_path(tmp_path, path)


def test_acceptance(tmp_path):
    (tmp_path / "out.txt").write_text("你好 mesh", encoding="utf-8")
    checks = [{"type": "file_exists", "path": "out.txt"},
              {"type": "file_contains", "path": "out.txt", "text": "你好"},
              {"type": "stdout_contains", "text": "DONE"},
              {"type": "file_contains", "path": "missing", "text": "x"},
              {"type": "file_contains", "path": "out.txt", "text": "absent"},
              {"type": "file_exists", "path": "../outside"},
              {"type": "file_exists", "path": "."},
              {"type": "stdout_contains", "text": "absent"}]
    details = golden.check_acceptance(checks, tmp_path, "DONE")
    assert [d["passed"] for d in details] == [True, True, True, False, False, False, False, False]


@pytest.mark.parametrize("result,expected", [
    ({}, None), ({"usage": {}}, None),
    ({"usage": {"input_tokens": 3}}, None),
    ({"usage": {"input_tokens": 3, "output_tokens": 2}}, 5),
    ({"usage": {"input_tokens": 0, "output_tokens": 0}}, 0),
    ({"usage": {"input_tokens": True, "output_tokens": 0}}, None),
    ({"usage": {"input_tokens": -1, "output_tokens": 0}}, None),
    ({"usage": {"input_tokens": 2, "output_tokens": 1, "usage_known": False}}, None),
    ({"output": {"usage": {"input_tokens": 2, "output_tokens": 3}}}, 5),
    ({"runtime_trace": [{"input_tokens": 3, "output_tokens": 4},
                        {"input_tokens": 2, "output_tokens": 1}]}, 10),
    ({"runtime_trace": [{"input_tokens": 3, "output_tokens": 4},
                        {"input_tokens": 2, "output_tokens": 1, "usage_known": False}]}, None),
    ({"usage": {"input_tokens": 0, "output_tokens": 0}, "runtime_trace": [
        {"type": "llm_call", "input_tokens": 0, "output_tokens": 0, "usage_known": False}]}, None),
    ({"runtime_trace": [{"type": "llm_call", "input_tokens": 3, "output_tokens": 4},
                        {"type": "subtask_end", "input_tokens": 3, "output_tokens": 4}]}, 7),
])
def test_usage(result, expected):
    assert golden.usage_tokens(result) == expected


def fake_execution(monkeypatch, suite, fail=False, throws=False):
    observed = []
    def factory(workspace):
        def execute(payload):
            task = next(t for t in suite["tasks"] if t["id"] == payload["id"])
            assert payload["budget"] == task["budget"]
            assert payload["required_skill"] == task["skill"]
            assert Path(payload["input_data"]["cwd"]) == workspace
            assert Path(payload["input_data"]["project_path"]) == workspace
            for name, content in task["workspace_files"].items():
                assert (workspace / name).read_text(encoding="utf-8") == content
            observed.append(workspace)
            if throws:
                raise RuntimeError("fake runtime failure")
            contents = {}
            for check in task["acceptance"]:
                if "path" in check:
                    contents.setdefault(check["path"], []).append(check.get("text", ""))
            for name, texts in contents.items():
                target = workspace / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("\n".join(texts), encoding="utf-8")
            stdout = "\n".join(c["text"] for c in task["acceptance"] if c["type"] == "stdout_contains")
            return {"status": "failed" if fail else "completed", "output": {"stdout": stdout},
                    "usage": {"input_tokens": 7, "output_tokens": 3}}
        return SimpleNamespace(execute=execute)
    monkeypatch.setattr(golden, "create_runtime", factory)
    return observed


def test_single_cli_report(suite, monkeypatch, tmp_path):
    observed = fake_execution(monkeypatch, suite)
    report_path = tmp_path / "report.json"
    assert golden.main(["--run", "--ids", "G01,G06", "--out", str(report_path)]) == 0
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["version"] == 1 and report["mode"] == "single" and report["started_at"]
    assert [r["id"] for r in report["tasks"]] == ["G01", "G06"]
    assert all(r["acceptance_passed"] for r in report["tasks"])
    assert report["summary"]["tokens"] == 20
    assert report["summary"]["failed_ids"] == []
    assert len(set(observed)) == 2
    assert all(not p.exists() for p in observed)


@pytest.mark.parametrize("throws", [False, True])
def test_failed_runtime(suite, monkeypatch, tmp_path, throws):
    observed = fake_execution(monkeypatch, suite, fail=True, throws=throws)
    out = tmp_path / "failed.json"
    assert golden.main(["--run", "--ids", "G01,G02", "--out", str(out)]) == 1
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["summary"]["failed_ids"] == ["G01", "G02"]
    assert all(r["failures"] for r in report["tasks"])
    assert all(not p.exists() for p in observed)


def test_keep(suite, monkeypatch):
    fake_execution(monkeypatch, suite)
    row = golden.evaluate_task(suite["tasks"][0], "single", keep=True)
    workspace = Path(row["workspace"])
    try:
        assert workspace.is_dir()
        assert "summary.md" in row["artifacts"]
    finally:
        golden.shutil.rmtree(workspace)


def test_plan_only(suite, monkeypatch, tmp_path):
    def forbidden(*args):
        raise AssertionError("plan mode executed a task")
    monkeypatch.setattr(golden, "create_runtime", lambda p: SimpleNamespace(execute=forbidden))
    plans = []
    def analyze(payload):
        plans.append(payload)
        return {"pattern": "teams", "team_size": 3, "decomposition": [{"name": "review"}]}
    monkeypatch.setattr(golden, "create_planner", lambda r: SimpleNamespace(analyze_with_skill=analyze))
    out = tmp_path / "plan.json"
    assert golden.main(["--run", "--mode", "plan", "--ids", "G18", "--out", str(out)]) == 0
    row = json.loads(out.read_text(encoding="utf-8"))["tasks"][0]
    assert len(plans) == 1 and plans[0]["budget"] == suite["tasks"][17]["budget"]
    assert row["team_size"] == 3 and row["pattern"] == "teams"
    assert row["acceptance_passed"] is None and row["tokens"] is None


@pytest.mark.parametrize("ids", ["G99", "", "G01,G01", "G01,"])
def test_unknown_ids(ids):
    assert golden.main(["--dry-run", "--ids", ids]) == 2


@pytest.mark.parametrize("text", ["{", "[]", "null", '{"version":1,"tasks":[]}'])
def test_bad_json(tmp_path, text):
    path = tmp_path / "bad.json"
    path.write_text(text, encoding="utf-8")
    assert golden.main(["--tasks", str(path), "--dry-run"]) == 2


def test_missing_file(tmp_path):
    assert golden.main(["--tasks", str(tmp_path / "absent"), "--list"]) == 2


def test_trace_fallback(suite, monkeypatch):
    fake_execution(monkeypatch, suite)
    original = golden.create_runtime
    def factory(workspace):
        runtime = original(workspace)
        execute = runtime.execute
        def without_usage(payload):
            result = execute(payload)
            result.pop("usage")
            return result
        runtime.execute = without_usage
        return runtime
    monkeypatch.setattr(golden, "create_runtime", factory)
    monkeypatch.setattr(golden, "read_runtime_trace", lambda task_id: [
        {"type": "llm_call", "input_tokens": 10, "output_tokens": 20}])
    assert golden.evaluate_task(suite["tasks"][0], "single")["tokens"] == 30


def test_shell_observation_stdout():
    result = {"output": {"steps": [
        {"tool": "shell_exec", "result_preview": "CHECK_OK"},
        {"tool": "file_write", "result_preview": "DO_NOT_COUNT"}]}}
    text = golden._stdout(result, "")
    assert "CHECK_OK" in text and "DO_NOT_COUNT" not in text


def test_acceptance_failure_is_exit_one(suite, monkeypatch, tmp_path):
    monkeypatch.setattr(golden, "create_runtime", lambda p: SimpleNamespace(
        execute=lambda payload: {"status": "completed"}))
    out = tmp_path / "acceptance.json"
    assert golden.main(["--run", "--ids", "G01", "--out", str(out)]) == 1
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["tasks"][0]["acceptance_passed"] is False
    assert report["summary"]["tokens"] is None
    assert report["summary"]["unknown_token_tasks"] == 1


@pytest.mark.parametrize("plan", [{}, {"pattern": "teams", "team_size": 0, "decomposition": []},
                                   {"pattern": "single", "team_size": 1, "decomposition": [],
                                    "status": "failed", "error": "planner unavailable"}])
def test_invalid_plan(suite, monkeypatch, plan):
    monkeypatch.setattr(golden, "create_runtime", lambda p: SimpleNamespace())
    monkeypatch.setattr(golden, "create_planner", lambda r: SimpleNamespace(
        analyze_with_skill=lambda payload: plan))
    row = golden.evaluate_task(suite["tasks"][17], "plan")
    assert row["status"] == "failed" and row["failures"]


def test_fixture_python_sources_compile(suite):
    for task in suite["tasks"]:
        for name, content in task["workspace_files"].items():
            if name.endswith(".py"):
                compile(content, name, "exec")
