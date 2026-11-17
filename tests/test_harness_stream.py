"""Offline JSONL subprocess, accounting and CLI policy regressions."""

import json
import os
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from lan_mesh.harness import registry


def run_script(tmp_path, script, backend='codex', **options):
    adapter = registry.get(backend)
    request = dict(argv=[sys.executable, '-u', '-c', script], cwd=str(tmp_path),
                   env=dict(os.environ), timeout=5)
    request.update(options)
    return list(adapter.stream(request))


def json_script(records):
    return '\n'.join('print(' + repr(json.dumps(record)) + ', flush=True)' for record in records)


def test_ordered_stream_and_usage(tmp_path):
    records = [{'type': 'assistant', 'text': str(i)} for i in range(3)]
    records += [{'type': 'usage', 'usage': {'input_tokens': 12, 'output_tokens': 8}},
                {'type': 'run.completed', 'result': '012'}]
    events = run_script(tmp_path, json_script(records))
    assert events[0].kind == 'run.started'
    assert events[-1].kind == 'run.completed'
    assert [e.data['text'] for e in events if e.kind == 'assistant'] == ['0', '1', '2']
    assert [e.seq for e in events] == list(range(len(events)))
    outcome = events[-1].data
    assert outcome['result'] == '012'
    assert outcome['usage'] == dict(input_tokens=12, output_tokens=8)
    assert outcome['usage_known'] is True
    assert outcome['cost_estimated'] is True
    assert 'usd' in outcome['degraded']
    assert outcome['status'] == 'completed'


@pytest.mark.parametrize('backend', ['claude', 'aider', 'codex'])
def test_plain_and_malformed_output(tmp_path, backend):
    result = run_script(tmp_path, "print('plain'); print('{broken')", backend)[-1].data
    assert result['result'] == 'plain\n{broken\n'
    assert result['usage_known'] is False
    assert result['cost_usd'] == 0.0
    assert result['cost_estimated'] is True
    assert 'usd' in result['degraded']


@pytest.mark.parametrize('records,expected', [
    ([{'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': 'hi'}]}},
      {'type': 'result', 'result': 'hi', 'total_cost_usd': 0.25,
       'usage': {'input_tokens': 20, 'output_tokens': 10}}], 'hi'),
    ([{'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'hello'}},
      {'type': 'turn.completed', 'usage': {'input_tokens': 20, 'output_tokens': 10}}], 'hello'),
])
def test_native_records(tmp_path, records, expected):
    result = run_script(tmp_path, json_script(records))[-1].data
    assert result['result'] == expected
    assert result['usage_known'] is True
    if 'total_cost_usd' in records[-1]:
        assert result['cost_usd'] == 0.25
        assert result['cost_estimated'] is False


def test_pool_price_estimate(tmp_path, monkeypatch):
    pool = tmp_path / 'pool.yaml'
    pool.write_text('models:\n- id: test-model\n  provider: openai\n'
                    '  cost_input_per_1k: 1\n  cost_output_per_1k: 2\n')
    monkeypatch.setenv('LAN_MESH_MODEL_POOL', str(pool))
    records = [{'type': 'usage', 'model': 'test-model',
                'usage': {'input_tokens': 100, 'output_tokens': 50}},
               {'type': 'usage', 'usage': {'input_tokens': 200, 'output_tokens': 100}}]
    result = run_script(tmp_path, json_script(records))[-1].data
    assert result['cost_usd'] == pytest.approx(0.4)
    assert result['cost_estimated'] is True
    assert 'usd' not in result['degraded']


def test_cancel_and_kill_tree(tmp_path):
    cancel = threading.Event()
    child = tmp_path / 'survived'
    script = ("import subprocess,sys,time; "
              "subprocess.Popen([sys.executable,'-c', " + repr(
                  'import time,pathlib; time.sleep(2); pathlib.Path(' + repr(str(child)) +
                  ').write_text("alive")') + "]); print('ready', flush=True); time.sleep(30)")
    request = dict(argv=[sys.executable, '-u', '-c', script], cwd=str(tmp_path),
                   env=dict(os.environ), timeout=10, cancel_event=cancel)
    started = time.monotonic()
    events = []
    for event in registry.get('codex').stream(request):
        events.append(event)
        if event.kind == 'assistant':
            cancel.set()
    assert events[-1].data['status'] == 'cancelled'
    assert time.monotonic() - started < 5
    time.sleep(2.1)
    assert not child.exists()


def test_timeout(tmp_path):
    result = run_script(tmp_path, 'import time; time.sleep(30)', timeout=0.15)[-1].data
    assert result['status'] == 'timeout'
    assert result['error_class'] == 'timeout'


@pytest.mark.parametrize('code,diagnostic,error', [
    (127, '', 'not_installed'), (1, 'usage limit reached', 'usage_limit'),
    (1, 'authentication failed', 'auth'), (1, 'network unavailable', 'network'),
    (1, 'sandbox denied', 'sandbox'), (1, 'something else', 'unknown'),
])
def test_failures(tmp_path, code, diagnostic, error):
    result = run_script(tmp_path, f'import sys; print({diagnostic!r}, file=sys.stderr); sys.exit({code})')[-1].data
    assert result['status'] == 'failed'
    assert result['returncode'] == code
    assert result['error_class'] == error


def test_missing_executable(tmp_path):
    events = list(registry.get('codex').stream(dict(
        argv=[str(tmp_path / 'missing-executable')], cwd=str(tmp_path), env=dict(os.environ))))
    assert events[-1].data['error_class'] == 'not_installed'


def test_budget_stops_and_unknown_budget_degrades(tmp_path):
    script = json_script([{'type': 'usage', 'cost_usd': 2.0}]) + '\nimport time; time.sleep(30)'
    result = run_script(tmp_path, script, budget_usd=1)[-1].data
    assert result['status'] == 'failed'
    assert result['error_class'] == 'budget_exceeded'
    result = run_script(tmp_path, "print('plain')", budget_usd=0)[-1].data
    assert result['status'] == 'completed'
    assert 'budget' in result['degraded']


def test_stderr_does_not_deadlock_and_output_is_bounded(tmp_path):
    result = run_script(tmp_path, "import sys; sys.stderr.write('x'*500000); print('y'*500000)",
                        max_output=1000)[-1].data
    assert result['status'] == 'completed'
    assert len(result['result']) <= 1000
    assert len(result['stderr']) <= 250


@pytest.mark.parametrize('ending,status', [('', 'completed'), ('sys.exit(1)', 'failed'),
                                         ('time.sleep(30)', 'timeout')])
def test_service_records_external_usage(tmp_path, monkeypatch, ending, status):
    from lan_mesh import agent_runtime as policy, runtime_usage
    from lan_mesh.runtime_cli_service import RuntimeCliService
    owner = SimpleNamespace(shared_folder=str(tmp_path), _cancel_event=threading.Event())
    service = object.__new__(RuntimeCliService)
    service.owner = owner
    monkeypatch.setattr(policy, 'resolve_cli_backend', lambda backend: ('codex', None))
    monkeypatch.setattr(policy, 'validate_cli_agent_cwd', lambda cwd, roots: '')
    script = 'import sys,time; ' + ending
    monkeypatch.setattr(service, '_build_cli_command', lambda *args: [sys.executable, '-c', script])
    calls = []
    monkeypatch.setattr(runtime_usage, 'record_external_usage', lambda **kwargs: calls.append(kwargs))
    result = service._handle_cli_agent(dict(requirement='test', backend='codex', timeout=0.2))
    assert result['status'] == status
    assert all(key in result for key in ('result', 'status', 'backend', 'elapsed_secs', 'returncode'))
    assert len(calls) == 1
    assert calls[0]['owner'] is owner
    assert calls[0]['model_id'] == 'cli:codex'
    assert calls[0]['call_type'] == 'cli'
    assert calls[0]['outcome'] == status
    assert calls[0]['usage_known'] is False


def test_known_zero_usage(tmp_path):
    result = run_script(tmp_path, json_script([{'type': 'usage', 'usage': {
        'input_tokens': 0, 'output_tokens': 0}}]))[-1].data
    assert result['usage_known'] is True
    assert result['usage'] == dict(input_tokens=0, output_tokens=0)


def test_pre_cancel_does_not_launch(tmp_path):
    cancel = threading.Event()
    cancel.set()
    result = run_script(tmp_path, 'raise RuntimeError("must not run")', cancel_event=cancel)[-1].data
    assert result['status'] == 'cancelled'


def test_cancellation_is_metered(tmp_path, monkeypatch):
    from lan_mesh import agent_runtime as policy, runtime_usage
    from lan_mesh.runtime_cli_service import RuntimeCliService
    owner = SimpleNamespace(shared_folder=str(tmp_path), _cancel_event=threading.Event())
    owner._cancel_event.set()
    service = object.__new__(RuntimeCliService)
    service.owner = owner
    monkeypatch.setattr(policy, 'resolve_cli_backend', lambda backend: ('codex', None))
    monkeypatch.setattr(policy, 'validate_cli_agent_cwd', lambda cwd, roots: '')
    monkeypatch.setattr(service, '_build_cli_command', lambda *args: [sys.executable, '-c', ''])
    calls = []
    monkeypatch.setattr(runtime_usage, 'record_external_usage', lambda **kwargs: calls.append(kwargs))
    result = service._handle_cli_agent(dict(requirement='test'))
    assert result['status'] == 'cancelled'
    assert calls[0]['outcome'] == 'cancelled'
    assert calls[0]['usage_known'] is False


def test_service_records_reported_usage_and_budget_failure(tmp_path, monkeypatch):
    from lan_mesh import agent_runtime as policy, runtime_usage
    from lan_mesh.runtime_cli_service import RuntimeCliService
    owner = SimpleNamespace(shared_folder=str(tmp_path), _cancel_event=threading.Event())
    service = object.__new__(RuntimeCliService)
    service.owner = owner
    monkeypatch.setattr(policy, 'resolve_cli_backend', lambda backend: ('codex', None))
    monkeypatch.setattr(policy, 'validate_cli_agent_cwd', lambda cwd, roots: '')
    script = json_script([{'type': 'usage', 'usage': {'input_tokens': 23, 'output_tokens': 17},
                           'cost_usd': 2}])
    monkeypatch.setattr(service, '_build_cli_command', lambda *args: [sys.executable, '-c', script])
    calls = []
    monkeypatch.setattr(runtime_usage, 'record_external_usage', lambda **kwargs: calls.append(kwargs))
    result = service._handle_cli_agent(dict(requirement='test', budget_usd=1))
    assert result['status'] == 'failed'
    assert result['error_class'] == 'budget_exceeded'
    assert len(calls) == 1
    assert calls[0]['input_tokens'] == 23
    assert calls[0]['output_tokens'] == 17
    assert calls[0]['usage_known'] is True
    assert calls[0]['outcome'] == 'failed'


def test_stream_is_live_and_generator_close_reaps_child(tmp_path):
    request = dict(argv=[sys.executable, '-u', '-c',
                        "print('first', flush=True); import time; time.sleep(30)"],
                   cwd=str(tmp_path), env=dict(os.environ), timeout=5)
    stream = registry.get('codex').stream(request)
    assert next(stream).kind == 'run.started'
    started = time.monotonic()
    assert next(stream).data['text'] == 'first\n'
    assert time.monotonic() - started < 3
    stream.close()


def test_structured_failure_with_zero_exit_is_failed(tmp_path):
    result = run_script(tmp_path, json_script([{
        'type': 'turn.failed', 'error': {'message': 'usage limit reached'}}]))[-1].data
    assert result['status'] == 'failed'
    assert result['error_class'] == 'usage_limit'
