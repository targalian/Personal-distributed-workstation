"""Control transfer must wait for admitted work, preserve scope and survive retries."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lan_mesh.agent_runtime import AgentRuntime
from lan_mesh.browser_session_service import BrowserSessionService, origin
from lan_mesh.collaboration_repository import CollaborationRepository
from lan_mesh.collaboration_routes import build_collaboration_routes
from lan_mesh.component_service import call_component
from lan_mesh.database import Database
from lan_mesh.pm_agent import ProjectManagerAgent
from lan_mesh.protocol import Task
from lan_mesh.service_runtime import ServiceError


def test_cancel_waits_for_admitted_execution_and_rejects_new_work(tmp_path: Path) -> None:
    runtime = AgentRuntime('a', str(tmp_path))
    entered, finish = Event(), Event()
    def handler(data: dict) -> dict:
        entered.set()
        assert finish.wait(3)
        return {'text': 'complete'}
    runtime._handlers['test'] = handler
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(runtime.execute, {'required_skill': 'test'})
        assert entered.wait(3)
        runtime.cancel()
        assert runtime.activity() == {'active_executions': 1, 'quiescent': False}
        assert runtime.execute({'required_skill': 'test'})['status'] == 'cancelled'
        finish.set()
        pending.result()
    assert runtime.activity()['quiescent'] is True
    runtime.services.close()


def test_pause_fences_completion_and_propagates_unconfirmed_agents(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = AgentRuntime('a', str(tmp_path))
    pm = ProjectManagerAgent('pm', runtime, 'http://local', 'host')
    pm._running = True
    pm._state.task_station['x'] = {'ip': '127.0.0.1', 'api_port': 42}
    pm._state.task_agent['x'] = {'agent_id': 'child'}
    calls = []
    monkeypatch.setattr('lan_mesh.pm_agent.http_post', lambda *a, **k: calls.append((a, k)))
    monkeypatch.setattr('lan_mesh.pm_dispatcher.requests.post', Mock(side_effect=ConnectionError('offline')))
    first = pm.pause()
    assert first['quiescent'] is False and first['pending_agents'] == ['child']
    before = len(calls)
    pm.report_status('completed')
    pm.deliver_result('x', 'x', 'result', [])
    assert len(calls) == before and pm._delivery_status == 'paused'
    monkeypatch.setattr('lan_mesh.pm_dispatcher.requests.post', lambda *a, **k: SimpleNamespace(
        raise_for_status=lambda: None, json=lambda: {'ok': True, 'quiescent': True}))
    assert pm.pause()['quiescent'] is True
    assert runtime.is_cancelled()
    pm._dispatcher.dispatch_subtask({}, {}, {}, {})  # Never reaches malformed target.
    runtime.services.close()


def test_takeover_confirmation_persists_and_repeated_requests_do_not_notify(tmp_path: Path) -> None:
    db = Database(str(tmp_path / 'collab.db'))
    db.save_task(Task(task_id='task', name='Task', status='running', pm_agent_id='pm'))
    controller = SimpleNamespace(db=db, secretary_active=True, get_task_graph_data=lambda _: {},
        chat_handler=None, pause_task=Mock(side_effect=[{'ok': True, 'quiescent': False},
                                                     {'ok': True, 'quiescent': True}]))
    app = FastAPI(); app.include_router(build_collaboration_routes(controller))
    with TestClient(app) as client:
        path = '/api/collaboration/task'
        assert client.post(path + '/takeover').json()['state'] == 'pausing'
        assert client.post(path + '/release').status_code == 409
        assert client.post(path + '/takeover').json()['state'] == 'human'
        assert client.post(path + '/takeover').json()['state'] == 'human'
        assert controller.pause_task.call_count == 2
        assert len(client.get(path).json()['notices']) == 2
        reopened = Database(str(tmp_path / 'collab.db'))
        assert CollaborationRepository(reopened).get('task')['state'] == 'human'
        assert client.post(path + '/reply', json={'message': 'go'}).status_code == 409
        assert client.post(path + '/release').json()['state'] == 'automatic'
        assert db.get_task('task').status == 'running'  # Release never invokes resume.
        assert client.get('/api/collaboration/missing').status_code == 404
        controller.secretary_active = False
        assert client.get(path).status_code == 503
    controller.services.close()


def test_notice_concurrent_dedup(tmp_path: Path) -> None:
    db = Database(str(tmp_path / 'notice.db'))
    repo = CollaborationRepository(db)
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(lambda _: repo.notice('same-event', 't', 'waiting'), range(30))) == 1
    assert len(repo.notices('t')) == 1


@pytest.mark.parametrize('url', ['file:///etc/passwd', 'data:text/html,hello', 'https://user:pass@host', 'javascript:alert(1)'])
def test_browser_rejects_non_web_and_embedded_secrets(url: str) -> None:
    with pytest.raises(ServiceError):
        origin(url)


class Page:
    def __init__(self) -> None:
        self.url = 'about:blank'
        self.navigations = []
    def goto(self, url: str, **kwargs: object) -> None:
        self.url = url
        self.navigations.append(url)
    def locator(self, selector: str) -> 'Page':
        return self
    def inner_text(self, **kwargs: object) -> str:
        return 'private task page'
    def screenshot(self, **kwargs: object) -> bytes:
        return b'image'


def test_browser_transfer_serializes_admitted_calls_and_enforces_task_scope() -> None:
    owner = SimpleNamespace()
    # Use the real owned service dispatcher; only the browser transport is substituted.
    call_component(owner, BrowserSessionService, 'assigned', task_id='task', project_id='project')
    service = owner.services.server.services['BrowserSessionService'] if 'BrowserSessionService' in owner.services.server.services else owner.services.server.services['/BrowserSessionService']
    page = Page()
    service._sessions['session'] = dict(task_id='task', project_id='project', mode='automatic',
        page=page, context=SimpleNamespace(close=lambda: None), approved={'https://example.com:443'})
    def command(operation: str, actor: str = 'automation', task_id: str = 'task', **params: object) -> dict:
        return call_component(owner, BrowserSessionService, 'command', session_id='session',
            operation=operation, params=params, task_id=task_id, project_id='project', actor=actor)
    assert command('navigate', url='https://example.com/a')['mode'] == 'automatic'
    with pytest.raises(ServiceError): command('snapshot', task_id='another')
    with pytest.raises(ServiceError): command('navigate', url='https://unapproved.example')
    with pytest.raises(ServiceError): command('takeover')
    assert command('takeover', actor='human')['mode'] == 'human'
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(command, 'navigate', url='https://example.com/stale') for _ in range(16)]
        for future in futures:
            with pytest.raises(ServiceError): future.result()
    assert page.navigations == ['https://example.com/a']
    assert command('navigate', actor='human', url='https://second.example')['mode'] == 'human'
    assert command('release', actor='human')['mode'] == 'automatic'
    assert command('close', actor='human')['mode'] == 'closed'
    with pytest.raises(ServiceError): command('snapshot')
    owner.services.close()


def test_browser_http_command_keeps_operation_and_task_scope(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from lan_mesh.browser_routes import build_browser_routes
    db = Database(str(tmp_path / 'browser-routes.db'))
    db.save_task(Task(task_id='task', project_id='project'))
    received = []
    def dispatch(owner: object, component: object, operation: str, /, **arguments: object) -> dict:
        received.append((operation, arguments))
        return {'mode': 'human'}
    monkeypatch.setattr('lan_mesh.browser_routes.call_component', dispatch)
    app = FastAPI();app.include_router(build_browser_routes(SimpleNamespace(db=db)))
    with TestClient(app) as client:
        response = client.post('/api/browser/sessions/s/actions', json={
            'task_id': 'task', 'operation': 'navigate', 'url': 'https://example.com'})
        assert response.status_code == 200
        operation, arguments = received[-1]
        assert operation == 'command'
        assert arguments['operation'] == 'navigate' and arguments['actor'] == 'human'
        assert arguments['project_id'] == 'project'
        assert client.post('/api/browser/sessions/s/takeover', json={'task_id': 'other'}).status_code == 404
