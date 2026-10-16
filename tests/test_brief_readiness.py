"""Decision compatibility, advisory readiness and immutable handoff evidence."""
import json
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lan_mesh.brief_readiness import brief_readiness, missing_text
from lan_mesh.brief_review import brief_changes
from lan_mesh.project_brief import BriefStore, ProjectBrief
from lan_mesh.station_routes_briefs import build_brief_routes
from tests.test_project_brief import chat, document


def complete_document():
    doc = document()
    doc['sections'].update(goals='减少手工整理时间', scope='个人局域网', requirements='可保存并导出需求', acceptance='实测导出文件包含全部确认内容')
    for node in doc['nodes']:
        node.update(status='confirmed', owner='Boss', acceptance='对应功能经过浏览器验收')
    doc['nodes'].append({'id': 'question', 'title': '数据存储方案', 'kind': 'discussion',
                         'description': '离线数据存储在哪里', 'decision': '使用本地SQLite并定期备份',
                         'owner': 'Boss', 'status': 'confirmed'})
    return doc


def test_readiness_targets_required_sections_and_unconfirmed_nodes():
    report = brief_readiness({'revision': 4, 'document': document(), 'proposal': {'id': 'pending'}})
    ids = {item['id']: item for item in report['issues']}
    assert report['revision'] == 4
    assert {f'section:{key}' for key in ('goals', 'scope', 'requirements', 'acceptance')} <= ids.keys()
    assert ids['section:goals']['section'] == 'goals'
    assert ids['node:detail:acceptance']['node_id'] == 'detail'
    assert report['unconfirmed_nodes'] == 4
    assert 'proposal' in ids
    assert report['has_freeform_questions'] is True


@pytest.mark.parametrize('text', ['', '  ', '待讨论。', 'TBD', '待确认！'])
def test_placeholders_are_not_complete(text):
    assert missing_text(text)
    doc = complete_document()
    doc['sections']['acceptance'] = text
    assert 'section:acceptance' in {i['id'] for i in brief_readiness({'revision': 1, 'document': doc})['issues']}


def test_readiness_is_advisory_and_does_not_infer_freeform_resolution():
    doc = complete_document()
    report = brief_readiness({'revision': 1, 'document': doc})
    assert not report['issues']
    assert report['has_freeform_questions']
    assert '不代表质量达标' in report['notice']
    assert not missing_text('预算待讨论，但首版不接付费服务')
    doc['nodes'][-1]['decision'] = '待讨论'
    report = brief_readiness({'revision': 1, 'document': doc})
    assert report['unresolved_discussions'] == 1
    assert 'node:question:decision' in {i['id'] for i in report['issues']}


def test_decisions_are_reviewable_selectively_and_exported(chat):
    cid = chat._active_conv_id
    original = complete_document()
    saved = chat.briefs.save(cid, original, 0)
    candidate = saved['document']
    candidate['nodes'][-1]['decision'] = '改用纯文件归档'
    candidate['title'] = 'AI建议的新标题'
    pending = chat.briefs.propose(cid, candidate, 1)
    assert {'title', 'node:question:decision'} == {change['id'] for change in pending['proposal']['changes']}
    reviewed = chat.briefs.review(cid, pending['proposal']['id'], 1, ['node:question:decision'])
    assert reviewed['document']['title'] == original['title']
    assert '决策结论：改用纯文件归档' in reviewed['markdown']
    assert reviewed['revision'] == 2
    restored = chat.briefs.restore(cid, 1, 2)
    assert restored['document']['nodes'][-1]['decision'] == original['nodes'][-1]['decision']


def test_old_files_and_pending_proposals_gain_defaults_without_rewrite(tmp_path):
    store = BriefStore(tmp_path)
    before = ProjectBrief.model_validate(document()).model_dump()
    for node in before['nodes']:
        node.pop('decision')
    after = json.loads(json.dumps(before))
    after['title'] = '旧建议标题'
    path = store._path('old')
    path.parent.mkdir(parents=True)
    raw = {'revision': 1, 'updated_at': 1, 'document': before, 'proposal': {
        'id': 'old-proposal', 'base_revision': 1, 'created_at': 1, 'document': after,
        'changes': [{'id': 'title', 'label': '项目名称', 'before': before['title'], 'after': after['title']}]
    }}
    path.write_text(json.dumps(raw))
    disk = path.read_bytes()
    loaded = store.get('old')
    assert all(n['decision'] == '' for n in loaded['document']['nodes'])
    assert all(n['decision'] == '' for n in loaded['proposal']['document']['nodes'])
    assert path.read_bytes() == disk
    result = store.review('old', 'old-proposal', 1, ['title'])
    assert result['document']['title'] == '旧建议标题'
    assert store.revision('old', 1)['document']['nodes'][0]['decision'] == ''
    assert not brief_changes(before, loaded['document'])


def test_check_read_only_and_handoff_keeps_original_warnings(chat):
    cid = chat._active_conv_id
    chat.briefs.save(cid, document(), 0)
    chat.controller.submit_task_from_chat = Mock(side_effect=lambda **kw: {'task_id': kw['task_id'], 'status': 'pending'})
    app = FastAPI(); app.include_router(build_brief_routes(chat.controller))
    base = f'/api/conversations/{cid}/brief'
    with TestClient(app) as client:
        path = chat.briefs._path(cid)
        before = path.read_bytes()
        report = client.get(base+'/readiness').json()
        assert report['issues'] and path.read_bytes() == before
        chat.controller.submit_task_from_chat.assert_not_called()
        response = client.post(base+'/handoffs', json={'expected_revision': 1})
        assert response.status_code == 200  # Advice is not a new execution permission gate.
        assert response.json()['readiness'] == report
        chat.briefs.save(cid, complete_document(), 1)
        assert not client.get(base+'/readiness').json()['issues']
        assert client.get(base+'/handoffs/1').json()['readiness'] == report
        assert client.post(base+'/handoffs', json={'expected_revision': 1}).status_code == 409
        assert client.get('/api/conversations/missing/brief/readiness').status_code == 404
        chat.controller.secretary_active = False
        assert client.get(base+'/readiness').status_code == 503


def test_old_handoff_record_without_readiness_remains_readable(chat):
    cid = chat._active_conv_id
    chat.briefs.save(cid, document(), 0)
    chat.controller.submit_task_from_chat = Mock(side_effect=lambda **kw: {'task_id': kw['task_id']})
    record = chat.brief_handoffs.dispatch(cid, 1)
    path = chat.brief_handoffs._folder(cid) / '1.json'
    record.pop('readiness')
    path.write_text(json.dumps(record))
    assert chat.brief_handoffs.get(cid, 1)['task_id'] == record['task_id']
    assert chat.brief_handoffs.dispatch(cid, 1)['task_id'] == record['task_id']
    assert chat.controller.submit_task_from_chat.call_count == 1
