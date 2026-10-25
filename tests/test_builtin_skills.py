"""Built-in skill discovery, distribution and real prompt composition behavior."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from lan_mesh.agent_runtime import AgentRuntime
from lan_mesh.database import Database
from lan_mesh.skill_knowledge import append_builtin_guidance, builtin_directories
from lan_mesh.skill_registry import SkillRegistry


@pytest.fixture
def registry(tmp_path):
    db = Database(str(tmp_path / 'skills.db'))
    registry = SkillRegistry(db, str(tmp_path / 'not-created'))
    registry.scan_and_register()
    yield registry
    registry.services.close()
    db._get_conn().close()


def test_packaged_skills_register_without_creating_installation_paths(registry):
    names = {directory.name for directory in builtin_directories()}
    assert names == {'yomkserver-modular', 'yomkserverpy-modular', 'lan-mesh-yomk'}
    assert not registry.skills_dir.exists()
    for role in ('station', 'secretary', 'pm', 'worker'):
        assert names <= {skill.skill_id for skill in registry.get_skills_for_role(role)}
    registry.scan_and_register()
    assert registry.stats()['total_skills'] == 3


def test_skill_package_contains_linked_examples_and_licenses(registry):
    package = {item['skill_id']: item for item in registry.build_skill_package('worker')}
    for name in ('yomkserver-modular', 'yomkserverpy-modular'):
        item = package[name]
        assert item['category'] == 'architecture'
        assert item['reference']
        assert item['resources']['examples.md']
        assert 'Apache' in item['resources']['LICENSE.txt']
        assert 'Yomk' in item['resources']['upstream-skill.md']
        assert 'github.com/Solitude-5309/YomkServer' in item['resources']['UPSTREAM.md']


@pytest.mark.parametrize('context,expected,absent', [
    ('用 YomkServerPy 开发 Python 服务', 'yomkserverpy-modular', 'yomkserver-modular'),
    ('用 YomkServer 开发 C++17 服务', 'yomkserver-modular', 'yomkserverpy-modular'),
    ('维护 lan_mesh 的组件退出流程', 'lan-mesh-yomk', 'yomkserver-modular'),
])
def test_task_matching_and_registry_selection(registry, context, expected, absent):
    prompt = append_builtin_guidance('角色约束', context)
    assert f'## 工作站内置技能: {expected}' in prompt
    assert f'## 工作站内置技能: {absent}' not in prompt
    assert append_builtin_guidance(prompt, context) == prompt
    registry_prompt = registry.build_system_prompt('pm', context)
    assert f'# 技能: {expected}' in registry_prompt
    assert f'# 技能: {absent}' not in registry_prompt


@pytest.mark.parametrize('context', ['', '总结今天的天气', '用 Python 写普通计算器'])
def test_unrelated_tasks_do_not_receive_yomk_guidance(registry, context):
    assert append_builtin_guidance('原有角色约束', context) == '原有角色约束'
    assert registry.build_system_prompt('worker', context) == ''


def test_runtime_without_cache_keeps_pm_prompt_and_adds_relevant_skill(tmp_path):
    runtime = AgentRuntime('skill-test', str(tmp_path), custom_system_prompt='PM 团队约束')
    runtime._skills_cache_dir = tmp_path / 'no-cache'
    try:
        assert runtime._build_system_prompt('普通摘要') == 'PM 团队约束'
        prompt = runtime._build_system_prompt('重构工作站 lan_mesh 服务')
        assert prompt.startswith('PM 团队约束')
        assert 'ComponentService' in prompt and 'request_sync' in prompt
        assert 'YomkApi.server()' in prompt
    finally:
        runtime.services.close()


@pytest.mark.parametrize('model', ['', 'test-model'])
def test_external_system_prompt_gets_guidance_on_both_llm_routes(tmp_path, monkeypatch, model):
    runtime = AgentRuntime('skill-test', str(tmp_path))
    captured = []

    def fake_call(*args, **kwargs):
        captured.append(kwargs['system_prompt'])
        return {'content': 'ok'}

    monkeypatch.setattr(runtime, '_resolve_provider', lambda _: {'base_url': 'http://unused', 'api_key': ''})
    monkeypatch.setattr(runtime, '_call_openai_compatible', fake_call)
    monkeypatch.setattr(runtime, '_call_llm_full', fake_call)
    try:
        runtime._call_llm_with_routing('实现服务', {
            '_system_prompt': '秘书身份', '_model_preference': model,
            'description': '使用 YomkServerPy 实现服务'})
        assert captured[0].startswith('秘书身份')
        assert '## 工作站内置技能: yomkserverpy-modular' in captured[0]
    finally:
        runtime.services.close()


def test_cached_builtins_do_not_leak_into_unrelated_tasks(tmp_path):
    runtime = AgentRuntime('skill-test', str(tmp_path))
    runtime._skills_cache_dir = tmp_path / 'cache'
    skill_dir = runtime._skills_cache_dir / 'yomkserver-modular'
    skill_dir.mkdir(parents=True)
    (skill_dir / 'SKILL.md').write_text('NEVER LOAD FOR WEATHER', encoding='utf-8')
    try:
        assert runtime._build_system_prompt('天气摘要') == ''
        assert 'NEVER LOAD' not in runtime._build_system_prompt('Yomk C++ 服务')
    finally:
        runtime.services.close()


def test_worker_receives_all_references_but_ignores_resource_traversal(registry, tmp_path, monkeypatch):
    import lan_mesh.worker as worker_module
    package = registry.build_skill_package('worker')
    package[0]['resources']['../escape.md'] = 'must not write'
    worker = worker_module.WorkerAgent.__new__(worker_module.WorkerAgent)
    worker.state = SimpleNamespace(secretary_ip='test.invalid', secretary_port=1234,
                                   device_id='skill-worker')
    response = Mock(status_code=200)
    response.json.return_value = package
    get = Mock(return_value=response)
    monkeypatch.setattr(worker_module.requests, 'get', get)
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    worker._pull_skills()
    cache = tmp_path / '.lan_mesh/skills_cache'
    assert (cache / 'yomkserverpy-modular/examples.md').is_file()
    assert (cache / 'yomkserver-modular/reference.md').is_file()
    assert not (cache / 'escape.md').exists()
    assert get.call_args.kwargs['timeout'] == 10


def test_secretary_supplies_message_as_skill_selection_context(tmp_path, monkeypatch):
    from lan_mesh.chat_handler import ChatHandler
    runtime = AgentRuntime('skill-test', str(tmp_path))
    captured = []
    def fake_call(prompt, system_prompt=''):
        captured.append(system_prompt)
        return {'content': 'ok'}
    monkeypatch.setattr(runtime, '_call_llm_full', fake_call)
    handler = ChatHandler.__new__(ChatHandler)
    handler.runtime = runtime
    handler._resolve_chat_model_pref = lambda _: ('', [])
    try:
        assert handler._call_chat_llm('开发工作站服务', '问题与历史', '秘书身份') == 'ok'
        assert '## 工作站内置技能: lan-mesh-yomk' in captured[0]
    finally:
        runtime.services.close()
