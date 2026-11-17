"""Subscription usage must reach the runtime's own durable attempt ledger once."""

import json
import time
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID

import pytest

from lan_mesh import agent_runtime as policy, codex_plan, model_resources
from lan_mesh.agent_runtime import AgentRuntime
from lan_mesh.config import ModelEntryConfig
from lan_mesh.database import Database
from lan_mesh.model_resources import ModelResource, ModelResourceManager, usage_context
from lan_mesh.runtime_usage import attempt_scope, current_usage_metadata, record_external_usage


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(Database, 'backup', lambda *a, **k: '')
    db = Database(str(tmp_path / 'subscription.db'))
    manager = ModelResourceManager()
    manager._db, manager._enabled = db, True
    manager._resources = {'pool': ModelResource(
        'pool', 'openai', 'token_plan', 100000, models=['chatgpt-codex', 'gpt-api'])}
    runtime = AgentRuntime('subscription-metering', str(tmp_path), resource_manager=manager)
    entries = [ModelEntryConfig(id='chatgpt-codex', provider='openai', api_key_env='',
                               usage_mode='chatgpt_plan'),
               ModelEntryConfig(id='gpt-api', provider='openai', api_key_env='REVIEW_KEY',
                               base_url='https://example.test/v1')]
    monkeypatch.setattr(policy, '_load_model_pool_entries', lambda: {e.id: e for e in entries})
    monkeypatch.setattr(policy, '_ensure_env_loaded', lambda: None)
    monkeypatch.setattr(codex_plan, 'model_available', lambda entry: True)
    monkeypatch.setenv('REVIEW_KEY', 'test-only')
    global_record = Mock(side_effect=AssertionError('global manager must not be used'))
    monkeypatch.setattr(model_resources, 'record_usage_global', global_record)
    yield runtime, db, manager, global_record
    if vars(runtime).get('services'):
        runtime.services.close()
    if vars(manager).get('services'):
        manager.services.close()
    db._get_conn().close()


def invoke(runtime, mode):
    if mode == 'full':
        return runtime._call_llm_full('hi')
    preferences = {'_model_preference': 'chatgpt-codex', '_fallback_models': ['gpt-api']}
    if mode == 'routing':
        return runtime._call_llm_with_routing('hi', preferences)
    return runtime._call_llm_messages_with_tools([{'role': 'user', 'content': 'hi'}], [], preferences)


@pytest.mark.parametrize('mode', ['full', 'routing', 'tools'])
def test_subscription_success_is_queryable_once(ledger, monkeypatch, mode):
    runtime, db, manager, global_record = ledger
    api_call = Mock(side_effect=AssertionError('successful subscription must not call API'))
    monkeypatch.setattr(policy.requests, 'post', api_call)
    monkeypatch.setattr(codex_plan, 'call_plan', lambda *a: {
        'message': {'content': 'ok'}, 'model': 'chatgpt-codex',
        'input_tokens': 11, 'output_tokens': 3, 'usage_known': True})
    with usage_context('task', 'project'), attempt_scope('attempt', 'planning'):
        invoke(runtime, mode)
        assert current_usage_metadata() == {}
    summaries = db.repositories.resources.query_attempt_usage('task', 'project')
    assert summaries == [dict(attempt_id='attempt', purpose='planning', outcome='completed',
                              calls=1, reported_tokens=14, reported_cost=14.0, unknown_usage_calls=0)]
    rows = db.query_unreported_usage()
    assert len(rows) == 1 and rows[0]['model_id'] == 'chatgpt-codex'
    assert UUID(rows[0]['usage_id']).version == 4
    assert manager.get_usage('pool')['used'] == 14
    api_call.assert_not_called()
    global_record.assert_not_called()


@pytest.mark.parametrize('mode', ['full', 'routing', 'tools'])
def test_failed_subscription_and_api_fallback_have_distinct_ledger_rows(ledger, monkeypatch, mode):
    runtime, db, manager, global_record = ledger
    monkeypatch.setattr(codex_plan, 'call_plan', Mock(side_effect=RuntimeError('offline')))
    usage = {'prompt_tokens': 5, 'completion_tokens': 2}
    data = {'choices': [{'message': {'content': 'api ok'}, 'finish_reason': 'stop'}], 'usage': usage}
    stream = {'choices': [{'delta': {'content': 'api ok'}}], 'usage': usage}
    post = Mock(return_value=SimpleNamespace(
        raise_for_status=lambda: None, json=lambda: data, headers={'Content-Type': 'text/event-stream'},
        iter_lines=lambda **kw: iter(['data: ' + json.dumps(stream), 'data: [DONE]']), close=lambda: None))
    monkeypatch.setattr(policy.requests, 'post', post)
    with usage_context('task', 'project'), attempt_scope('attempt', 'execution'):
        invoke(runtime, mode)
    assert post.call_count == 1
    assert post.call_args.args[0] == 'https://example.test/v1/chat/completions'
    rows = db.query_unreported_usage()
    assert len(rows) == 2 and len({row['usage_id'] for row in rows}) == 2
    by_model = {row['model_id']: row for row in rows}
    assert by_model['chatgpt-codex']['usage_metadata']['outcome'] == 'failed'
    assert by_model['chatgpt-codex']['usage_metadata']['usage_known'] == 0
    assert by_model['gpt-api']['usage_metadata']['outcome'] == 'completed'
    summaries = db.repositories.resources.query_attempt_usage('task', 'project')
    assert {r['outcome']: (r['calls'], r['reported_tokens'], r['unknown_usage_calls'])
            for r in summaries} == {'failed': (1, 0, 1), 'completed': (1, 7, 0)}
    assert {r['attempt_id'] for r in summaries} == {'attempt'}
    assert {r['purpose'] for r in summaries} == {'execution'}
    assert manager.get_usage('pool')['used'] == 7
    global_record.assert_not_called()


def test_accounting_failure_warns_and_restores_scope(ledger, monkeypatch, caplog):
    runtime, _, _, _ = ledger
    monkeypatch.setattr(runtime, '_record_usage', Mock(side_effect=RuntimeError('db unavailable')))
    with attempt_scope('attempt', 'planning'):
        record_external_usage(runtime, 'chatgpt-codex', 11, 3)
        assert current_usage_metadata() == {}
    assert '用量记账失败' in caplog.text


def test_call_plan_does_not_write_global_usage(monkeypatch):
    events = iter([
        {'method': 'thread/tokenUsage/updated',
         'params': {'tokenUsage': {'last': {'inputTokens': 11, 'outputTokens': 3}}}},
        {'method': 'item/completed', 'params': {'item': {'type': 'agentMessage', 'text': 'ok'}}},
        {'method': 'turn/completed', 'params': {'turn': {'status': 'completed'}}},
    ])
    server = SimpleNamespace(seq=0, send=lambda event: None, receive=lambda: next(events),
                             request=lambda method, params: (
                                 {'account': {'type': 'chatgpt'}} if method == 'account/read'
                                 else {'thread': {'id': 'thread'}}))
    from contextlib import nullcontext
    monkeypatch.setattr(codex_plan, '_Server', lambda: nullcontext(server))
    global_record = Mock()
    monkeypatch.setattr(model_resources, 'record_usage_global', global_record)
    entry = ModelEntryConfig(id='chatgpt-codex', provider='openai', usage_mode='chatgpt_plan')
    assert codex_plan.call_plan(entry, [])['input_tokens'] == 11
    global_record.assert_not_called()


@pytest.mark.parametrize('mode', ['full', 'routing', 'tools'])
@pytest.mark.parametrize('usage_known', [False, True])
def test_zero_token_subscription_preserves_usage_known(ledger, monkeypatch, mode, usage_known):
    runtime, db, _, _ = ledger
    monkeypatch.setattr(codex_plan, 'call_plan', lambda *a: {
        'message': {'content': 'ok'}, 'model': 'chatgpt-codex',
        'input_tokens': 0, 'output_tokens': 0, 'usage_known': usage_known})
    with usage_context('task', 'project'), attempt_scope('attempt', 'execution'):
        invoke(runtime, mode)
    summary, = db.repositories.resources.query_attempt_usage('task', 'project')
    assert summary['unknown_usage_calls'] == int(not usage_known)
    assert summary['reported_tokens'] == 0
    row, = db.query_unreported_usage()
    assert row['usage_metadata']['usage_known'] == int(usage_known)


@pytest.mark.parametrize('failed', [False, True])
def test_subscription_trace_includes_transport_duration(ledger, monkeypatch, failed):
    from lan_mesh import runtime_trace
    from lan_mesh.runtime_llm_service import RuntimeLlmService

    runtime, db, _, _ = ledger
    trace = Mock()
    monkeypatch.setattr(runtime_trace, 'trace_llm_call', trace)

    def slow_call(*args):
        time.sleep(0.05)
        if failed:
            raise RuntimeError('offline')
        return {'message': {'content': 'ok'}, 'input_tokens': 0,
                'output_tokens': 0, 'usage_known': False}

    monkeypatch.setattr(codex_plan, 'call_plan', slow_call)
    entry = policy._load_model_pool_entries()['chatgpt-codex']
    service = SimpleNamespace(owner=runtime)
    if failed:
        with pytest.raises(RuntimeError, match='offline'):
            RuntimeLlmService._call_plan(service, entry, [])
    else:
        RuntimeLlmService._call_plan(service, entry, [])
    trace.assert_called_once()
    assert trace.call_args.kwargs['total_ms'] >= 40
    assert trace.call_args.kwargs['status'] == ('failed' if failed else 'completed')
    row, = db.query_unreported_usage()
    assert row['usage_metadata']['usage_known'] == 0


@pytest.mark.parametrize('tokens, outcome, explicit, expected', [
    (0, 'completed', None, False), (3, 'completed', None, True),
    (3, 'failed', None, False), (0, 'completed', True, True),
    (3, 'completed', False, False),
])
def test_external_usage_default_and_explicit_known(ledger, tokens, outcome, explicit, expected):
    runtime, db, _, _ = ledger
    record_external_usage(runtime, 'chatgpt-codex', tokens, 0,
                          outcome=outcome, usage_known=explicit)
    row, = db.query_unreported_usage()
    assert row['usage_metadata']['usage_known'] == int(expected)


@pytest.mark.parametrize('usage', [None, {}, {'inputTokens': 0},
                                  {'inputTokens': 0, 'outputTokens': 0},
                                  {'inputTokens': 11, 'outputTokens': 3}])
@pytest.mark.parametrize('tool_call', [False, True])
def test_call_plan_usage_known_requires_reported_counts(monkeypatch, usage, tool_call):
    from contextlib import nullcontext

    events = []
    if usage is not None:
        events.append({'method': 'thread/tokenUsage/updated',
                       'params': {'tokenUsage': {'last': usage}}})
    if tool_call:
        events.append({'method': 'item/tool/call',
                       'params': {'tool': 'dir_list', 'arguments': {}, 'callId': 'call'}})
    else:
        events.extend([
            {'method': 'item/completed', 'params': {'item': {'type': 'agentMessage', 'text': 'ok'}}},
            {'method': 'turn/completed', 'params': {'turn': {'status': 'completed'}}},
        ])
    pending = iter(events)
    server = SimpleNamespace(seq=0, send=lambda event: None, receive=lambda: next(pending),
                             request=lambda method, params: (
                                 {'account': {'type': 'chatgpt'}} if method == 'account/read'
                                 else {'thread': {'id': 'thread'}}))
    monkeypatch.setattr(codex_plan, '_Server', lambda: nullcontext(server))
    entry = ModelEntryConfig(id='chatgpt-codex', provider='openai', usage_mode='chatgpt_plan')
    result = codex_plan.call_plan(entry, [], [
        {'type': 'function', 'function': {'name': 'dir_list', 'parameters': {}}}])
    counts = usage or {}
    assert result['usage_known'] is ('inputTokens' in counts and 'outputTokens' in counts)
    assert result['input_tokens'] == counts.get('inputTokens', 0)
    assert result['output_tokens'] == counts.get('outputTokens', 0)
