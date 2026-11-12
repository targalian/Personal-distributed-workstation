"""Behavioral regressions for bounded context, honest outcomes and tool execution."""

import copy
from types import SimpleNamespace

import pytest

from lan_mesh.agent_runtime import AgentRuntime, infer_result_status
from lan_mesh.runtime_context import ReactContextBudget
from lan_mesh.tool_registry import ToolRegistry, _tool_file_read


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    import lan_mesh.runtime_trace as trace
    monkeypatch.setattr(trace, 'trace_subtask_start', lambda **kw: 'test-trace')
    monkeypatch.setattr(trace, 'trace_subtask_end', lambda *a, **kw: None)
    value = AgentRuntime('reliability', str(tmp_path))
    value._skills_cache_dir = tmp_path / 'skills'
    yield value
    value.services.close()


def tool_reply(name='dir_list', arguments='{}', call_id='call-1'):
    return {'message': {'role': 'assistant', 'content': '', 'tool_calls': [
        {'id': call_id, 'type': 'function', 'function': {'name': name, 'arguments': arguments}},
    ]}, 'model': 'fake', 'input_tokens': 11, 'output_tokens': 3}


def execute(runtime, skill='react_agent', **data):
    return runtime.execute({'required_skill': skill, 'input_data': data})


@pytest.mark.parametrize('payload', [
    {'status': 'max_iterations_reached'}, {'_error': True, 'content': 'offline'},
    {'returncode': 2, 'stderr': 'missing file'}, {'timed_out': True},
])
def test_structured_failure_signals_are_not_success(payload):
    status, error = infer_result_status(payload)
    assert status == 'failed' and error


@pytest.mark.parametrize('skill,field', [
    ('code_generation', 'code'), ('code_review', 'review'), ('document_summary', 'summary'),
])
def test_model_failure_survives_skill_and_execution_facades(runtime, monkeypatch, skill, field):
    monkeypatch.setattr(runtime, '_call_llm_with_routing', lambda *a: {
        'content': 'provider unavailable', '_error': True, 'model': 'none',
        'input_tokens': 0, 'output_tokens': 0})
    result = execute(runtime, skill, requirement='work')
    assert result['status'] == 'failed'
    assert result['error'] == 'provider unavailable'
    assert result['output'][field] == 'provider unavailable'


def test_unconfigured_model_fails_at_real_routing_boundary(runtime, monkeypatch):
    monkeypatch.setattr(runtime, '_resolve_provider', lambda _: None)
    for skill in ('react_agent', 'code_generation'):
        result = runtime.execute({'required_skill': skill, 'model_preference': 'missing',
                                  'input_data': {'requirement': 'work'}})
        assert result['status'] == 'failed'
        assert 'API Key' in result['error']


def test_fallback_visits_each_model_once(runtime, monkeypatch):
    calls = []
    monkeypatch.setattr(runtime, '_resolve_provider', lambda _: {'base_url': '', 'api_key': ''})
    def fail(messages, tools, model, *args):
        calls.append(model)
        raise RuntimeError('offline')
    monkeypatch.setattr(runtime, '_call_openai_with_tools', fail)
    result = runtime._call_llm_messages_with_tools([], [], {
        '_model_preference': 'a', '_fallback_models': ['a', 'b', 'b']})
    assert calls == ['a', 'b']
    assert result['status'] == 'failed'


def test_transport_preserves_finish_reason_and_http_timeout(runtime, monkeypatch):
    import lan_mesh.agent_runtime as policy
    captured = {}
    def post(url, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {
            'choices': [{'message': {'content': 'partial'}, 'finish_reason': 'length'}],
            'usage': {'prompt_tokens': 12, 'completion_tokens': 4}})
    monkeypatch.setattr(policy.requests, 'post', post)
    monkeypatch.setattr(runtime, '_record_usage', lambda *a: None)
    result = runtime._call_openai_with_tools([], [], 'fake', 'http://unused', '')
    assert result['finish_reason'] == 'length'
    assert captured['timeout'] == 10
    assert result['input_tokens'] == 12


@pytest.mark.parametrize('response', [
    {'message': {'content': ''}},
    {'message': {'content': 'partial'}, 'finish_reason': 'length'},
    {'message': {'content': 'blocked'}, 'finish_reason': 'content_filter'},
])
def test_incomplete_final_response_is_failed(runtime, monkeypatch, response):
    monkeypatch.setattr(runtime, '_call_llm_messages_with_tools', lambda *a: response)
    assert execute(runtime, requirement='work')['status'] == 'failed'


def test_iteration_exhaustion_is_failed_and_keeps_usage(runtime, monkeypatch):
    monkeypatch.setattr(runtime, '_call_llm_messages_with_tools', lambda *a: tool_reply())
    monkeypatch.setattr(ToolRegistry, 'call_tool', lambda *a: {'content': []})
    result = execute(runtime, max_iterations=1)
    assert result['status'] == 'failed'
    assert result['output']['stop_reason'] == 'iteration_limit'
    assert result['usage']['input_tokens'] == 11


def test_cancel_during_model_wait_prevents_tool_side_effects(runtime, monkeypatch):
    invoked = []
    def response(*args):
        runtime.cancel()
        return tool_reply('file_write', '{"path":"unwanted","content":"write"}')
    monkeypatch.setattr(runtime, '_call_llm_messages_with_tools', response)
    monkeypatch.setattr(ToolRegistry, 'call_tool', lambda *a: invoked.append(a))
    result = runtime._handle_react_agent({})
    assert result['status'] == 'cancelled' and invoked == []
    assert result['usage']['input_tokens'] == 11


@pytest.mark.parametrize('arguments', ['{bad', 'null', '[]', '"string"'])
def test_invalid_tool_arguments_never_run_with_defaults(runtime, monkeypatch, arguments):
    invoked = []
    monkeypatch.setattr(runtime, '_call_llm_messages_with_tools', lambda *a: tool_reply(arguments=arguments))
    monkeypatch.setattr(ToolRegistry, 'call_tool', lambda *a: invoked.append(a))
    result = execute(runtime, max_iterations=1)
    assert invoked == []
    assert result['output']['steps'][0]['is_error'] is True


def test_three_identical_failed_rounds_stop_without_using_entire_budget(runtime, monkeypatch):
    count = []
    def response(*args):
        count.append(1)
        return tool_reply(call_id=str(len(count)))
    monkeypatch.setattr(runtime, '_call_llm_messages_with_tools', response)
    monkeypatch.setattr(ToolRegistry, 'call_tool', lambda *a: {
        'content': [{'type': 'text', 'text': 'not found'}], 'isError': True})
    result = execute(runtime, max_iterations=30)
    assert len(count) == 3
    assert result['output']['stop_reason'] == 'repeated_tool_failure'
    assert result['status'] == 'failed'


def test_successful_repeated_observations_are_not_failure_loops(runtime, monkeypatch):
    replies = iter([tool_reply() for _ in range(4)] + [{'message': {'content': 'done'}}])
    monkeypatch.setattr(runtime, '_call_llm_messages_with_tools', lambda *a: next(replies))
    monkeypatch.setattr(ToolRegistry, 'call_tool', lambda *a: {'content': []})
    result = execute(runtime)
    assert result['status'] == 'completed' and result['output']['iterations'] == 5


def test_tool_catalog_marks_nonzero_exit_and_explicit_error():
    registry = ToolRegistry()
    try:
        for value in ({'returncode': 1}, {'error': 'denied'}, {'timed_out': True}):
            registry._tools['fake'] = {'handler': lambda args, value=value: value}
            assert registry.call_tool('fake', {})['isError'] is True
        registry._tools['fake'] = {'handler': lambda args: {'returncode': 0}}
        assert registry.call_tool('fake', {})['isError'] is False
    finally:
        registry.services.close()


def test_context_pressure_preserves_requirements_schema_and_tool_pairs():
    original = [{'role': 'system', 'content': 'rules/blueprint'},
                {'role': 'user', 'content': 'original constraints'}]
    for i in range(5):
        original.append(tool_reply(call_id=str(i))['message'])
        original.append({'role': 'tool', 'tool_call_id': str(i), 'content': '首' * 4000 + '尾'})
    messages = copy.deepcopy(original)
    schema = [{'type': 'function', 'function': {'name': 'dir_list'}}]
    budget = ReactContextBudget(max_chars=6000)
    assert budget.fit(messages, schema)
    assert messages[:2] == original[:2]
    assert [m for m in messages if m['role'] == 'assistant'] == [m for m in original if m['role'] == 'assistant']
    assert [m['tool_call_id'] for m in messages if m['role'] == 'tool'] == [str(i) for i in range(5)]
    assert budget.request_size(messages, schema) <= 6000
    assert '已裁剪' in messages[3]['content'] and messages[3]['content'].endswith('尾')


def test_oversized_required_context_stops_before_any_llm_call(runtime, monkeypatch):
    calls = []
    monkeypatch.setattr(runtime, '_call_llm_messages_with_tools', lambda *a: calls.append(a))
    result = execute(runtime, requirement='不可丢失' * 10000, context_max_chars=4096)
    assert calls == []
    assert result['output']['stop_reason'] == 'context_limit'


def test_large_multi_block_tool_output_is_bounded_in_actual_requests(runtime, monkeypatch):
    captured = []
    def response(messages, tools, data):
        captured.append(copy.deepcopy(messages))
        return tool_reply() if len(captured) == 1 else {'message': {'content': 'done'}}
    monkeypatch.setattr(runtime, '_call_llm_messages_with_tools', response)
    monkeypatch.setattr(ToolRegistry, 'call_tool', lambda *a: {'content': [
        {'type': 'text', 'text': 'HEAD' + 'x' * 50000}, {'type': 'text', 'text': 'TAIL'}]})
    result = execute(runtime, tool_result_max_chars=1024)
    observation = captured[1][-1]['content']
    assert len(observation) <= 1024 and observation.startswith('HEAD') and observation.endswith('TAIL')
    assert result['output']['context']['removed_chars'] > 48000
    assert result['status'] == 'completed'


def test_file_pages_reconstruct_unicode_and_keep_legacy_contract(tmp_path):
    path = tmp_path / 'text.txt'
    content = '中文🙂abcdef' * 1000
    path.write_text(content, encoding='utf-8')
    assert _tool_file_read({'path': str(path)}) == {'path': str(path), 'content': content}
    offset, pages = 0, []
    while offset is not None:
        page = _tool_file_read({'path': str(path), 'offset': offset, 'limit': 137})
        pages.append(page['content'])
        offset = page['next_offset']
    assert ''.join(pages) == content
    assert _tool_file_read({'path': str(path), 'offset': 999999})['content'] == ''


@pytest.mark.parametrize('options', [{'offset': -1}, {'limit': 0}, {'limit': 32001}])
def test_invalid_file_page_limits_fail(tmp_path, options):
    path = tmp_path / 'text.txt'
    path.write_text('data')
    with pytest.raises(ValueError):
        _tool_file_read({'path': str(path), **options})


def write_skill(runtime, name, body):
    directory = runtime._skills_cache_dir / name
    directory.mkdir(parents=True)
    (directory / 'SKILL.md').write_text(body, encoding='utf-8')


def test_skill_catalog_does_not_inject_unrelated_body(runtime):
    write_skill(runtime, 'deploy', '---\nname: deploy\ndescription: deployment\n---\nUNRELATED_RULE')
    prompt = runtime._build_system_prompt('summarize weather')
    assert 'UNRELATED_RULE' not in prompt and 'deploy/SKILL.md' in prompt
    runtime._local.current_skill = 'deploy'
    assert 'UNRELATED_RULE' in runtime._build_system_prompt('deploy')


def test_skill_activation_includes_relevant_body_and_excludes_other_context(runtime):
    write_skill(runtime, 'report', '---\nactivation:\n  any: [report]\n---\nREPORT_RULE')
    assert 'REPORT_RULE' in runtime._build_system_prompt('write report')
    assert 'REPORT_RULE' not in runtime._build_system_prompt('write code')
    assert 'REPORT_RULE' not in runtime._build_system_prompt('')


def test_oversized_skill_is_discoverable_without_partial_instructions(runtime):
    write_skill(runtime, 'large', 'SENSITIVE_RULE' * 2000)
    runtime._local.current_skill = 'large'
    prompt = runtime._build_system_prompt('work')
    assert 'SENSITIVE_RULE' not in prompt
    assert 'large/SKILL.md' in prompt


def test_truncated_tool_reply_is_never_executed(runtime, monkeypatch):
    calls = []
    response = tool_reply('file_write', '{"path":"output","content":"partial"}')
    response['finish_reason'] = 'length'
    monkeypatch.setattr(runtime, '_call_llm_messages_with_tools', lambda *a: response)
    monkeypatch.setattr(ToolRegistry, 'call_tool', lambda *a: calls.append(a))
    result = execute(runtime)
    assert result['status'] == 'failed' and calls == []


def test_cancelled_execution_keeps_consumed_usage(runtime, monkeypatch):
    def response(*args):
        runtime.cancel()
        return tool_reply()
    monkeypatch.setattr(runtime, '_call_llm_messages_with_tools', response)
    result = execute(runtime)
    assert result['status'] == 'cancelled'
    assert result['usage']['input_tokens'] == 11
    assert result['usage']['output_tokens'] == 3


def test_corrupt_cached_skill_does_not_break_other_tasks(runtime):
    write_skill(runtime, 'bad', 'placeholder')
    (runtime._skills_cache_dir / 'bad' / 'SKILL.md').write_bytes(b'\xff\xfe')
    write_skill(runtime, 'valid', '---\nactivation:\n  any: [report]\n---\nVALID_RULE')
    assert 'VALID_RULE' in runtime._build_system_prompt('report')
