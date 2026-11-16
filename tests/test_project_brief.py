"""项目描述：存储、版本、审阅、导出与确认下发。"""
import json
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import unquote

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from lan_mesh.project_brief import (SECTIONS, BriefStore, ProjectBrief, RevisionConflict,
                                    render_markdown)
from lan_mesh.station_routes_briefs import build_brief_routes


def document(title="协作工作台"):
    return {
        "title": title,
        "sections": {"background": "把讨论沉淀成可执行需求", "questions": "预算待 Boss 确认"},
        "nodes": [
            {"id": "module", "title": "项目工作区"},
            {"id": "feature", "title": "文档编辑", "parent_id": "module", "kind": "feature"},
            {"id": "detail", "title": "草稿保存", "parent_id": "feature", "kind": "feature"},
            {"id": "export", "title": "Markdown 导出"},
        ],
        "edges": [{"source": "feature", "target": "export"}],
    }


class FakeChat:
    def __init__(self, root):
        self.root = root

    def _get_conv_meta(self, conv_id):
        return {"id": conv_id, "project_id": "proj-1"} if conv_id == "conv-1" else None

    def _get_conv_dir(self):
        return self.root


class FakeDB:
    def __init__(self):
        self.tasks = {"task-1": SimpleNamespace(status="pending", pm_agent_id="pm-1")}

    def get_task(self, task_id):
        return self.tasks.get(task_id)


@pytest.fixture
def store(tmp_path):
    return BriefStore(tmp_path / "conversations")


@pytest.fixture
def client(tmp_path):
    controller = SimpleNamespace(
        secretary_active=True, db=FakeDB(),
        submit_task_from_chat=Mock(return_value={"task_id": "task-1", "status": "pending"}))
    controller.chat_handler = FakeChat(tmp_path / "conversations")
    app = FastAPI()
    app.include_router(build_brief_routes(controller))
    return SimpleNamespace(client=TestClient(app), controller=controller)


def test_markdown_contains_sections_nested_graph_and_unresolved_items():
    text = render_markdown(ProjectBrief.model_validate(document()), 3)
    for label in SECTIONS.values():
        assert f"## {label}" in text
    assert "subgraph" in text
    assert "n1 --> n3" in text
    assert "预算待 Boss 确认" in text
    assert "版本 3" in text


@pytest.mark.parametrize("mutation, reason", [
    (lambda d: d["nodes"].append(dict(d["nodes"][0])), "节点 ID 不可重复"),
    (lambda d: d["edges"].append({"source": "export", "target": "feature"}), "DAG"),
    (lambda d: d["edges"].append({"source": "missing", "target": "export"}), "依赖边引用了不存在的节点"),
    (lambda d: d["nodes"].append({"id": "deep", "title": "第四层",
                                 "parent_id": "detail"}), "三层嵌套"),
])
def test_reject_invalid_graphs(mutation, reason):
    value = document()
    mutation(value)
    with pytest.raises(ValidationError, match=reason):
        ProjectBrief.model_validate(value)


def test_save_persists_isolates_and_rejects_stale_revision(store):
    assert store.get("conv-1")["revision"] == 0
    saved = store.save("conv-1", document(), 0)
    assert saved["revision"] == 1
    assert saved["markdown"].startswith("# 协作工作台")
    assert store.get("conv-1")["revision"] == 1
    assert store.get("conv-2")["revision"] == 0
    with pytest.raises(RevisionConflict):
        store.save("conv-1", document("另一版"), 0)
    assert store.get("conv-1")["document"]["title"] == "协作工作台"


def test_proposal_review_history_and_restore(store):
    store.save("conv-1", document(), 0)
    candidate = {**document("改过的标题"), "sections": {"background": "新背景", "questions": "预算待 Boss 确认"}}
    proposed = store.propose("conv-1", candidate, 1)
    assert proposed["revision"] == 1 and proposed["proposal"]
    assert store.get("conv-1")["document"]["title"] == "协作工作台"

    with pytest.raises(RevisionConflict):
        store.propose("conv-1", candidate, 1)

    reviewed = store.review("conv-1", proposed["proposal"]["id"], 1, ["title"])
    assert reviewed["revision"] == 2
    assert reviewed["document"]["title"] == "改过的标题"
    assert reviewed["document"]["sections"]["background"] == "把讨论沉淀成可执行需求"

    history = store.history("conv-1")
    assert [item["revision"] for item in history["revisions"]] == [2, 1]
    restored = store.restore("conv-1", 1, 2)
    assert restored["revision"] == 3
    assert restored["document"]["title"] == "协作工作台"


def test_review_rejects_unknown_selection(store):
    store.save("conv-1", document(), 0)
    proposed = store.propose("conv-1", document("改过的标题"), 1)
    with pytest.raises(ValueError, match="未知的变更项"):
        store.review("conv-1", proposed["proposal"]["id"], 1, ["not-a-field"])


def test_routes_get_save_conflict_scope_and_secretary_guard(client, tmp_path):
    http = client.client
    assert http.get("/api/conversations/conv-1/brief").json()["revision"] == 0
    saved = http.put("/api/conversations/conv-1/brief",
                     json={"document": document(), "expected_revision": 0})
    assert saved.status_code == 200 and saved.json()["revision"] == 1
    stale = http.put("/api/conversations/conv-1/brief",
                     json={"document": document("x"), "expected_revision": 0})
    assert stale.status_code == 409
    assert http.get("/api/conversations/missing/brief").status_code == 404

    exported = http.get("/api/conversations/conv-1/brief/markdown")
    assert exported.status_code == 200
    assert exported.headers["content-type"].startswith("text/markdown")
    assert "项目描述.md" in unquote(exported.headers["content-disposition"])
    assert "协作工作台" in exported.text

    readiness = http.get("/api/conversations/conv-1/brief/readiness").json()
    assert readiness["revision"] == 1
    assert any(item["id"].startswith("section:goals") for item in readiness["issues"])

    client.controller.secretary_active = False
    assert http.get("/api/conversations/conv-1/brief").status_code == 503


def test_routes_history_review_and_restore(client):
    http = client.client
    http.put("/api/conversations/conv-1/brief", json={"document": document(), "expected_revision": 0})
    store = BriefStore(client.controller.chat_handler._get_conv_dir())
    proposed = store.propose("conv-1", document("改过的标题"), 1)
    reviewed = http.post("/api/conversations/conv-1/brief/review", json={
        "proposal_id": proposed["proposal"]["id"], "expected_revision": 1, "selected": ["title"]})
    assert reviewed.status_code == 200 and reviewed.json()["revision"] == 2

    history = http.get("/api/conversations/conv-1/brief/history").json()
    assert [item["revision"] for item in history["revisions"]] == [2, 1]
    revision = http.get("/api/conversations/conv-1/brief/history/1").json()
    assert revision["document"]["title"] == "协作工作台"
    restored = http.post("/api/conversations/conv-1/brief/history/1/restore",
                         json={"expected_revision": 2})
    assert restored.status_code == 200 and restored.json()["revision"] == 3
    assert http.get("/api/conversations/conv-1/brief/history/99").status_code == 404


def test_handoff_dispatches_once_per_revision_and_records_task(client):
    http = client.client
    http.put("/api/conversations/conv-1/brief", json={"document": document(), "expected_revision": 0})
    first = http.post("/api/conversations/conv-1/brief/handoffs", json={"expected_revision": 1})
    assert first.status_code == 200
    record = first.json()
    assert record["status"] == "submitted" and record["task_id"] == "task-1"
    assert record["task_status"] == "pending" and record["pm_agent_id"] == "pm-1"
    kwargs = client.controller.submit_task_from_chat.call_args.kwargs
    assert kwargs["name"] == "协作工作台" and kwargs["input_data"]["_brief"]["revision"] == 1
    assert kwargs["project_id"] == "proj-1"
    assert kwargs["source_conv_id"] == "conv-1"

    again = http.post("/api/conversations/conv-1/brief/handoffs", json={"expected_revision": 1})
    assert again.status_code == 200 and again.json()["task_id"] == "task-1"
    assert client.controller.submit_task_from_chat.call_count == 1
    listed = http.get("/api/conversations/conv-1/brief/handoffs").json()["handoffs"]
    assert [item["revision"] for item in listed] == [1]


def test_handoff_requires_saved_revision(client):
    http = client.client
    response = http.post("/api/conversations/conv-1/brief/handoffs", json={"expected_revision": 0})
    assert response.status_code == 422
    client.controller.submit_task_from_chat.assert_not_called()


def test_handoff_records_uncertain_when_dispatch_raises(client):
    client.controller.submit_task_from_chat = Mock(side_effect=TimeoutError("timeout"))
    http = client.client
    http.put("/api/conversations/conv-1/brief", json={"document": document(), "expected_revision": 0})
    record = http.post("/api/conversations/conv-1/brief/handoffs", json={"expected_revision": 1}).json()
    assert record["status"] == "uncertain" and "timeout" in record["error"]


def test_saved_file_is_readable_json(store, tmp_path):
    store.save("conv-1", document(), 0)
    path = tmp_path / "conversations" / "project_briefs" / "conv-1" / "brief.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    assert value["revision"] == 1 and value["document"]["title"] == "协作工作台"
