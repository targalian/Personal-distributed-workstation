"""Behavioral checks for model windows, durable tool evidence and scoped recovery."""

import copy
import json
import re
import subprocess
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lan_mesh.agent_runtime import AgentRuntime
from lan_mesh.config import ModelEntryConfig
from lan_mesh.runtime_artifact_routes import build_runtime_artifact_routes
from lan_mesh.runtime_artifacts import RuntimeArtifacts, artifact_root
from lan_mesh.runtime_context import ReactContextBudget
from lan_mesh.runtime_model_budget import ModelWindowBudget, estimate_request_tokens
from lan_mesh.shared_folder import SharedFolderManager
from lan_mesh.tool_registry import ToolRegistry, _tool_http_request, _tool_shell_exec


@pytest.fixture
def runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    value = AgentRuntime('archive-agent', str(tmp_path))
    monkeypatch.setattr(value, '_build_system_prompt', lambda _: '必须保留规则')
    monkeypatch.setattr(value, '_record_usage', lambda *a: None)
    yield value
    value.services.close()


def _reply(index: Any=0, name: Any='dir_list', args: Any='{}') -> Any:
    return {'message': {'role': 'assistant', 'content': '', 'tool_calls': [
        {'id': f'call-{index}', 'type': 'function', 'function': {'name': name, 'arguments': args}}]},
        'input_tokens': 11, 'output_tokens': 3, 'model': 'test'}


def _store(result: Any, folder: Any) -> Any:
    execution = result['execution']
    return RuntimeArtifacts(artifact_root(str(folder)), execution['execution_id'], execution['scope'])


def _record(store: Any, reference: Any) -> Any:
    return json.loads((store.directory / (reference['artifact_id'] + '.json')).read_text())


def _scope() -> Any:
    return dict(agent_id='a', task_id='t', project_id='p', attempt_id='attempt')


def test_model_budget_reserves_reply_and_accounts_for_unicode_and_tools() -> None:
    messages = [{'role': 'user', 'content': '中文🙂' * 300}]
    tools = [{'type': 'function', 'function': {'description': '中文' * 100}}]
    window = ModelWindowBudget.resolve(SimpleNamespace(max_context_tokens=4096, max_output_tokens=700), {})
    fitted, report = window.prepare(messages, tools, {})
    assert fitted is None and not report['accepted']
    assert report['estimated_input_tokens'] > len(json.dumps(messages, ensure_ascii=False))
    assert report['input_budget'] + report['reply_tokens'] + report['safety_tokens'] == 4096
    assert report['reply_tokens'] == 700 and report['configured_model']
    assert ModelWindowBudget.resolve(None, {}).known_model is False


@pytest.mark.parametrize('setting', [None, 'bad', float('inf'), -3, 100000])
def test_small_window_reserve_is_always_bounded(setting: Any) -> None:
    budget = ModelWindowBudget.resolve(SimpleNamespace(max_context_tokens=1024), {'reply_max_tokens': setting})
    assert 1 <= budget.reply_tokens <= 512
    assert budget.window - budget.reply_tokens - budget.safety_tokens > 0


def test_model_config_rejects_impossible_limits_and_accepts_old_entries() -> None:
    fields = dict(id='local', provider='local', api_key_env='LOCAL_MODEL_KEY')
    assert ModelEntryConfig(**fields).max_output_tokens == 4096
    for key in ('max_context_tokens', 'max_output_tokens'):
        with pytest.raises(ValueError):
            ModelEntryConfig(**fields, **{key: 0})


def test_fallback_checks_each_window_without_mutating_input(runtime: AgentRuntime, monkeypatch: pytest.MonkeyPatch) -> None:
    import lan_mesh.agent_runtime as policy
    pool = {name: SimpleNamespace(max_context_tokens=size, max_output_tokens=512)
            for name, size in [('small', 1024), ('large', 16000)]}
    monkeypatch.setattr(policy, '_load_model_pool_entries', lambda: pool)
    monkeypatch.setattr(runtime, '_resolve_provider', lambda _: dict(base_url='http://unused', api_key=''))
    sent = []
    def request(messages: Any, tools: Any, model: Any, base: Any, key: Any, reply: Any) -> Any:
        sent.append((copy.deepcopy(messages), model, reply))
        return {'message': {'content': 'done'}}
    monkeypatch.setattr(runtime, '_call_openai_with_tools', request)
    original = [{'role': 'system', 'content': '规则'}, {'role': 'user', 'content': '中文要求' * 300}]
    messages = copy.deepcopy(original)
    response = runtime._call_llm_messages_with_tools(messages, [], {
        '_model_preference': 'small', '_fallback_models': ['small', 'large']})
    assert sent == [(original, 'large', 512)] and messages == original
    assert [r['accepted'] for r in response['context_candidates']] == [False, True]


def _long_context() -> Any:
    messages = [{'role': 'system', 'content': '规则不可修改'}, {'role': 'user', 'content': '必须保留要求'}]
    for i in range(8):
        messages.append(_reply(i, args=json.dumps({'query': 'x' * 1800}))['message'])
        messages.append({'role': 'tool', 'tool_call_id': f'call-{i}', 'content': f'证据{i}'})
    return messages


def test_compaction_preserves_requirements_latest_pair_and_checkpoint() -> None:
    messages = _long_context()
    original = copy.deepcopy(messages)
    budget = ReactContextBudget(max_chars=6500, checkpoint_ref='a' * 64)
    assert budget.fit(messages, []) and budget.compacted_rounds > 0
    assert messages[:2] == original[:2] and messages[-2:] == original[-2:]
    marker = next(m for m in messages if m.get('content', '').startswith('[执行检查点参考]'))
    assert '证据' in marker['content'] and 'a' * 64 in marker['content']
    calls = [c['id'] for m in messages for c in m.get('tool_calls', [])]
    assert calls == [m['tool_call_id'] for m in messages if m['role'] == 'tool']
    assert budget.request_size(messages, []) <= 6500


def test_without_checkpoint_or_with_unshrinkable_requirements_context_is_unchanged() -> None:
    messages = _long_context()
    original = copy.deepcopy(messages)
    assert not ReactContextBudget(max_chars=6500).fit(messages, [])
    assert messages == original
    messages.insert(2, {'role': 'user', 'content': '用户追加' * 3000})
    original = copy.deepcopy(messages)
    assert not ReactContextBudget(max_chars=4096, checkpoint_ref='a' * 64).fit(messages, [])
    assert messages == original


def test_incomplete_tool_pairs_cannot_be_discarded_by_compaction() -> None:
    messages = _long_context()
    messages[3]['tool_call_id'] = 'mismatched'
    budget = ReactContextBudget(max_chars=9000, checkpoint_ref='a' * 64)
    assert budget.fit(messages, [])
    assert any(m.get('tool_call_id') == 'mismatched' for m in messages)
    assert any(c['id'] == 'call-0' for m in messages for c in m.get('tool_calls', []))


def test_complete_multiblock_result_survives_truncation_cancel_and_restart(runtime: AgentRuntime, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    raw = {'content': [{'type': 'text', 'text': 'HEAD' + '中🙂' * 15000},
                       {'type': 'image', 'data': 'binary-reference'}, {'type': 'text', 'text': 'TAIL'}],
           'result': {'structured': '保留'}, 'isError': False}
    monkeypatch.setattr(runtime, '_call_llm_messages_with_tools', lambda *a: _reply())
    def tool(*args) -> Any:
        runtime.cancel()
        return raw
    monkeypatch.setattr(ToolRegistry, 'call_tool', tool)
    result = runtime._handle_react_agent({'tool_result_max_chars': 512, '_task_id': 'task'})
    assert result['status'] == 'cancelled' and result['usage']['input_tokens'] == 11
    store = _store(result, tmp_path)
    step = result['steps'][0]
    assert _record(store, step['artifact'])['payload']['result'] == raw
    assert store.inspect()['status'] == 'cancelled'
    full, offset = '', 0
    while offset is not None:
        page = store.read(step['artifact']['artifact_id'], offset, 1237)
        full += page['content']
        offset = page['next_offset']
    assert full == raw['content'][0]['text'] + '\nTAIL'
    checkpoint = _record(store, result['checkpoint'])
    assert checkpoint['payload']['messages'][-1]['content'] == full


def test_model_can_reread_archived_middle_through_registered_tool(runtime: AgentRuntime, monkeypatch: pytest.MonkeyPatch) -> None:
    original_call = ToolRegistry.call_tool
    calls = []
    def tool(registry: Any, name: Any, args: Any) -> Any:
        if name == 'dir_list':
            return {'content': [{'type': 'text', 'text': 'x' * 20000 + '中间证据' + 'y' * 20000}]}
        return original_call(registry, name, args)
    def response(messages: Any, tools: Any, data: Any) -> Any:
        calls.append(copy.deepcopy(messages))
        if len(calls) == 1:
            return _reply()
        if len(calls) == 2:
            artifact_id = re.search(r'artifact_id=([a-f0-9]{64})', messages[-1]['content'])[1]
            return _reply(1, 'artifact_read', json.dumps(dict(artifact_id=artifact_id, offset=20000, limit=4)))
        assert '中间证据' in messages[-1]['content']
        return {'message': {'content': '已核对中间证据'}}
    monkeypatch.setattr(ToolRegistry, 'call_tool', tool)
    monkeypatch.setattr(runtime, '_call_llm_messages_with_tools', response)
    result = runtime._handle_react_agent({'tool_result_max_chars': 512})
    assert result['status'] == 'completed' and len(result['steps']) == 2


@pytest.mark.parametrize('phase,executed', [('before_model', 0), ('tool_started', 0),
                                         ('tool_result', 1), ('tool_observed', 1), ('finished', 1)])
def test_archive_failure_stops_further_side_effects_and_preserves_usage(runtime: AgentRuntime, monkeypatch: pytest.MonkeyPatch, phase: Any, executed: Any) -> None:
    original = RuntimeArtifacts.save
    calls = []
    def fail(store: Any, kind: Any, payload: Any) -> Any:
        if phase == kind or payload.get('phase') == phase:
            raise OSError('disk full')
        return original(store, kind, payload)
    monkeypatch.setattr(RuntimeArtifacts, 'save', fail)
    monkeypatch.setattr(runtime, '_call_llm_messages_with_tools', lambda *a: _reply())
    monkeypatch.setattr(ToolRegistry, 'call_tool', lambda *a: calls.append(1) or {'content': []})
    result = runtime._handle_react_agent({'max_iterations': 1})
    assert result['status'] == 'failed' and len(calls) == executed
    assert result['usage']['input_tokens'] == (0 if phase == 'before_model' else 11)
    if phase == 'tool_result':
        assert result['review_required'] and result['pending_call']['id'] == 'call-0'


def test_atomic_failure_keeps_prior_manifest_and_cleans_temporary_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import lan_mesh.runtime_artifacts as artifacts
    store = RuntimeArtifacts.create(str(tmp_path), _scope())
    original = store.inspect()
    monkeypatch.setattr(artifacts.os, 'replace', lambda *a: (_ for _ in ()).throw(OSError('disk full')))
    with pytest.raises(OSError):
        store.update('completed')
    assert store.inspect() == original and not list(store.directory.glob('.pending-*'))


def test_checksum_scope_run_isolation_and_symlink_guards(tmp_path: Path) -> None:
    store = RuntimeArtifacts.create(str(tmp_path), _scope())
    reference = store.save('tool_result', {'observation': 'evidence'})
    other = RuntimeArtifacts.create(str(tmp_path), _scope())
    with pytest.raises(FileNotFoundError):
        other.read(reference['artifact_id'])
    wrong_scope = {**_scope(), 'project_id': 'different'}
    with pytest.raises(PermissionError):
        RuntimeArtifacts(artifact_root(str(tmp_path)), store.execution_id, wrong_scope).inspect()
    path = store.directory / (reference['artifact_id'] + '.json')
    path.write_text('tampered')
    with pytest.raises(ValueError, match='校验'):
        store.read(reference['artifact_id'])
    path.unlink()
    path.symlink_to(store.directory / 'run.json')
    with pytest.raises(ValueError, match='符号链接'):
        store.read(reference['artifact_id'])
    with pytest.raises(ValueError):
        RuntimeArtifacts(artifact_root(str(tmp_path)), '../escape', _scope())


def test_authenticated_api_requires_exact_scope_and_valid_pagination(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import lan_mesh.station_routes_common as common
    store = RuntimeArtifacts.create(str(tmp_path), _scope())
    reference = store.save('tool_result', {'observation': '中文证据'})
    token = uuid.uuid4().hex
    for name, value in [('_mesh_auth_enabled', True), ('_mesh_auth_token', token),
                        ('_API_KEY', ''), ('_user_tokens', {})]:
        monkeypatch.setattr(common, name, value)
    owner = SimpleNamespace(state=SimpleNamespace(shared_folder=SimpleNamespace(path=tmp_path)))
    app = FastAPI()
    app.middleware('http')(common.api_guard_middleware)
    app.include_router(build_runtime_artifact_routes(owner))
    base = f'/api/runtime/artifacts/{store.execution_id}'
    try:
        with TestClient(app) as client:
            assert client.get(base, params=_scope()).status_code == 401
            client.headers['Authorization'] = f'Bearer {token}'
            assert client.get(base).status_code == 422
            assert client.get(base, params=_scope()).json()['status'] == 'running'
            assert client.get(base, params={**_scope(), 'task_id': 'different'}).status_code == 404
            path = base + '/' + reference['artifact_id']
            assert client.get(path, params={**_scope(), 'limit': 2}).json()['content'] == '中文'
            assert client.get(path, params={**_scope(), 'limit': 16001}).status_code == 400
    finally:
        owner.services.close()


def test_shared_files_and_cloud_sync_cannot_expose_or_overwrite_artifacts(tmp_path: Path) -> None:
    from lan_mesh.cloud_sync import CloudTransferService
    from lan_mesh.component_service import call_component
    folder = SharedFolderManager(str(tmp_path))
    store = RuntimeArtifacts.create(str(tmp_path), _scope())
    relative = f'.runtime_artifacts/{store.execution_id}/run.json'
    with pytest.raises(ValueError):
        folder.get_file(relative)
    (tmp_path / 'alias').symlink_to(store.directory, target_is_directory=True)
    with pytest.raises(ValueError):
        folder.get_file('alias/run.json')
    assert not any(item['name'] == 'alias' for item in folder.list_files())
    ordinary = tmp_path / 'ordinary.txt'
    ordinary.write_text('shared')
    uploaded, downloaded = [], []
    owner = SimpleNamespace(local_path=tmp_path, bucket='bucket',
                            _client=SimpleNamespace(upload_file=lambda *a: uploaded.append(a),
                                                    download_file=lambda *a: downloaded.append(a)),
                            _cloud_key=lambda x: x, _list_cloud_objects=lambda: {
                                relative: {'key': relative, 'mtime': 99999999999},
                                'alias/run.json': {'key': 'alias/run.json', 'mtime': 99999999999}})
    try:
        assert call_component(owner, CloudTransferService, '_upload_all')[0] == 1
        assert call_component(owner, CloudTransferService, '_download_all')[0] == 0
        assert uploaded[0][0] == str(ordinary) and downloaded == []
    finally:
        owner.services.close()


def test_http_and_shell_timeout_preserve_full_observations(monkeypatch: pytest.MonkeyPatch) -> None:
    import requests
    import lan_mesh.tool_registry as registry
    text = '中文🙂' * 10000
    monkeypatch.setattr(requests, 'request', lambda *a, **k: SimpleNamespace(status_code=200, headers={}, text=text))
    assert _tool_http_request({'url': 'http://unused'})['body'] == text
    def timeout(*args, **kwargs) -> Any:
        raise subprocess.TimeoutExpired('test', 1, output='已输出'.encode(), stderr=b'partial-error')
    monkeypatch.setattr(registry.subprocess, 'run', timeout)
    result = _tool_shell_exec({'command': 'unused'})
    assert result['stdout'] == '已输出' and result['stderr'].startswith('partial-error') and result['timed_out']


def test_long_react_task_fits_actual_requests_and_recovers_originals(runtime: AgentRuntime, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import lan_mesh.agent_runtime as policy
    window = SimpleNamespace(max_context_tokens=18000, max_output_tokens=1024)
    monkeypatch.setattr(policy, '_load_model_pool_entries', lambda: {'test': window})
    monkeypatch.setattr(runtime, '_resolve_provider', lambda _: dict(base_url='http://unused', api_key=''))
    monkeypatch.setattr(ToolRegistry, 'call_tool', lambda *a: {'content': [{'type': 'text', 'text': '数据' * 8000}]})
    requests = []
    def post(url: Any, **kwargs) -> Any:
        payload = kwargs['json']
        assert estimate_request_tokens(payload['messages'], payload['tools']) + payload['max_tokens'] + 900 <= 18000
        requests.append(copy.deepcopy(payload))
        message = (_reply(len(requests), args=json.dumps({'query': '参数' * 400}))['message']
                   if len(requests) <= 12 else {'content': '已完成验证'})
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {
            'choices': [{'message': message, 'finish_reason': 'stop'}],
            'usage': {'prompt_tokens': 11, 'completion_tokens': 3}})
    monkeypatch.setattr(policy.requests, 'post', post)
    result = runtime._handle_react_agent({'_model_preference': 'test', 'requirement': '保留原始目标'})
    assert result['status'] == 'completed' and len(requests) == 13
    assert any(r['compacted_rounds'] for r in result['model_requests'])
    assert result['usage']['input_tokens'] == 143
    store = _store(result, tmp_path)
    checkpoint = _record(store, result['checkpoint'])['payload']
    assert len([m for m in checkpoint['messages'] if m['role'] == 'tool']) == 12
    assert checkpoint['messages'][1]['content'] == '保留原始目标'
    for step in result['steps']:
        assert _record(store, step['artifact'])['payload']['observation'] == '数据' * 8000
    saved_requests = [json.loads(p.read_text()) for p in store.directory.glob('*.json') if p.name != 'run.json']
    assert len([r for r in saved_requests if r['kind'] == 'model_request']) == 13


def test_cancel_during_pretool_checkpoint_prevents_side_effect(runtime: AgentRuntime, monkeypatch: pytest.MonkeyPatch) -> None:
    original = RuntimeArtifacts.checkpoint
    calls = []
    def cancel(store: Any, messages: Any, state: Any, **details: Any) -> dict:
        reference = original(store, messages, state, **details)
        if details.get('phase') == 'tool_started':
            runtime.cancel()
        return reference
    monkeypatch.setattr(RuntimeArtifacts, 'checkpoint', cancel)
    monkeypatch.setattr(runtime, '_call_llm_messages_with_tools', lambda *a: _reply())
    monkeypatch.setattr(ToolRegistry, 'call_tool', lambda *a: calls.append(a))
    result = runtime._handle_react_agent({})
    assert result['status'] == 'cancelled' and calls == [] and not result['review_required']


def test_task_envelope_overrides_untrusted_artifact_scope(runtime: AgentRuntime, monkeypatch: pytest.MonkeyPatch) -> None:
    import lan_mesh.runtime_trace as trace
    monkeypatch.setattr(trace, 'trace_subtask_start', lambda **kw: 'test-trace')
    monkeypatch.setattr(trace, 'trace_subtask_end', lambda *a, **kw: None)
    monkeypatch.setattr(runtime, '_call_llm_messages_with_tools', lambda *a: {'message': {'content': 'done'}})
    result = runtime.execute(dict(required_skill='react_agent', parent_task_id='trusted-task',
                                  project_id='trusted-project', attempt_id='trusted-attempt', input_data={
                                      '_task_id': 'fake', '_project_id': 'fake', '_attempt_id': 'fake'}))
    assert result['status'] == 'completed'
    assert result['output']['execution']['scope'] == dict(agent_id=runtime.agent_id,
            task_id='trusted-task', project_id='trusted-project', attempt_id='trusted-attempt')
