"""子任务产出引用、路径边界及 PM 聚合提示词回归测试。"""

import hashlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from lan_mesh.artifact_refs import DEFAULT_LIMIT, resolve, store_output, summarize
from lan_mesh.pm_monitor import PMMonitor
from lan_mesh.pm_state import PMState


def test_short_summary_has_no_storage_side_effect(tmp_path):
    assert summarize("短文本\r\n") == "短文本\r\n"
    assert not list(tmp_path.iterdir())
    assert summarize("x" * DEFAULT_LIMIT) == "x" * DEFAULT_LIMIT


def test_store_roundtrip_metadata_and_safe_filename(tmp_path):
    text = "完整原文\r\n第二行\n" * 900
    name = "分析/报告:" + "中" * 50
    ref = store_output(str(tmp_path), "task-123", name, text)
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in name)[:40]
    assert ref["rel"] == f"deliverables/subtasks/task-123_{safe}.md"
    assert Path(ref["path"]).is_absolute()
    assert Path(ref["path"]).read_bytes() == text.encode("utf-8")
    assert ref["chars"] == len(text)
    assert ref["sha256"] == hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
    assert resolve(str(tmp_path), ref["rel"]) == text
    assert resolve(str(tmp_path), ref["rel"], 17) == text[:17]
    summary = summarize(text, ref=ref)
    assert summary.startswith(text[:800])
    assert ref["rel"] in summary and str(len(text)) in summary
    assert len(summary) < DEFAULT_LIMIT


def test_long_summary_without_reference():
    text = "z" * 9000
    assert summarize(text).startswith(text[:800])
    assert "9000" in summarize(text)
    assert len(summarize(text, limit=12)) < 100


@pytest.mark.parametrize("rel", ["../x.md", "../shared-other/x.md", "/x.md",
                                  "C:\\x.md", "C:x.md", "\\\\host\\share\\x.md"])
def test_resolve_rejects_escape(tmp_path, rel):
    with pytest.raises(ValueError):
        resolve(str(tmp_path / "shared"), rel)


def test_symlink_escape_rejected_for_read_and_write(tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (shared / "deliverables").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("创建符号链接需要系统权限")
    with pytest.raises(ValueError):
        resolve(str(shared), "deliverables/x.md")
    with pytest.raises(ValueError):
        store_output(str(shared), "task", "name", "text")


def _monitor(text):
    state = PMState(task={"task_id": "task-123", "name": "主任务"},
                    plan={"decomposition": [{"name": "分析", "skill": "research"}]},
                    subtask_outputs={"分析": text})
    agent = SimpleNamespace(running=True, report_status=Mock(), report_progress=Mock(),
                            deliver_result=Mock(), _clear_snapshot=Mock())
    return PMMonitor("pm-123", object(), "http://secretary", state, agent, None)


def test_aggregation_prompt_has_summary_reference_and_full_delivery(tmp_path, monkeypatch):
    from lan_mesh import config, pm_monitor

    text = "开头证据\n" * 2000 + "ONLY_IN_FULL_OUTPUT"
    monitor = _monitor(text)
    monkeypatch.setattr(config, "get_shared_folder", lambda *args: tmp_path)
    monkeypatch.setattr(config, "load_config", lambda: None)
    captured = []

    def llm(runtime, prompt, *args):
        captured.append(prompt)
        return {"content": "聚合完成"}

    monkeypatch.setattr(pm_monitor, "call_for_task", llm)
    monitor.aggregate_results()
    rel = "deliverables/subtasks/task-123_分析.md"
    assert len(captured) == 1
    assert text[:800] in captured[0] and rel in captured[0]
    assert "ONLY_IN_FULL_OUTPUT" not in captured[0]
    assert resolve(str(tmp_path), rel) == text
    assert monitor._state.subtask_outputs["分析"] == text
    assert monitor._agent.deliver_result.call_args.args[3][0]["output"] == text
    assert monitor._state.subtask_outputs["_aggregated"] == "聚合完成"


def test_aggregation_survives_storage_failure(monkeypatch):
    from lan_mesh import config, pm_monitor

    text = "x" * 12000
    monitor = _monitor(text)
    monkeypatch.setattr(config, "load_config", lambda: None)
    monkeypatch.setattr(config, "get_shared_folder", lambda *args: "unused")
    store = Mock(side_effect=PermissionError("read-only"))
    monkeypatch.setattr(pm_monitor, "store_output", store)
    llm = Mock(return_value={"content": "正常聚合"})
    monkeypatch.setattr(pm_monitor, "call_for_task", llm)
    monitor.aggregate_results()
    store.assert_called_once_with("unused", "task-123", "分析", text)
    prompt = llm.call_args.args[1]
    assert text not in prompt and "12000" in prompt
    monitor._agent.deliver_result.assert_called_once()
    assert monitor._state.subtask_outputs["分析"] == text


@pytest.mark.parametrize("size", [0, DEFAULT_LIMIT - 1, DEFAULT_LIMIT])
def test_aggregation_short_output_skips_storage(tmp_path, monkeypatch, size):
    from lan_mesh import config, pm_monitor

    text = "x" * size
    monitor = _monitor(text)
    folder = Mock(return_value=tmp_path)
    monkeypatch.setattr(config, "get_shared_folder", folder)
    monkeypatch.setattr(config, "load_config", Mock(return_value=None))
    store = Mock(wraps=store_output)
    monkeypatch.setattr(pm_monitor, "store_output", store)
    llm = Mock(return_value={"content": "聚合完成"})
    monkeypatch.setattr(pm_monitor, "call_for_task", llm)
    build_prompt = Mock(wraps=pm_monitor.build_aggregation_prompt)
    monkeypatch.setattr(pm_monitor, "build_aggregation_prompt", build_prompt)

    monitor.aggregate_results()

    store.assert_not_called()
    folder.assert_not_called()
    assert not (tmp_path / "deliverables" / "subtasks").exists()
    assert build_prompt.call_args.args[2][0]["output"] == text
    assert monitor._agent.deliver_result.call_args.args[3][0]["output"] == text


@pytest.mark.parametrize("route", ["remote", "local", "remote_failure"])
@pytest.mark.parametrize("size", [DEFAULT_LIMIT, DEFAULT_LIMIT + 1, 12000])
def test_replan_dispatch_summarizes_dependency_outputs(tmp_path, monkeypatch, route, size):
    from lan_mesh import config

    text = "x" * (size - 4) + "TAIL"
    monitor = _monitor(text)
    state = monitor._state
    state.task["input_data"] = {"existing": "preserved"}
    short = "短依赖\r\n"
    state.subtask_outputs["短依赖"] = short
    sub = {"name": "重规划", "skill": "research", "description": "继续分析",
           "depends_on": ["分析", "短依赖"]}
    station = {} if route == "local" else {"url": "http://worker"}
    state.pending_subtasks[sub["name"]] = {
        "sub": sub, "station": station, "replanned": True}
    dispatcher = Mock()
    dispatcher._build_subagent_prompt_for_sub.side_effect = (
        lambda task, *args: "\n".join(task["input_data"]["_dependency_outputs"].values()))
    dispatcher._create_subagent_on_station.return_value = {"agent_id": "new-agent"}
    if route == "remote_failure":
        dispatcher._create_subagent_on_station.side_effect = RuntimeError("offline")
    monitor._dispatcher = dispatcher
    monkeypatch.setattr(config, "get_shared_folder", lambda *args: tmp_path)
    monkeypatch.setattr(config, "load_config", lambda: None)

    monitor._dispatch_replan_pending()

    if route == "remote":
        dispatched = dispatcher.dispatch_subtask.call_args.args[2]
        dispatcher.execute_subtask_locally.assert_not_called()
    else:
        dispatched = dispatcher.execute_subtask_locally.call_args.args[0]
        dispatcher.dispatch_subtask.assert_not_called()
    dependencies = dispatched["input_data"]["_dependency_outputs"]
    assert dependencies["短依赖"] == short
    assert dispatched["input_data"]["existing"] == "preserved"
    assert "_dependency_outputs" not in state.task["input_data"]
    assert state.subtask_outputs == {"分析": text, "短依赖": short}
    rel = "deliverables/subtasks/task-123_分析.md"
    if size > DEFAULT_LIMIT:
        assert dependencies["分析"] == summarize(text, ref={"rel": rel})
        assert "TAIL" not in dependencies["分析"]
        assert resolve(str(tmp_path), rel) == text
        assert len(list((tmp_path / "deliverables" / "subtasks").iterdir())) == 1
    else:
        assert dependencies["分析"] == text
        assert not (tmp_path / "deliverables" / "subtasks").exists()
    if route != "local":
        prompt = dispatcher._create_subagent_on_station.call_args.kwargs["system_prompt"]
        assert dependencies["分析"] in prompt
    assert sub["name"] in state.dispatched
    assert sub["name"] not in state.pending_subtasks
