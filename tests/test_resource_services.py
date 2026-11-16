"""Resource contracts, secret-free metadata and scoped execution accounting."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI

from lan_mesh.agent_runtime import AgentRuntime
from lan_mesh.model_resources import ModelResource, ModelResourceManager, _get_usage_context, set_usage_context
from lan_mesh.station_routes_resources import build_resource_routes


@pytest.mark.parametrize('fails', [False, True])
def test_custom_free_pool_never_falls_back_to_an_unconfigured_paid_provider(tmp_path, monkeypatch, fails):
    from lan_mesh import agent_runtime as policy
    from lan_mesh.config import ModelEntryConfig
    entry = ModelEntryConfig(id='free-test', provider='custom-free', api_key_env='QA_FREE_KEY',
                             base_url='http://free.invalid/v1')
    monkeypatch.setattr(policy, '_load_model_pool_entries', lambda: {'free-test': entry})
    monkeypatch.setattr(policy, '_ensure_env_loaded', lambda: None)
    monkeypatch.setenv('QA_FREE_KEY', 'qa-free')
    monkeypatch.setenv('ARK_API_KEY', 'must-not-use')
    runtime = AgentRuntime('free-test', str(tmp_path))
    seen = []
    def call(prompt, model_id, base_url, api_key, **kwargs):
        seen.append((model_id, base_url, api_key))
        if fails:
            raise RuntimeError('free provider unavailable')
        return {'content': 'PONG', 'model': model_id}
    monkeypatch.setattr(runtime, '_call_openai_compatible', call)
    try:
        result = runtime._call_llm_full('ping')
        assert seen == [('free-test', 'http://free.invalid/v1', 'qa-free')]
        assert result['model'] == ('none' if fails else 'free-test')
        if fails:
            assert result['_error'] is True
    finally:
        runtime.services.close()


def test_custom_free_tool_provider_works_without_an_explicit_model_preference(tmp_path, monkeypatch):
    from lan_mesh import agent_runtime as policy
    from lan_mesh.config import ModelEntryConfig
    entry = ModelEntryConfig(id='free-test', provider='custom-free', api_key_env='QA_FREE_KEY',
                             base_url='http://free.invalid/v1')
    monkeypatch.setattr(policy, '_load_model_pool_entries', lambda: {'free-test': entry})
    monkeypatch.setenv('QA_FREE_KEY', 'qa-free')
    runtime = AgentRuntime('free-tools', str(tmp_path))
    seen = []
    def call(messages, tools, model_id, base_url, api_key, max_tokens=4096):
        seen.append(model_id)
        return {'message': {'content': '5'}, 'model': model_id}
    monkeypatch.setattr(runtime, '_call_openai_with_tools', call)
    try:
        assert runtime._call_llm_messages_with_tools([], [], {})['model'] == 'free-test'
        assert seen == ['free-test']
    finally:
        runtime.services.close()


def test_resource_api_contract_snapshot():
    ctl=SimpleNamespace(db=None,state=None)
    app=FastAPI();app.include_router(build_resource_routes(ctl))
    try:
        expected=json.loads((Path(__file__).parent/'fixtures/resource_api_contract.json').read_text(encoding='utf-8'))
        assert app.openapi()==expected
    finally:
        ctl.services.close()


@pytest.mark.parametrize('raises', [False,True])
def test_execution_restores_thread_accounting_context(tmp_path, raises):
    runtime=AgentRuntime('accounting',str(tmp_path))
    seen=[]
    def handler(data):
        seen.append(_get_usage_context())
        if raises:raise RuntimeError('failure')
        return {'result':'ok'}
    runtime._handlers['accounting']=handler
    set_usage_context('outer','outer-project')
    try:
        result=runtime.execute({'required_skill':'accounting','parent_task_id':'inner','project_id':'inner-project'})
        assert result['status']==('failed' if raises else 'completed')
        assert seen==[('inner','inner-project')]
        assert _get_usage_context()==('outer','outer-project')
        assert getattr(runtime._local,'current_skill','')==''
    finally:
        set_usage_context()
        runtime.services.close()


def test_resource_metadata_and_repr_never_contain_key():
    manager=ModelResourceManager()
    manager._resources={'pool':ModelResource('pool','test','token_plan',100,api_key='test-secret-value')}
    try:
        assert manager.list_resources()[0]['id']=='pool'
        assert 'test-secret-value' not in repr(manager._resources['pool'])
        assert 'test-secret-value' not in json.dumps(manager.list_resources())
        assert 'test-secret-value' not in json.dumps(manager.services.describe())
    finally:
        manager.services.close()


def test_explicit_runtime_resource_owner_is_isolated(tmp_path):
    class Ledger:
        def __init__(self):self.records=[]
        def record_usage(self, *args):self.records.append(args);return {'tracked':True}
    left,right=Ledger(),Ledger()
    first=AgentRuntime('first',str(tmp_path),resource_manager=left)
    second=AgentRuntime('second',str(tmp_path),resource_manager=right)
    first._record_usage('a',10,20)
    second._record_usage('b',30,40)
    assert left.records==[('a',10,20)]
    assert right.records==[('b',30,40)]


def test_resource_report_target_url_is_business_argument():
    manager=ModelResourceManager()
    try:
        assert manager.set_report_target('http://example.test',token='private-token') is False
        assert manager._secretary_url=='http://example.test'
        assert manager._ws_url.startswith('ws://example.test/ws/worker')
    finally:
        manager.services.close()


@pytest.mark.parametrize('after_tool', [False, True])
@pytest.mark.parametrize('failure', [
    {'model': 'none', 'message': {'content': 'provider unavailable'}},
    {'model': 'free-test', '_error': True, 'message': {'content': 'rate limited'}},
])
def test_react_provider_failure_is_not_success(tmp_path, after_tool, failure):
    runtime = AgentRuntime('qa-provider-failure', str(tmp_path))
    responses = []
    if after_tool:
        responses.append({'model': 'free-test', 'message': {'tool_calls': [{
            'id': 'qa-read', 'function': {'name': 'dir_list', 'arguments': '{}'}}]}})
    responses.append(failure)
    runtime._call_llm_messages_with_tools = lambda *args: responses.pop(0)
    try:
        result = runtime.execute({'required_skill': 'react_agent',
                                  'input_data': {'requirement': 'Inspect test directory'}})
        assert result['status'] == 'failed'
        assert failure['message']['content'] in result['error']
        assert len(result['output']['steps']) == int(after_tool)
    finally:
        runtime.services.close()


@pytest.mark.parametrize('provider', ['zhipu', 'siliconflow', 'other'])
@pytest.mark.parametrize('enabled', [None, False, True])
def test_thinking_setting_reaches_stream_and_tool_transports(tmp_path, monkeypatch, provider, enabled):
    from lan_mesh import agent_runtime as policy
    from lan_mesh.config import ModelEntryConfig
    entry = ModelEntryConfig(id='qa-model', provider=provider, api_key_env='QA_KEY',
                             thinking_enabled=enabled)
    monkeypatch.setattr(policy, '_load_model_pool_entries', lambda: {'qa-model': entry})
    payloads = []
    class Response:
        headers = {'Content-Type': 'text/event-stream; charset=utf-8'}
        def raise_for_status(self): pass
        def close(self): pass
        def json(self): return {'choices': [{'message': {'content': 'PONG'}}]}
        def iter_lines(self, **kwargs):
            yield 'data: {"choices":[{"delta":{"content":"PONG"}}]}'
            yield 'data: [DONE]'
    def post(*args, **kwargs):
        payloads.append(kwargs['json'])
        return Response()
    monkeypatch.setattr(policy.requests, 'post', post)
    runtime = AgentRuntime('qa-thinking', str(tmp_path))
    try:
        runtime._call_openai_with_tools([], [], 'qa-model', 'http://qa.invalid', 'qa')
        result = runtime._call_openai_compatible('ping', 'qa-model', 'http://qa.invalid', 'qa')
        assert result['content'] == 'PONG'
        expected = {}
        if enabled is not None and provider == 'zhipu':
            expected = {'thinking': {'type': 'enabled' if enabled else 'disabled'}}
        elif enabled is not None and provider == 'siliconflow':
            expected = {'enable_thinking': enabled}
        assert len(payloads) == 2
        for payload in payloads:
            assert {k: payload[k] for k in ('thinking', 'enable_thinking') if k in payload} == expected
            assert payload['model'] == 'qa-model'
    finally:
        runtime.services.close()
