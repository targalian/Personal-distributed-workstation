"""Large source indexing, live PM scoping, pagination and runtime reference boundaries."""

import json
import threading
import tracemalloc
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lan_mesh.history_index import synchronize_history
from lan_mesh.memory_routes import build_memory_routes
from lan_mesh.pm_memory import attach_memory_scope, memory_reference, runtime_memory
from lan_mesh.protocol import Task
from tests.test_scoped_memory import chat, _call, _message, _create


def test_large_jsonl_index_is_bounded_and_preserves_last_source(chat: object) -> None:
    path = Path(chat._get_conv_dir()) / 'conv-a.jsonl'
    with path.open('w') as f:
        for i in range(16000):
            f.write(json.dumps({'role': 'user', 'content': f'条目{i}：' + '原始正文' * 100,
                               'timestamp': i}, ensure_ascii=False) + '\n')
    tracemalloc.start()
    report = synchronize_history(chat)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert report['indexed_messages'] == 16000
    assert peak < 8_000_000, peak  # Source is >19MB; no whole-file message/row lists.
    result = _call(chat, 'search_history', conv_id='conv-a', query='条目15999')
    assert result['items'][0]['seq'] == 16000
    assert _call(chat, 'read_source', conv_id='conv-a', anchor=result['items'][0]['anchor'])['content'].startswith('条目15999')
    assert synchronize_history(chat)['indexed_messages'] == 0


def test_index_midstream_failure_rolls_back_messages_and_fts(chat: object, monkeypatch: pytest.MonkeyPatch) -> None:
    import lan_mesh.history_index as history
    anchor = _message(chat, '索引原有内容')
    repo = chat._db.repositories.history
    marks = list(chat._db._get_conn().execute('SELECT fingerprint FROM context_streams'))
    original = history.iter_history_file

    def interrupted(path: Path, stats: dict) -> object:
        yield from original(path, stats)
        for i in range(501):
            yield dict(seq=100+i, role='user', content='不完整替换', timestamp=1)
        raise OSError('source disconnected')

    monkeypatch.setattr(history, 'iter_history_file', interrupted)
    with pytest.raises(OSError):
        synchronize_history(chat, force=True)
    assert repo.read_anchor('project-a', 'conv-a', '', anchor)['content'] == '索引原有内容'
    assert not repo.search('project-a', 'conv-a', '', '不完整')['items']
    assert [tuple(r) for r in chat._db._get_conn().execute('SELECT fingerprint FROM context_streams')] == [tuple(r) for r in marks]


def test_index_rejects_file_modified_during_final_batch(chat: object, monkeypatch: pytest.MonkeyPatch) -> None:
    import lan_mesh.history_index as history
    anchor = _message(chat, '稳定来源')
    original = history.iter_history_file

    def changing(path: Path, stats: dict) -> object:
        yield from original(path, stats)
        with path.open('a') as f:
            f.write('{}\n')

    monkeypatch.setattr(history, 'iter_history_file', changing)
    with pytest.raises(OSError, match='正在变化'):
        synchronize_history(chat, force=True)
    assert chat._db.repositories.history.read_anchor('project-a', 'conv-a', '', anchor)['content'] == '稳定来源'


def test_memory_paging_and_inactive_visibility(chat: object) -> None:
    anchor = _message(chat, '分页来源')
    records = [_create(chat, anchor, content=f'记忆{i}') for i in range(7)]
    first = _call(chat, 'list_memories', conv_id='conv-a', limit=3)
    second = _call(chat, 'list_memories', conv_id='conv-a', limit=3, offset=first['next_offset'])
    third = _call(chat, 'list_memories', conv_id='conv-a', limit=3, offset=second['next_offset'])
    assert third['next_offset'] is None
    assert {r['memory_id'] for page in (first, second, third) for r in page['items']} == {r['memory_id'] for r in records}


def test_pm_context_uses_persisted_assignment_and_rechecks_revocation(chat: object) -> None:
    shared = _create(chat, _message(chat, '项目偏好'), scope='project', content='项目使用中文')
    private = _create(chat, _message(chat, '私有偏好', agent='pm-a'), scope='agent', agent='pm-a', content='PM独立记忆')
    _create(chat, _message(chat, '另一项目', cid='conv-b'), cid='conv-b', content='不准跨项目')
    chat._db.save_task(Task(task_id='task', project_id='project-a', pm_agent_id='pm-a'))
    app = FastAPI(); app.include_router(build_memory_routes(chat.controller))
    with TestClient(app) as client:
        url = '/api/tasks/task/memory-context?pm_id=pm-a'
        items = client.get(url).json()['items']
        assert {r['memory_id'] for r in items} == {shared['memory_id'], private['memory_id']}
        assert client.get('/api/tasks/task/memory-context?pm_id=pm-b').status_code == 404
        _call(chat, 'revise_memory', conv_id='conv-a', memory_id=shared['memory_id'], payload={
            'operation': 'revoke', 'confirmed': True, 'expected_revision': 1})
        assert [r['memory_id'] for r in client.get(url).json()['items']] == [private['memory_id']]
        chat._conv_index[1]['pm_threads'] = [{'pm_id': 'pm-a'}]
        assert client.get(url).status_code == 409
        chat._conv_index[1]['pm_threads'] = []
        chat._db.save_task(Task(task_id='task', project_id='project-b', pm_agent_id='pm-a'))
        assert client.get(url).status_code == 409


def test_runtime_uses_owned_endpoint_and_never_cached_text(monkeypatch: pytest.MonkeyPatch) -> None:
    import lan_mesh.pm_memory as module
    seen = []
    def fetch(url: str, scope: dict) -> str:
        seen.append((url, scope)); return 'fresh reference'
    monkeypatch.setattr(module, 'memory_reference', fetch)
    owner = SimpleNamespace(_memory_secretary_url='http://secretary', _local=threading.local())
    task = attach_memory_scope({'task_id':'t', 'input_data':{'_scoped_memory':'obsolete'}}, 'pm')
    assert '_scoped_memory' not in task['input_data']
    data = dict(task['input_data'], _task_id='t', secretary_url='http://untrusted')
    assert runtime_memory(owner, data) == 'fresh reference'
    assert seen == [('http://secretary', {'task_id':'t', 'pm_id':'pm'})]
    assert runtime_memory(owner, dict(data, _task_id='other')) == ''
    owner._local.memory_secretary_url = 'http://trusted-dispatch-metadata'
    assert runtime_memory(owner, data) == 'fresh reference'
    assert seen[-1][0] == 'http://trusted-dispatch-metadata'


def test_reference_is_bounded_and_preserves_provenance(monkeypatch: pytest.MonkeyPatch) -> None:
    import lan_mesh.pm_memory as module
    body = {'scope':{'task_id':'t','pm_id':'pm'}, 'items':[
        {'effective_state':'active', 'content':'输出中文', 'memory_id':'m', 'revision':3,
         'source_anchor':'msg-a', 'source_conv_id':'conv-a', 'scope':'project'},
        {'effective_state':'revoked', 'content':'不要采用'},
        *[{'effective_state':'active', 'content':'a'*1000} for _ in range(20)]]}
    def get(url: str, **kwargs: object) -> object:
        assert kwargs['timeout']==10 and kwargs['allow_redirects'] is False
        return SimpleNamespace(status_code=200, json=lambda:body)
    monkeypatch.setattr(module.requests, 'get', get)
    text = memory_reference('http://secretary', {'task_id':'t','pm_id':'pm'})
    assert len(text)<5200 and '不是当前指令或操作授权' in text
    assert '不要采用' not in text and 'msg-a' in text and '"revision": 3' in text
    body['scope']['task_id']='wrong'
    assert memory_reference('http://secretary', {'task_id':'t','pm_id':'pm'}) == ''


def test_missing_secretary_returns_no_stale_reference(monkeypatch: pytest.MonkeyPatch) -> None:
    import lan_mesh.pm_memory as module
    def fail(*args: object, **kwargs: object) -> object:
        raise OSError('offline')
    monkeypatch.setattr(module.requests, 'get', fail)
    assert memory_reference('http://secretary', {'task_id':'t','pm_id':'pm'}) == ''


def test_auto_binding_uses_saved_conversation_after_active_switch(chat: object) -> None:
    from lan_mesh.station_local_pm import StationLocalPmMixin
    chat._active_conv_id = 'conv-b'
    owner = SimpleNamespace(chat_handler=chat)
    task = {'project_id':'project-a', 'input_data':{'_source_conv_id':'conv-a'}}
    StationLocalPmMixin._auto_attach_pm_thread(owner, 'pm-new', '执行任务', 'PM', task)
    assert chat.find_conv_by_pm('pm-new') == 'conv-a'
    StationLocalPmMixin._auto_attach_pm_thread(owner, 'pm-wrong', '执行任务', 'PM', {'project_id':'project-a'})
    assert not chat.find_conv_by_pm('pm-wrong')


def test_pm_waits_for_assignment_without_changing_requested_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    import lan_mesh.pm_memory as module
    calls = []
    def get(url: str, **kwargs: object) -> object:
        calls.append((url, kwargs['params']))
        return SimpleNamespace(status_code=404 if len(calls)==1 else 200, json=lambda:{
            'scope':{'task_id':'t','pm_id':'pm'}, 'items':[{'effective_state':'active','content':'成功读取'}]})
    monkeypatch.setattr(module.requests, 'get', get)
    assert '成功读取' in memory_reference('http://secretary', {'task_id':'t','pm_id':'pm'})
    assert calls[0] == calls[1] and len(calls)==2


@pytest.mark.parametrize('skill', ['react_agent', 'code_generation'])
def test_real_runtime_consumes_only_live_scoped_reference(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, skill: str) -> None:
    from lan_mesh.agent_runtime import AgentRuntime
    import lan_mesh.pm_memory as module
    runtime = AgentRuntime('agent', str(tmp_path))
    runtime._memory_secretary_url = 'http://secretary'
    available = [True]
    observed = []
    def get(*args: object, **kwargs: object) -> object:
        return SimpleNamespace(status_code=200, json=lambda:{
            'scope':{'task_id':'t','pm_id':'pm'}, 'items':[
                {'effective_state':'active','content':'本次有效记忆','source_anchor':'msg-1'}] if available[0] else []})
    def plain(prompt: str, **kw: object) -> dict:
        observed.append(kw['system_prompt'])
        return {'content':'完成','model':'test','input_tokens':0,'output_tokens':0}
    def react(messages: list, tools: list, data: dict) -> dict:
        observed.append(messages[0]['content'])
        return {'message':{'role':'assistant','content':'完成'},'model':'test','input_tokens':0,'output_tokens':0}
    monkeypatch.setattr(module.requests, 'get', get)
    monkeypatch.setattr(runtime, '_call_llm_full', plain)
    monkeypatch.setattr(runtime, '_call_llm_messages_with_tools', react)
    monkeypatch.setattr(runtime, '_record_usage', lambda *args:None)
    task = attach_memory_scope({'task_id':'t','input_data':{'requirement':'生成说明'}}, 'pm')
    subtask = {'parent_task_id':'t', 'required_skill':skill, 'input_data':task['input_data']}
    try:
        assert runtime.execute(subtask)['status']=='completed'
        assert '本次有效记忆' in observed[-1] and 'msg-1' in observed[-1]
        available[0]=False
        assert runtime.execute(subtask)['status']=='completed'
        assert '本次有效记忆' not in observed[-1]
    finally:
        runtime.services.close()
