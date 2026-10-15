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


def test_chat_proposes_artifact_without_dispatch_and_uses_saved_document(chat):
    cid = chat._active_conv_id
    chat._execute_action = Mock()
    result = chat.chat('帮我做一个项目文档编辑器', cid)
    assert result['brief_revision'] == 0
    assert result['brief_proposal_id']
    assert result['action_taken'] == ''
    chat._execute_action.assert_not_called()
    pending = chat.briefs.get(cid)
    assert pending['document']['title'] == '待命名项目'
    assert 'AI 建议' in result['reply']
    proposal = pending['proposal']
    chat.briefs.review(cid, proposal['id'], 0, [change['id'] for change in proposal['changes']])
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
    chat.controller.submit_task_from_chat = Mock(side_effect=lambda **kw: {'task_id': kw['task_id'], 'status': 'pending'})
    chat._get_task_memory_hint = Mock(return_value='')
    chat._action_submit_task('下发项目描述')
    kwargs = chat.controller.submit_task_from_chat.call_args.kwargs
    assert '预算待 Boss 确认' in kwargs['description']
    assert 'flowchart LR' in kwargs['description']


def test_chat_handoff_cannot_replace_reviewed_document(chat):
    cid = chat._active_conv_id
    reviewed = {**document(), 'title': 'Boss 已审阅的版本'}
    chat.briefs.save(cid, reviewed, 0)
    chat.controller.submit_task_from_chat = Mock(side_effect=lambda **kw: {'task_id': kw['task_id'], 'status': 'pending'})
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
    updated = {**document(), 'title': '模型建议的新标题'}
    chat.runtime._call_llm_with_routing.return_value = {'content': json.dumps({'reply': '建议', 'project_brief': updated})}
    generated = client.post(base+'/generate', json={'expected_revision': 1}).json()
    assert generated['revision'] == 1
    assert generated['document']['title'] == '协作工作台'
    assert client.post(base+'/generate', json={'expected_revision': 1}).status_code == 409
    review = {'proposal_id': generated['proposal']['id'], 'expected_revision': 1, 'selected': ['title']}
    assert client.post(base+'/review', json={**review, 'selected': ['invalid']}).status_code == 422
    assert client.post(base+'/review', json=review).json()['revision'] == 2
    assert client.post(base+'/review', json=review).status_code == 409
    assert client.get('/api/conversations/missing/brief').status_code == 404
    chat.controller.secretary_active = False
    assert client.get(base).status_code == 503


def test_proposal_survives_restart_and_partial_review_keeps_unselected_fields(tmp_path):
    store = BriefStore(tmp_path)
    original = store.save('one', document(), 0)
    candidate = {**document(), 'title': 'AI 标题', 'sections': {'background': 'AI 背景'}}
    pending = store.propose('one', candidate, 1)
    assert pending['document'] == original['document']
    assert pending['markdown'] == original['markdown']
    assert BriefStore(tmp_path).get('one')['proposal'] == pending['proposal']
    assert store.get('two')['proposal'] is None
    result = store.review('one', pending['proposal']['id'], 1, ['section:background'])
    assert result['revision'] == 2 and result['proposal'] is None
    assert result['document']['title'] == '协作工作台'
    assert result['document']['sections']['background'] == 'AI 背景'
    assert result['document']['sections']['questions'] == '预算待 Boss 确认'


def test_pending_proposal_is_not_replaced_and_stale_proposal_cannot_overwrite_edits(tmp_path):
    store = BriefStore(tmp_path)
    store.save('one', document(), 0)
    proposal = store.propose('one', {**document(), 'title': 'AI'}, 1)['proposal']
    with pytest.raises(RevisionConflict, match='已有待审阅'):
        store.propose('one', {**document(), 'title': '第二份'}, 1)
    store.save('one', {**document(), 'title': 'Boss 手改'}, 1)
    with pytest.raises(RevisionConflict, match='旧版本'):
        store.review('one', proposal['id'], 2, ['title'])
    assert store.get('one')['document']['title'] == 'Boss 手改'
    discarded = store.review('one', proposal['id'], 2, [])
    assert discarded['revision'] == 2 and discarded['proposal'] is None


def test_review_atomic_failure_keeps_proposal_and_original(tmp_path, monkeypatch):
    store = BriefStore(tmp_path)
    pending = store.propose('one', document(), 0)
    monkeypatch.setattr('lan_mesh.project_brief.os.replace', Mock(side_effect=OSError('disk full')))
    with pytest.raises(OSError):
        store.review('one', pending['proposal']['id'], 0, ['title'])
    assert store.get('one') == pending


def test_concurrent_reviews_only_one_can_resolve_proposal(tmp_path):
    store = BriefStore(tmp_path)
    proposal = store.propose('one', document(), 0)['proposal']
    def review(_):
        try:
            return store.review('one', proposal['id'], 0, ['title'])['revision']
        except RevisionConflict:
            return 'conflict'
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(map(str, pool.map(review, range(2)))) == ['1', 'conflict']


def test_graph_structure_is_one_review_unit_but_node_fields_are_independent(tmp_path):
    store = BriefStore(tmp_path)
    original = store.save('one', document(), 0)
    candidate = document()
    candidate['nodes'] = [candidate['nodes'][-1]]
    candidate['edges'] = []
    proposal = store.propose('one', candidate, 1)['proposal']
    assert [change['id'] for change in proposal['changes']] == ['graph']
    store.review('one', proposal['id'], 1, [])
    candidate = original['document']
    candidate['nodes'][0].update(title='AI模块名', owner='Boss')
    proposal = store.propose('one', candidate, 1)['proposal']
    assert {change['id'] for change in proposal['changes']} == {'node:module:title', 'node:module:owner'}
    result = store.review('one', proposal['id'], 1, ['node:module:owner'])
    assert result['document']['nodes'][0]['title'] == '项目工作区'
    assert result['document']['nodes'][0]['owner'] == 'Boss'
    assert len(result['document']['nodes']) == 4


def test_noop_and_failed_generation_do_not_mutate_document(chat):
    cid = chat._active_conv_id
    original = chat.briefs.save(cid, document(), 0)
    assert chat.generate_project_brief(cid, 1) == original
    def concurrent_edit(*_):
        chat.briefs.save(cid, {**document(), 'title': '同时发生的编辑'}, 1)
        return {'content': json.dumps({'reply': '建议', 'project_brief': document()})}
    chat.runtime._call_llm_with_routing.side_effect = concurrent_edit
    with pytest.raises(RevisionConflict):
        chat.generate_project_brief(cid, 1)
    assert chat.briefs.get(cid)['proposal'] is None
    assert chat.briefs.get(cid)['document']['title'] == '同时发生的编辑'


def test_handoff_does_not_include_unaccepted_suggestion(chat):
    cid = chat._active_conv_id
    chat.briefs.save(cid, document(), 0)
    chat.briefs.propose(cid, {**document(), 'title': '未采纳的标题'}, 1)
    chat.controller.submit_task_from_chat = Mock(side_effect=lambda **kw: {'task_id': kw['task_id'], 'status': 'pending'})
    chat._get_task_memory_hint = Mock(return_value='')
    chat.chat('下发项目描述', cid)
    assert chat.controller.submit_task_from_chat.call_args.kwargs['name'] == '协作工作台'
    assert chat.briefs.get(cid)['proposal'] is not None


def test_revision_history_survives_restart_and_restore_creates_new_version(tmp_path):
    store = BriefStore(tmp_path)
    first = store.save('one', document(), 0)
    store.save('one', {**document(), 'title': '第二版'}, 1)
    restored = BriefStore(tmp_path).restore('one', 1, 2)
    assert restored['revision'] == 3
    assert restored['document'] == first['document']
    assert store.revision('one', 2)['document']['title'] == '第二版'
    entries = store.history('one')['revisions']
    assert [entry['revision'] for entry in entries] == [3, 2, 1]
    assert entries[0]['source'] == 'restore' and entries[0]['restored_from'] == 1
    assert store.history('two')['revisions'] == []
    with pytest.raises(KeyError):
        store.revision('two', 1)
    with pytest.raises(RevisionConflict):
        store.restore('one', 1, 2)


def test_legacy_document_history_is_captured_on_first_new_save(tmp_path):
    store = BriefStore(tmp_path)
    path = store._path('legacy')
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({'revision': 12, 'updated_at': 100,
                                'document': ProjectBrief.model_validate(document()).model_dump()}))
    assert store.history('legacy')['revisions'][0]['source'] == 'legacy'
    assert store.revision('legacy', 12)['document']['title'] == '协作工作台'
    store.save('legacy', {**document(), 'title': '新内容'}, 12)
    assert store.revision('legacy', 12)['document']['title'] == '协作工作台'
    assert store.revision('legacy', 13)['document']['title'] == '新内容'
    with pytest.raises(KeyError):
        store.revision('legacy', 11)


def test_failed_index_publish_does_not_expose_uncommitted_snapshot(tmp_path, monkeypatch):
    store = BriefStore(tmp_path)
    store.save('one', document(), 0)
    real_write = store._atomic_write
    def fail_current(path, text):
        if path.name == 'brief.json':
            raise OSError('disk full after snapshot')
        real_write(path, text)
    monkeypatch.setattr(store, '_atomic_write', fail_current)
    with pytest.raises(OSError):
        store.save('one', {**document(), 'title': '未提交'}, 1)
    assert len(store.history('one')['revisions']) == 1
    assert store.get('one')['revision'] == 1
    with pytest.raises(KeyError):
        store.revision('one', 2)
    monkeypatch.setattr(store, '_atomic_write', real_write)
    store.save('one', {**document(), 'title': '重试成功'}, 1)
    assert store.revision('one', 2)['document']['title'] == '重试成功'


def test_proposals_do_not_create_history_until_accepted_and_pagination_is_stable(tmp_path):
    store = BriefStore(tmp_path)
    pending = store.propose('one', document(), 0)
    assert store.history('one')['revisions'] == []
    store.review('one', pending['proposal']['id'], 0, ['title'])
    assert store.history('one')['revisions'][0]['source'] == 'ai_review'
    store.save('one', document(), 1)
    store.save('one', {**document(), 'title': '第三版'}, 2)
    first = store.history('one', limit=2)
    assert [entry['revision'] for entry in first['revisions']] == [3, 2]
    assert first['next_before'] == 2
    second = store.history('one', before_revision=2, limit=2)
    assert [entry['revision'] for entry in second['revisions']] == [1]
    assert second['next_before'] is None


def test_history_api_diff_restore_conflicts_and_isolation(chat):
    app = FastAPI()
    app.include_router(build_brief_routes(chat.controller))
    client = TestClient(app)
    cid = chat._active_conv_id
    base = f'/api/conversations/{cid}/brief'
    chat.briefs.save(cid, document(), 0)
    chat.briefs.save(cid, {**document(), 'title': '新标题'}, 1)
    assert len(client.get(base+'/history').json()['revisions']) == 2
    detail = client.get(base+'/history/1').json()
    assert detail['current_revision'] == 2
    assert detail['changes'][0]['id'] == 'title'
    assert detail['changes'][0]['before'] == '新标题'
    assert detail['changes'][0]['after'] == '协作工作台'
    assert client.get(base+'/history/99').status_code == 404
    assert client.post(base+'/history/1/restore', json={'expected_revision': 1}).status_code == 409
    assert client.post(base+'/history/99/restore', json={'expected_revision': 2}).status_code == 404
    assert client.post(base+'/history/1/restore', json={'expected_revision': 2}).json()['revision'] == 3
    assert client.get('/api/conversations/missing/brief/history').status_code == 404
    chat.controller.secretary_active = False
    assert client.get(base+'/history').status_code == 503


def test_handoff_freezes_document_and_links_task_before_dispatch(chat):
    from lan_mesh.brief_handoff import BriefHandoff
    cid = chat._active_conv_id
    saved = chat.briefs.save(cid, document(), 0)
    def submit(**kwargs):
        pending = chat.brief_handoffs.get(cid, 1)
        assert pending['status'] == 'prepared'
        assert pending['task_id'] == kwargs['task_id']
        assert kwargs['input_data']['_brief']['sha256'] == pending['sha256']
        return {'task_id': kwargs['task_id'], 'status': 'running', 'pm_agent_id': 'pm-test'}
    chat.controller.submit_task_from_chat = Mock(side_effect=submit)
    first = chat.brief_handoffs.dispatch(cid, 1)
    assert first['status'] == 'submitted' and first['task_status'] == 'running'
    assert first['markdown'] == saved['markdown']
    assert chat.brief_handoffs.dispatch(cid, 1) == first
    chat.controller.submit_task_from_chat.assert_called_once()
    chat.briefs.save(cid, {**document(), 'title': '后续改稿'}, 1)
    persisted = BriefHandoff(chat.briefs, chat.controller).get(cid, 1)
    assert persisted['title'] == '协作工作台' and persisted['markdown'] == saved['markdown']
    assert 'document' not in chat.brief_handoffs.list(cid)[0]


def test_handoff_failures_never_dispatch_without_record_or_retry_unknown_result(chat, monkeypatch):
    cid = chat._active_conv_id
    chat.briefs.save(cid, document(), 0)
    chat.controller.submit_task_from_chat = Mock(side_effect=TimeoutError('unknown outcome'))
    original = chat.brief_handoffs._write
    monkeypatch.setattr(chat.brief_handoffs, '_write', Mock(side_effect=OSError('full')))
    with pytest.raises(OSError):
        chat.brief_handoffs.dispatch(cid, 1)
    chat.controller.submit_task_from_chat.assert_not_called()
    monkeypatch.setattr(chat.brief_handoffs, '_write', original)
    result = chat.brief_handoffs.dispatch(cid, 1)
    assert result['status'] == 'uncertain'
    assert chat.brief_handoffs.dispatch(cid, 1)['task_id'] == result['task_id']
    chat.controller.submit_task_from_chat.assert_called_once()


def test_concurrent_handoff_only_one_dispatch_and_stale_revision_rejected(chat):
    cid = chat._active_conv_id
    chat.briefs.save(cid, document(), 0)
    chat.controller.submit_task_from_chat = Mock(side_effect=lambda **kw: {'task_id': kw['task_id'], 'status': 'pending'})
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: chat.brief_handoffs.dispatch(cid, 1), range(2)))
    assert results[0]['task_id'] == results[1]['task_id']
    chat.controller.submit_task_from_chat.assert_called_once()
    with pytest.raises(RevisionConflict):
        chat.brief_handoffs.dispatch(cid, 0)


def test_chat_handoff_uses_request_conversation_and_rejects_edit_during_llm(chat):
    cid = chat._active_conv_id
    other = chat.create_conversation('其他项目')['id']
    chat.briefs.save(cid, document(), 0)
    chat.briefs.save(other, {**document(), 'title': '其他会话'}, 0)
    chat.controller.submit_task_from_chat = Mock(side_effect=lambda **kw: {'task_id': kw['task_id'], 'status': 'pending'})
    def switch_during_reply(*_):
        chat._active_conv_id = other
        return {'content': '收到'}
    chat.runtime._call_llm_with_routing.side_effect = switch_during_reply
    chat.chat('下发项目描述', cid)
    assert chat.controller.submit_task_from_chat.call_args.kwargs['name'] == '协作工作台'
    assert chat.brief_handoffs.list(other) == []
    chat.controller.submit_task_from_chat.reset_mock()
    def edit_during_reply(*_):
        chat.briefs.save(other, {**document(), 'title': '尚未审阅的新稿'}, 1)
        return {'content': '收到'}
    chat.runtime._call_llm_with_routing.side_effect = edit_during_reply
    result = chat.chat('下发项目描述', other)
    assert '重新审阅' in result['reply']
    chat.controller.submit_task_from_chat.assert_not_called()


def test_real_controller_persists_brief_reference_even_when_no_worker(chat, tmp_path):
    from lan_mesh.database import Database
    from lan_mesh.station_controller import StationController
    cid = chat._active_conv_id
    chat.briefs.save(cid, document(), 0)
    controller = SimpleNamespace(db=Database(str(tmp_path / 'tasks.sqlite3')),
                                 state=SimpleNamespace(device_id='self'), _queue_ws_broadcast=Mock())
    chat.controller.db = controller.db
    chat.controller.submit_task_from_chat = lambda **kw: StationController.submit_task_from_chat(controller, **kw)
    result = chat.brief_handoffs.dispatch(cid, 1)
    task = controller.db.get_task(result['task_id'])
    assert task.input_data['_brief']['conv_id'] == cid
    assert task.input_data['_brief']['revision'] == 1
    assert task.status == 'failed'
    assert result['task_status'] == 'failed'
    assert '无可用' in result['error']
    task.status = 'delivered'
    controller.db.save_task(task)
    assert chat.brief_handoffs.get(cid, 1)['task_status'] == 'delivered'
    controller.db.delete_task(task.task_id)
    assert chat.brief_handoffs.get(cid, 1)['task_status'] == 'unavailable'
    assert chat.brief_handoffs.get(cid, 1)['markdown'].startswith('# 协作工作台')


def test_handoff_api_confirmation_conflict_and_isolation(chat):
    app = FastAPI()
    app.include_router(build_brief_routes(chat.controller))
    client = TestClient(app)
    cid = chat._active_conv_id
    base = f'/api/conversations/{cid}/brief/handoffs'
    assert client.post(base, json={'expected_revision': 0}).status_code == 422
    chat.briefs.save(cid, document(), 0)
    chat.controller.submit_task_from_chat = Mock(side_effect=lambda **kw: {'task_id': kw['task_id'], 'status': 'pending'})
    assert client.post(base, json={'expected_revision': 0}).status_code == 409
    first = client.post(base, json={'expected_revision': 1}).json()
    assert first['status'] == 'submitted'
    assert client.post(base, json={'expected_revision': 1}).json()['id'] == first['id']
    assert client.get(base).json()['handoffs'][0]['task_id'] == first['task_id']
    assert client.get(base+'/1').json()['markdown'].startswith('# 协作工作台')
    assert client.get(base+'/2').status_code == 404
    assert client.get('/api/conversations/missing/brief/handoffs').status_code == 404


def test_handoff_final_write_failure_keeps_task_reference_and_prevents_redispatch(chat, tmp_path, monkeypatch):
    from lan_mesh.database import Database
    from lan_mesh.protocol import Task
    cid = chat._active_conv_id
    chat.briefs.save(cid, document(), 0)
    chat.controller.db = Database(str(tmp_path / 'tasks.sqlite3'))
    def submit(**kw):
        task = Task(task_id=kw['task_id'], name=kw['name'], description=kw['description'],
                    input_data=kw['input_data'], status='running')
        chat.controller.db.save_task(task)
        return task.to_dict()
    chat.controller.submit_task_from_chat = Mock(side_effect=submit)
    write = chat.brief_handoffs._write
    def fail_final(conv_id, record):
        if record['status'] != 'prepared':
            raise OSError('disk full after dispatch')
        write(conv_id, record)
    monkeypatch.setattr(chat.brief_handoffs, '_write', fail_final)
    with pytest.raises(OSError):
        chat.brief_handoffs.dispatch(cid, 1)
    record = chat.brief_handoffs.dispatch(cid, 1)
    assert record['status'] == 'prepared'
    assert record['task_status'] == 'running'
    assert chat.controller.db.get_task(record['task_id']).input_data['_brief']['revision'] == 1
    chat.controller.submit_task_from_chat.assert_called_once()
