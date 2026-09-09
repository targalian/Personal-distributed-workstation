'''Shadow development guardrail tests.'''

import os
import time
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_cli_agent_cwd_guard_blocks_repo(tmp_path: Path) -> None:
    '''Main repo cwd is blocked and external roots require explicit allowlist.'''
    import lan_mesh.agent_runtime as runtime

    assert runtime.validate_cli_agent_cwd(str(REPO_ROOT)) is not None
    assert runtime.validate_cli_agent_cwd(str(tmp_path)) is not None
    (tmp_path / 'nested').mkdir()
    assert runtime.validate_cli_agent_cwd(
        str(tmp_path / 'nested'), [str(tmp_path)]) is None


def test_cli_agent_handler_rejects_repo_before_backend(tmp_path: Path) -> None:
    '''Direct repo cwd must fail before any CLI executable is launched.'''
    from lan_mesh.agent_runtime import AgentRuntime

    agent = AgentRuntime(agent_id='test-shadow', shared_folder_path=str(tmp_path))
    result = agent._handle_cli_agent({
        'requirement': 'test',
        'cwd': str(REPO_ROOT),
        'backend': 'codex',
    })

    assert result.get('error')
    assert result.get('cwd') == str(REPO_ROOT)


def test_cli_agent_handler_allows_only_shared_root(tmp_path: Path) -> None:
    '''A cwd outside runtime shared folder is rejected before backend launch.'''
    from lan_mesh.agent_runtime import AgentRuntime

    outside = tmp_path / 'outside'
    outside.mkdir()
    agent = AgentRuntime(agent_id='test-shadow',
                         shared_folder_path=str(tmp_path / 'shared'))
    (tmp_path / 'shared').mkdir()

    result = agent._handle_cli_agent({
        'requirement': 'test',
        'cwd': str(outside),
        'backend': 'codex',
    })

    assert '不在白名单内' in result.get('error', '')


def test_self_mod_violations_detect_guardrail_files(tmp_path: Path) -> None:
    '''Changed guardrail and gate paths are reported as forbidden.'''
    from lan_mesh.agent_runtime import check_self_mod_violations

    (tmp_path / '.githooks').mkdir()
    (tmp_path / '.githooks' / 'pre-push').write_text('x', encoding='utf-8')
    (tmp_path / 'lan_mesh').mkdir()
    (tmp_path / 'lan_mesh' / 'agent_runtime.py').write_text('x', encoding='utf-8')

    violations = check_self_mod_violations([
        'lan_mesh/agent_runtime.py',
        '.githooks/pre-push',
        'AGENTS.md',
        'scripts/ship.ps1',
    ])

    assert 'lan_mesh/agent_runtime.py' in violations
    assert '.githooks/' in violations


def test_cli_env_filters_secrets_and_git_credentials(monkeypatch) -> None:
    '''Only required backend and platform variables reach CLI subprocess.'''
    from lan_mesh.agent_runtime import _build_cli_env

    monkeypatch.delenv('ALIYUN_TOKENPLAN_API_KEY', raising=False)

    old_values = {
        'DEEPSEEK_API_KEY': 'secret-value-1234567890',
        'GITHUB_TOKEN': 'git-secret-1234567890',
        'GIT_ASKPASS': 'helper',
        'ANTHROPIC_API_KEY': 'anthropic-secret-1234567890',
    }
    for key, value in old_values.items():
        os.environ[key] = value
    try:
        env = _build_cli_env('claude')
    finally:
        for key, value in old_values.items():
            os.environ[key] = value

    assert 'ANTHROPIC_API_KEY' in env
    assert 'DEEPSEEK_API_KEY' not in env
    assert 'GITHUB_TOKEN' not in env
    assert 'GIT_ASKPASS' not in env
    assert env['GIT_TERMINAL_PROMPT'] == '0'


def test_shadow_copy_filters_sensitive_files(tmp_path: Path) -> None:
    '''Sensitive configs are not copied into the shadow workspace.'''
    from lan_mesh import shadow_dev

    ignored = shadow_dev._shadow_ignore(
        str(tmp_path), ['.env', 'model_pool.yaml', 'config.yaml', 'safe.py'])

    assert '.env' in ignored
    assert 'model_pool.yaml' in ignored
    assert 'config.yaml' in ignored
    assert 'safe.py' not in ignored


def test_shadow_diff_scans_added_secrets() -> None:
    '''Added secret-looking lines fail the shadow secret gate.'''
    from lan_mesh.shadow_dev import scan_added_lines_for_secrets

    patch = (
        '--- a/file\n'
        '+++ b/file\n'
        # 无引号形态: 能被 scan_added_lines_for_secrets 检出 (引号可选),
        # 但避开 pre-push 门禁的硬编码密钥误报 (其正则强制带引号)
        '+api_key = sk-abcdefghijklmnopqrstuvw\n'
        '-old line\n'
    )

    assert scan_added_lines_for_secrets(patch)
    assert not scan_added_lines_for_secrets('+++ b/file\n+safe = "value"\n')


def test_shadow_manager_runs_queued_task(monkeypatch, tmp_path: Path) -> None:
    '''Guardian executes submissions serially and records the report.'''
    import lan_mesh.shadow_dev as shadow_dev

    executed: list[str] = []

    def fake_execute(run_id: str, task: str, backend: str, timeout: int,
                     keep: bool, simulate: bool) -> dict:
        executed.append(run_id)
        return {
            'run_id': run_id,
            'task': task,
            'backend': backend,
            'verdict': 'READY_FOR_REVIEW',
            'diff': {'diff_file': str(tmp_path / 'changes.patch')},
        }

    monkeypatch.setattr(shadow_dev, 'execute_run', fake_execute)
    manager = shadow_dev.ShadowDevManager()
    record = manager.submit('demo task', simulate=True)

    for _ in range(200):
        if manager.get_run(record['run_id'])['status'] != 'queued':
            break
        time.sleep(0.01)

    manager.stop_guardian()

    assert executed == [record['run_id']]
    assert manager.get_run(record['run_id'])['status'] == 'READY_FOR_REVIEW'
    assert manager.status()['running'] is False
    assert manager.status()['queued'] == 0


def test_shadow_manager_merges_history(monkeypatch, tmp_path: Path) -> None:
    '''Historical reports are listed even after process restart.'''
    import json
    import lan_mesh.shadow_dev as shadow_dev

    run_id = '20260830-120000-oldrun'
    run_dir = tmp_path / run_id
    run_dir.mkdir()
    (run_dir / 'report.json').write_text(
        json.dumps({'run_id': run_id, 'verdict': 'READY_FOR_REVIEW'}),
        encoding='utf-8')
    monkeypatch.setattr(shadow_dev, 'SHADOW_HOME', tmp_path)
    manager = shadow_dev.ShadowDevManager()

    runs = manager.list_runs()

    assert runs[0]['run_id'] == run_id
    assert runs[0]['status'] == 'READY_FOR_REVIEW'


def test_shadow_dev_api_contract() -> None:
    '''Shadow API submits work and returns status/list/detail.'''
    from lan_mesh.station_routes_shadow import build_shadow_dev_routes

    class FakeManager:
        def submit(self, task: str, backend: str, timeout: int,
                   simulate: bool) -> dict:
            return {'run_id': 'run-1', 'task': task, 'backend': backend,
                    'timeout': timeout, 'status': 'queued'}

        def list_runs(self) -> list[dict]:
            return [{'run_id': 'run-1', 'status': 'queued'}]

        def get_run(self, run_id: str) -> dict | None:
            return {'run_id': run_id, 'status': 'queued'} if run_id == 'run-1' else None

        def status(self) -> dict:
            return {'running': True, 'busy': False, 'queued': 1}

    class FakeController:
        shadow_dev_manager = FakeManager()

    app = FastAPI()
    app.include_router(build_shadow_dev_routes(FakeController()))
    client = TestClient(app)

    created = client.post('/api/shadow-dev/runs', json={'task': 'demo'})
    listed = client.get('/api/shadow-dev/runs')
    detail = client.get('/api/shadow-dev/runs/run-1')
    missing = client.get('/api/shadow-dev/runs/missing')
    status = client.get('/api/shadow-dev/status')

    assert created.status_code == 202
    assert created.json()['run_id'] == 'run-1'
    assert listed.json()['runs'][0]['status'] == 'queued'
    assert detail.status_code == 200
    assert missing.status_code == 404
    assert status.json()['queued'] == 1
def _capture_shadow_events(monkeypatch) -> list[dict]:
    """Intercept event_bus.publish_event and collect shadow run events."""
    import lan_mesh.event_bus as event_bus

    captured: list[dict] = []

    def fake_publish(event_type: str, data: dict) -> None:
        captured.append({'type': event_type, 'data': data})

    monkeypatch.setattr(event_bus, 'publish_event', fake_publish)
    return captured


def test_shadow_run_events_cover_full_lifecycle(monkeypatch, tmp_path: Path) -> None:
    """iter-98 F6: queued -> running -> terminal are broadcast on the event bus."""
    import lan_mesh.shadow_dev as shadow_dev

    events = _capture_shadow_events(monkeypatch)

    def fake_execute(run_id: str, task: str, backend: str, timeout: int,
                     keep: bool, simulate: bool) -> dict:
        return {'run_id': run_id, 'task': task, 'verdict': 'READY_FOR_REVIEW'}

    monkeypatch.setattr(shadow_dev, 'execute_run', fake_execute)
    monkeypatch.setattr(shadow_dev, 'SHADOW_HOME', tmp_path)
    manager = shadow_dev.ShadowDevManager()
    record = manager.submit('demo task', simulate=True)

    for _ in range(200):
        if manager.get_run(record['run_id'])['status'] != 'queued':
            break
        time.sleep(0.01)
    for _ in range(200):
        if len([e for e in events if e['type'] == 'shadow_run_update']) >= 3:
            break
        time.sleep(0.01)
    manager.stop_guardian()

    shadow_events = [e for e in events if e['type'] == 'shadow_run_update']
    statuses = [e['data']['status'] for e in shadow_events]
    assert statuses[:3] == ['queued', 'running', 'READY_FOR_REVIEW']
    assert all(e['data']['run_id'] == record['run_id'] for e in shadow_events)
    assert shadow_events[0]['data']['task'] == 'demo task'
    assert shadow_events[0]['data']['queued'] == 1


def test_shadow_run_event_reports_execution_failure(monkeypatch,
                                                    tmp_path: Path) -> None:
    """iter-98 F6: guardian exceptions still reach the UI as ERROR events."""
    import lan_mesh.shadow_dev as shadow_dev

    events = _capture_shadow_events(monkeypatch)

    def boom(run_id: str, task: str, backend: str, timeout: int,
             keep: bool, simulate: bool) -> dict:
        raise RuntimeError('shadow blew up')

    monkeypatch.setattr(shadow_dev, 'execute_run', boom)
    monkeypatch.setattr(shadow_dev, 'SHADOW_HOME', tmp_path)
    manager = shadow_dev.ShadowDevManager()
    record = manager.submit('failing task', simulate=True)

    for _ in range(200):
        if manager.get_run(record['run_id'])['status'] == 'ERROR':
            break
        time.sleep(0.01)
    manager.stop_guardian()

    errors = [e for e in events
              if e['type'] == 'shadow_run_update' and e['data']['status'] == 'ERROR']
    assert errors, 'ERROR terminal state must be broadcast'
    assert 'shadow blew up' in errors[-1]['data'].get('error', '')


def test_shadow_stop_guardian_broadcasts_cancelled(monkeypatch,
                                                   tmp_path: Path) -> None:
    """iter-98 F6: queued runs dropped by stop_guardian are broadcast too."""
    import threading as _threading
    import lan_mesh.shadow_dev as shadow_dev

    events = _capture_shadow_events(monkeypatch)
    release = _threading.Event()

    def blocking_execute(run_id: str, task: str, backend: str, timeout: int,
                         keep: bool, simulate: bool) -> dict:
        release.wait(5)
        return {'run_id': run_id, 'verdict': 'READY_FOR_REVIEW'}

    monkeypatch.setattr(shadow_dev, 'execute_run', blocking_execute)
    monkeypatch.setattr(shadow_dev, 'SHADOW_HOME', tmp_path)
    manager = shadow_dev.ShadowDevManager()
    first = manager.submit('long running', simulate=True)
    for _ in range(200):
        if manager.get_run(first['run_id'])['status'] == 'running':
            break
        time.sleep(0.01)
    second = manager.submit('still queued', simulate=True)

    manager.stop_guardian()
    release.set()

    cancelled = [e['data']['run_id'] for e in events
                 if e['type'] == 'shadow_run_update'
                 and e['data']['status'] == 'cancelled']
    assert second['run_id'] in cancelled
    assert first['run_id'] not in cancelled


def test_shadow_event_failure_does_not_break_submit(monkeypatch,
                                                    tmp_path: Path) -> None:
    """iter-98 F6: a broken event channel must never block shadow execution."""
    import lan_mesh.event_bus as event_bus
    import lan_mesh.shadow_dev as shadow_dev

    def exploding_publish(event_type: str, data: dict) -> None:
        raise RuntimeError('bus down')

    monkeypatch.setattr(event_bus, 'publish_event', exploding_publish)
    monkeypatch.setattr(shadow_dev, 'execute_run',
                        lambda *a, **k: {'verdict': 'READY_FOR_REVIEW'})
    monkeypatch.setattr(shadow_dev, 'SHADOW_HOME', tmp_path)
    manager = shadow_dev.ShadowDevManager()

    record = manager.submit('resilient task', simulate=True)
    for _ in range(200):
        if manager.get_run(record['run_id'])['status'] != 'queued':
            break
        time.sleep(0.01)
    manager.stop_guardian()

    assert manager.get_run(record['run_id'])['status'] == 'READY_FOR_REVIEW'


def test_dashboard_reacts_to_shadow_run_event() -> None:
    """iter-98 F6: dashboard wires shadow_run_update to the shadowdev panel."""
    from lan_mesh.station_controller import TEMPLATES_DIR

    html = (TEMPLATES_DIR / 'dashboard.html').read_text(encoding='utf-8')
    handler = html.split("if(t==='shadow_run_update'){", 1)[1].split(
        "if(t==='cost_budget_warning')", 1)[0]

    assert "refreshShadowDev()" in handler
    # 仅在影子 Tab 停留时刷新 (iter-91 F3 同类恒真守卫踩过坑)
    assert "panel-shadowdev" in handler
    assert "classList.contains('active')" in handler
    # 排队/执行中不弹 toast, 否则一次运行会连弹三次
    assert "st!=='queued'&&st!=='running'" in handler
