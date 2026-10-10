"""Artifact correctness: graph integrity, concurrency, isolation, and chat integration."""
import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from lan_mesh.chat_handler import ChatHandler
from lan_mesh.project_brief import BriefStore, ProjectBrief, RevisionConflict, SECTIONS, render_markdown
from lan_mesh.station_routes_briefs import build_brief_routes


def document():
    return {
        "title": "协作工作台",
        "sections": {"background": "把讨论沉淀为可执行需求", "questions": "预算待 Boss 确认"},
        "nodes": [
            {"id": "module", "title": "项目工作区"},
            {"id": "feature", "title": "文档编辑", "parent_id": "module", "kind": "feature"},
            {"id": "detail", "title": "草稿保存", "parent_id": "feature", "kind": "feature"},
            {"id": "export", "title": "Markdown 导出"},
        ],
        "edges": [{"source": "feature", "target": "export"}],
    }


def test_markdown_contains_sections_nested_graph_and_unresolved_items():
    brief = ProjectBrief.model_validate(document())
    text = render_markdown(brief, 3)
    for label in SECTIONS.values():
        assert f"## {label}" in text
    assert 'subgraph n0["项目工作区"]' in text
    assert 'subgraph n1["文档编辑"]' in text
    assert 'n2["草稿保存"]' in text
    assert "n1 --> n3" in text
    assert "预算待 Boss 确认" in text
    assert "版本 3" in text


@pytest.mark.parametrize('mutation,reason', [
    (lambda d: d['nodes'].append({'id': 'fourth', 'title': '第四层', 'parent_id': 'detail'}), '三层'),
    (lambda d: d['nodes'][0].update(parent_id='feature'), '包含关系不能成环'),
    (lambda d: d['nodes'][0].update(parent_id='absent'), '父节点不存在'),
    (lambda d: d['nodes'].append(d['nodes'][0]), 'ID 不可重复'),
    (lambda d: d['edges'].append({'source': 'export', 'target': 'feature'}), '不能形成循环'),
    (lambda d: d['edges'].append({'source': 'module', 'target': 'detail'}), '父子包含关系'),
    (lambda d: d['edges'].append({'source': 'export', 'target': 'absent'}), '不存在的节点'),
    (lambda d: d['edges'].append(d['edges'][0]), '重复依赖'),
    (lambda d: d['edges'].append({'source': 'export', 'target': 'export'}), '自连'),
])
def test_reject_invalid_graphs(mutation, reason):
    value = document()
    mutation(value)
    with pytest.raises(ValidationError, match=reason):
        ProjectBrief.model_validate(value)


def test_persistence_isolation_conflicts_and_atomic_failure(tmp_path, monkeypatch):
    store = BriefStore(tmp_path)
    stored = store.save('one', document(), 0)
    assert BriefStore(tmp_path).get('one') == stored
    assert store.get('two')['revision'] == 0
    with pytest.raises(RevisionConflict):
        store.save('one', {**document(), 'title': 'stale'}, 0)
    monkeypatch.setattr('lan_mesh.project_brief.os.replace', Mock(side_effect=OSError('disk full')))
    with pytest.raises(OSError):
        store.save('one', document(), 1)
    assert store.get('one') == stored
    assert not list(tmp_path.rglob('*.tmp'))
    with pytest.raises(ValueError):
        store.get('../escape')


def test_concurrent_edit_only_one_writer_wins(tmp_path):
    store = BriefStore(tmp_path)
    def edit(_):
        try:
            return store.save('conv', document(), 0)['revision']
        except RevisionConflict:
            return 'conflict'
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(map(str, pool.map(edit, range(2)))) == ['1', 'conflict']


@pytest.fixture
def chat(tmp_path):
    controller = SimpleNamespace(model_router=None, _default_model='', secretary_active=True)
    runtime = Mock()
    runtime._call_llm_with_routing.return_value = {
        'content': json.dumps({'reply': '已整理需求，预算待确认。', 'project_brief': document()}, ensure_ascii=False)
    }
    ch = ChatHandler(runtime, controller, shared_folder=SimpleNamespace(path=tmp_path))
    ch._build_status_context = lambda: ''
    ch._auto_title = Mock()
    controller.chat_handler = ch
    return ch


def test_chat_updates_artifact_without_dispatch_and_uses_saved_document(chat):
    cid = chat._active_conv_id
    chat._execute_action = Mock()
    result = chat.chat('帮我做一个项目文档编辑器', cid)
    assert result['brief_revision'] == 1
    assert result['action_taken'] == ''
    chat._execute_action.assert_not_called()
    assert chat.briefs.get(cid)['document']['title'] == '协作工作台'
    chat.runtime._call_llm_with_routing.return_value = {'content': '普通回复'}
    chat.chat('预算尚未确定', cid)
    assert '协作工作台' in chat.runtime._call_llm_with_routing.call_args.args[1]['_system_prompt']
    assert chat.briefs.get(cid)['revision'] == 1
    assert chat._detect_action('下发项目描述') == 'submit_task'
    assert chat._detect_action('请提交任务：实现编辑器') == 'submit_task'
    assert chat._detect_action('先不提交任务，讨论一下需求') != 'submit_task'


def test_invalid_ai_output_keeps_previous_document(chat):
    cid = chat._active_conv_id
    chat.briefs.save(cid, document(), 0)
    bad = document()
    bad['edges'].append({'source': 'export', 'target': 'feature'})
    chat.runtime._call_llm_with_routing.return_value = {'content': json.dumps({'reply': '建议', 'project_brief': bad})}
    result = chat.chat('补充方案', cid)
    assert '原稿已保留' in result['reply']
    assert chat.briefs.get(cid)['revision'] == 1


def test_explicit_handoff_uses_full_markdown(chat):
    cid = chat._active_conv_id
    chat.briefs.save(cid, document(), 0)
    chat.controller.submit_task_from_chat = Mock(return_value={'status': 'pending'})
    chat._get_task_memory_hint = Mock(return_value='')
    chat._action_submit_task('下发项目描述')
    kwargs = chat.controller.submit_task_from_chat.call_args.kwargs
    assert '预算待 Boss 确认' in kwargs['description']
    assert 'flowchart LR' in kwargs['description']


def test_chat_handoff_cannot_replace_reviewed_document(chat):
    cid = chat._active_conv_id
    reviewed = {**document(), 'title': 'Boss 已审阅的版本'}
    chat.briefs.save(cid, reviewed, 0)
    chat.controller.submit_task_from_chat = Mock(return_value={'status': 'pending'})
    chat._get_task_memory_hint = Mock(return_value='')
    result = chat.chat('下发项目描述', cid)
    assert result['action_taken'] == 'submit_task'
    assert chat.briefs.get(cid)['revision'] == 1
    assert chat.controller.submit_task_from_chat.call_args.kwargs['name'] == 'Boss 已审阅的版本'


def test_api_validation_export_generation_and_conversation_scope(chat):
    app = FastAPI()
    app.include_router(build_brief_routes(chat.controller))
    client = TestClient(app)
    base = f'/api/conversations/{chat._active_conv_id}/brief'
    assert client.get(base).json()['revision'] == 0
    payload = {'document': document(), 'expected_revision': 0}
    assert client.put(base, json=payload).status_code == 200
    assert client.put(base, json=payload).status_code == 409
    bad = document()
    bad['nodes'][0]['x'] = -5
    assert client.put(base, json={'document': bad, 'expected_revision': 1}).status_code == 422
    export = client.get(base+'/markdown')
    assert export.status_code == 200
    assert 'filename*=UTF-8' in export.headers['content-disposition']
    assert export.text == chat.briefs.get(chat._active_conv_id)['markdown']
    assert client.post(base+'/generate', json={'expected_revision': 1}).json()['revision'] == 2
    assert client.post(base+'/generate', json={'expected_revision': 1}).status_code == 409
    assert client.get('/api/conversations/missing/brief').status_code == 404
    chat.controller.secretary_active = False
    assert client.get(base).status_code == 503
