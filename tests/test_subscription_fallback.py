"""Subscription entries must not mask API credentials or enter API model lists."""

import pytest

from lan_mesh import agent_runtime as policy, codex_plan
from lan_mesh.agent_runtime import AgentRuntime
from lan_mesh.config import ModelEntryConfig


@pytest.fixture
def mixed_runtime(tmp_path, monkeypatch):
    entries = [ModelEntryConfig(id='chatgpt-codex', provider='openai', api_key_env='',
                               usage_mode='chatgpt_plan', quality_score=0.99),
               ModelEntryConfig(id='gpt-api', provider='openai', api_key_env='REVIEW_KEY',
                               base_url='https://example.test/v1', quality_score=0.5)]
    monkeypatch.setattr(policy, '_load_model_pool_entries', lambda: {e.id: e for e in entries})
    monkeypatch.setattr(policy, '_ensure_env_loaded', lambda: None)
    monkeypatch.setenv('REVIEW_KEY', 'test-only')
    runtime = AgentRuntime('subscription-fallback', str(tmp_path))
    yield runtime
    runtime.services.close()


@pytest.mark.parametrize('available', [False, True])
def test_unavailable_or_failed_subscription_falls_back_once(mixed_runtime, monkeypatch, available):
    calls = []
    plan_calls = []
    monkeypatch.setattr(codex_plan, 'model_available', lambda entry: available)

    def fail_plan(*args):
        plan_calls.append(args)
        raise RuntimeError('subscription offline')

    def api(prompt, model, base_url, api_key, **kwargs):
        calls.append((model, base_url, api_key))
        return {'content': 'API ok', 'model': model}

    monkeypatch.setattr(codex_plan, 'call_plan', fail_plan)
    monkeypatch.setattr(mixed_runtime, '_call_openai_compatible', api)
    result = mixed_runtime._call_llm_full('hi')
    assert calls == [('gpt-api', 'https://example.test/v1', 'test-only')]
    assert result['content'] == 'API ok'
    assert len(plan_calls) == int(available)


def test_api_models_exclude_subscription_entries(mixed_runtime):
    assert mixed_runtime._get_provider_models('openai') == ['gpt-api']
    assert mixed_runtime._resolve_provider('gpt-api') == {
        'base_url': 'https://example.test/v1', 'api_key': 'test-only'}
    assert mixed_runtime._resolve_provider('chatgpt-codex')['usage_mode'] == 'chatgpt_plan'


def test_subscription_only_pool_has_no_api_defaults(mixed_runtime, monkeypatch):
    entry = policy._load_model_pool_entries()['chatgpt-codex']
    monkeypatch.setattr(policy, '_load_model_pool_entries', lambda: {entry.id: entry})
    assert mixed_runtime._get_provider_models('openai') == []
