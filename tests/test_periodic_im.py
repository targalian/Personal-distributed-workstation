"""Calendar edges, durable trigger admission and IM room/event ownership regressions."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock
import json
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lan_mesh.component_service import call_component
from lan_mesh.database import Database
from lan_mesh.idempotent_submission import IdempotentSubmissionService
from lan_mesh.im_service import ImService
from lan_mesh.im_telegram import receive_native, flush_replies
from lan_mesh.periodic_repository import PeriodicRepository
from lan_mesh.periodic_routes import build_periodic_routes
from lan_mesh.periodic_service import PeriodicService
from lan_mesh.periodic_spec import next_due, validate_spec
from lan_mesh.protocol import Task
from lan_mesh.service_runtime import ServiceError
from lan_mesh.submission_context import can_dispatch
from lan_mesh.task_scheduling_service import get_scheduling_service


@pytest.fixture
def controller(tmp_path: Path) -> object:
    db = Database(str(tmp_path / 'scheduled.db'))
    chat = SimpleNamespace(_conv_index=[{'id': 'conv', 'project_id': 'project'},
        {'id': 'next', 'project_id': 'project'}, {'id': 'foreign', 'project_id': 'other'}],
        chat=Mock(return_value={'reply': 'done'}), find_conv_by_pm=lambda pm: 'conv' if pm == 'pm' else 'foreign')
    ctl = SimpleNamespace(db=db, secretary_active=True, chat_handler=chat,
        project_manager=None, state=SimpleNamespace(device_id='local', device_name='Local'),
        bot_gateway=SimpleNamespace(notify=Mock()), inject_input_to_pm=Mock(return_value={'ok': True}))
    yield ctl
    if vars(ctl).get('services'):ctl.services.close()


def stamp(value: str) -> float:
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc).timestamp()


def test_daily_dst_gap_and_repeated_hour_run_once() -> None:
    spec = validate_spec({'kind': 'daily', 'timezone': 'America/New_York', 'hour': 2, 'minute': 30}, 0)
    assert next_due(spec, stamp('2026-03-08T00:00:00')) == stamp('2026-03-09T06:30:00')
    spec['hour'] = 1
    first = next_due(spec, stamp('2026-11-01T00:00:00'))
    assert first == stamp('2026-11-01T05:30:00')
    assert next_due(spec, first) == stamp('2026-11-02T06:30:00')
    chinese = validate_spec({'kind': 'daily', 'timezone': 'Asia/Shanghai', 'hour': 9}, 0)
    assert next_due(chinese, stamp('2026-11-12T00:00:00')) == stamp('2026-11-12T01:00:00')


@pytest.mark.parametrize('spec', [{'timezone': 'Bad/Zone'}, {'kind': 'cron'}, {'kind': 'interval', 'seconds': 1}, {'hour': 24}])
def test_invalid_schedule_never_persists(controller: object, spec: dict) -> None:
    with pytest.raises(ServiceError):
        call_component(controller, PeriodicService, 'create', conv_id='conv',
                       payload={'name': 'invalid', 'spec': spec, 'template': {'description': 'work'}})
    assert PeriodicRepository(controller.db).list('conv') == []


def _schedule(controller: object, catchup: str = 'once') -> dict:
    row = call_component(controller, PeriodicService, 'create', conv_id='conv', payload={
        'name': 'hourly', 'spec': {'kind': 'interval', 'seconds': 60, 'anchor': 0},
        'template': {'description': 'work'}, 'catchup': catchup})
    with controller.db._get_conn() as conn:
        conn.execute('UPDATE periodic_schedules SET next_run=60 WHERE schedule_id=?', (row['schedule_id'],))
    return row


def test_downtime_catchup_and_concurrent_claims(controller: object) -> None:
    row = _schedule(controller)
    repo = PeriodicRepository(controller.db)
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(lambda _: repo.claim_due(601), range(24))) == 1
    run = repo.runs(row['schedule_id'])[0]
    assert run['due_at'] == 600 and run['state'] == 'reserved'
    assert repo.get(row['schedule_id'], 'conv')['next_run'] == 660
    repo.claim_due(660)
    assert repo.runs(row['schedule_id'])[0]['state'] == 'skipped_busy'
    row2 = _schedule(controller, 'skip')
    repo.claim_due(601)
    assert repo.runs(row2['schedule_id'])[0]['state'] == 'skipped_missed'


def test_stable_task_id_survives_concurrent_retries_and_reopen(controller: object) -> None:
    scheduler = get_scheduling_service(controller)
    seen = []
    def dispatch(task_id: str) -> dict:
        seen.append(task_id)
        controller.db.update_task_status(task_id, 'running')
        return controller.db.get_task(task_id).to_dict()
    scheduler.dispatch_existing = dispatch
    payload = {'name': 'same', 'description': 'same', 'project_id': 'project'}
    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(lambda _: call_component(controller, IdempotentSubmissionService,
                    'submit', event_id='trigger:1', payload=payload), range(30)))
    assert len(seen) == 1 and len({r['task_id'] for r in results}) == 1
    controller.services.close()
    result = call_component(controller, IdempotentSubmissionService, 'submit', event_id='trigger:1', payload=payload)
    assert result['task_id'] == seen[0] and len(controller.db.list_tasks()) == 1
    with pytest.raises(ServiceError):
        call_component(controller, IdempotentSubmissionService, 'submit', event_id='trigger:1', payload={'description': 'changed'})


def test_interrupted_submission_is_not_automatically_dispatched(controller: object) -> None:
    scheduler = get_scheduling_service(controller)
    scheduler.dispatch_existing = Mock(side_effect=KeyboardInterrupt())
    payload = {'name': 'interrupt', 'description': 'work'}
    with pytest.raises(KeyboardInterrupt):
        call_component(controller, IdempotentSubmissionService, 'submit', event_id='crash', payload=payload)
    task = controller.db.list_tasks()[0]
    assert not can_dispatch(controller.db, task)
    controller.services.close()
    result = call_component(controller, IdempotentSubmissionService, 'submit', event_id='crash', payload=payload)
    assert result['status'] == 'review_required'
    assert controller.db.get_task(task.task_id).status == 'paused'
    assert len(controller.db.list_tasks()) == 1


def test_periodic_retry_before_dispatch_and_owner_migration(controller: object) -> None:
    row = _schedule(controller)
    controller.project_manager = SimpleNamespace(check_budget=Mock(return_value=False))
    call_component(controller, PeriodicService, 'pump', now=60)
    repo = PeriodicRepository(controller.db)
    assert repo.runs(row['schedule_id'])[0]['state'] == 'retry_wait'
    assert controller.db.list_tasks() == []
    controller.project_manager.check_budget.return_value = True
    scheduler = get_scheduling_service(controller)
    scheduler.dispatch_existing = lambda task_id: controller.db.get_task(task_id).to_dict()
    call_component(controller, PeriodicService, 'pump', now=63)
    run = repo.runs(row['schedule_id'])[0]
    assert run['state'] == 'submitted' and len(controller.db.list_tasks()) == 1
    with pytest.raises(ServiceError):
        call_component(controller, PeriodicService, 'history', schedule_id=row['schedule_id'], conv_id='foreign')
    migrated = call_component(controller, PeriodicService, 'update', schedule_id=row['schedule_id'],
                            conv_id='conv', payload={'target_conv_id': 'next', 'enabled': False})
    assert migrated['conv_id'] == 'next' and not migrated['enabled']


def _bind(controller: object) -> None:
    call_component(controller, ImService, 'bind', conv_id='conv', payload={'channel': 'telegram', 'room_id': 'known'})


def test_im_retries_only_invoke_shared_chat_once_and_keep_owner(controller: object) -> None:
    _bind(controller)
    payload = {'room_id': 'known', 'event_id': 'msg1', 'message': 'create task', 'conv_id': 'foreign', 'project_id': 'other'}
    with ThreadPoolExecutor(max_workers=8) as pool:
        replies = list(pool.map(lambda _: call_component(controller, ImService, 'receive', channel='telegram', payload=payload), range(24)))
    assert controller.chat_handler.chat.call_count == 1
    controller.chat_handler.chat.assert_called_with('create task', conv_id='conv')
    assert sum(not r['duplicate'] for r in replies) == 1
    controller.services.close()
    assert call_component(controller, ImService, 'receive', channel='telegram', payload=payload)['reply'] == 'done'
    assert controller.chat_handler.chat.call_count == 1
    with pytest.raises(ServiceError):
        call_component(controller, ImService, 'receive', channel='telegram', payload={**payload, 'message': 'changed'})
    with pytest.raises(ServiceError):
        call_component(controller, ImService, 'receive', channel='telegram', payload={**payload, 'room_id': 'unknown'})


def test_im_unknown_outcome_blocks_replay(controller: object) -> None:
    _bind(controller)
    controller.chat_handler.chat.side_effect = RuntimeError('lost response')
    event = dict(room_id='known', event_id='failed', message='start')
    assert call_component(controller, ImService, 'receive', channel='telegram', payload=event)['state'] == 'review_required'
    assert call_component(controller, ImService, 'receive', channel='telegram', payload=event)['duplicate']
    assert controller.chat_handler.chat.call_count == 1


def test_im_decision_cannot_target_another_conversation(controller: object) -> None:
    _bind(controller)
    reply = call_component(controller, ImService, 'receive', channel='telegram', payload={
        'room_id': 'known', 'event_id': 'decision', 'message': 'approve', 'kind': 'decision', 'pm_id': 'foreign'})
    assert reply['state'] == 'review_required' and not controller.inject_input_to_pm.called


def test_native_telegram_refuses_unknown_room_before_any_reply(controller: object, monkeypatch: pytest.MonkeyPatch) -> None:
    import threading
    gateway = SimpleNamespace(_chat_handler=SimpleNamespace(controller=controller), _lock=threading.RLock())
    channel = SimpleNamespace(chat_id='known', bot_token='test')
    post = Mock();monkeypatch.setattr('lan_mesh.bot_gateway.requests.post', post)
    assert receive_native(gateway, channel, {'room_id': 'stranger', 'event_id': 'e', 'message': 'run'})
    assert not post.called and not controller.chat_handler.chat.called


def test_telegram_reply_retry_does_not_repeat_task(controller: object, monkeypatch: pytest.MonkeyPatch) -> None:
    import threading
    _bind(controller)
    gateway = SimpleNamespace(_chat_handler=SimpleNamespace(controller=controller), _lock=threading.RLock())
    channel = SimpleNamespace(chat_id='known', bot_token='test')
    post = Mock(side_effect=[SimpleNamespace(status_code=429), SimpleNamespace(status_code=200, json=lambda: {'ok': True})])
    monkeypatch.setattr('lan_mesh.bot_gateway.requests.post', post)
    event = {'room_id': 'known', 'event_id': 'e', 'message': 'run'}
    receive_native(gateway, channel, event)
    with controller.db._get_conn() as conn:conn.execute('UPDATE im_reply_outbox SET retry_at=0')
    receive_native(gateway, channel, event)
    assert controller.chat_handler.chat.call_count == 1 and post.call_count == 2
    assert controller.db._get_conn().execute('SELECT state FROM im_reply_outbox').fetchone()[0] == 'sent'
    receive_native(gateway, channel, event)
    assert post.call_count == 2


def test_schedule_http_scope_and_disabled_secretary(controller: object) -> None:
    app = FastAPI();app.include_router(build_periodic_routes(controller))
    with TestClient(app) as client:
        response = client.post('/api/schedules?conv_id=conv', json={
            'name': 'Daily', 'template': {'description': 'report'}, 'spec': {'timezone': 'Asia/Shanghai'}})
        assert response.status_code == 200
        assert client.get('/api/schedules?conv_id=foreign').json()['items'] == []
        controller.secretary_active = False
        assert client.get('/api/schedules?conv_id=conv').status_code == 503


def test_http_roles_match_task_actions_and_im_binding_requires_boss(controller: object, monkeypatch: pytest.MonkeyPatch) -> None:
    from lan_mesh import station_routes_common as common
    monkeypatch.setattr(common, '_mesh_auth_enabled', True)
    monkeypatch.setattr(common, '_mesh_auth_token', 'mesh')
    monkeypatch.setattr(common, '_API_KEY', '')
    monkeypatch.setattr(common, '_users', {'configured': {}})
    monkeypatch.setattr(common, '_user_tokens', {'configured': {}})
    monkeypatch.setattr(common, '_rate_limiter', SimpleNamespace(is_allowed=lambda *a, **k: True))
    monkeypatch.setattr(common, 'resolve_role', lambda value: {'role': value} if value in {'boss', 'operator', 'viewer'} else None)
    app = FastAPI();app.middleware('http')(common.api_guard_middleware);app.include_router(build_periodic_routes(controller))
    with TestClient(app) as client:
        endpoint = '/api/station/im-bindings?conv_id=conv'
        body = {'channel': 'wecom', 'room_id': 'room'}
        assert client.post(endpoint, json=body).status_code == 401
        assert client.post(endpoint, json=body, headers={'Authorization': 'Bearer operator'}).status_code == 403
        assert client.post(endpoint, json=body, headers={'Authorization': 'Bearer boss'}).status_code == 200
        event = {'event_id': 'auth', 'room_id': 'room', 'message': 'run'}
        assert client.post('/api/im/wecom/messages', json=event, headers={'Authorization': 'Bearer viewer'}).status_code == 403
        assert client.post('/api/im/wecom/messages', json=event, headers={'Authorization': 'Bearer operator'}).status_code == 200
        assert controller.chat_handler.chat.call_count == 1


def test_im_history_does_not_follow_room_into_different_project(controller: object) -> None:
    _bind(controller)
    event = {'room_id': 'known', 'event_id': 'old', 'message': 'private'}
    call_component(controller, ImService, 'receive', channel='telegram', payload=event)
    call_component(controller, ImService, 'bind', conv_id='foreign', payload={'channel': 'telegram', 'room_id': 'known'})
    assert call_component(controller, ImService, 'events', conv_id='foreign')['items'] == []
    with pytest.raises(ServiceError):
        call_component(controller, ImService, 'receive', channel='telegram', payload=event)
    assert len(call_component(controller, ImService, 'events', conv_id='conv')['items']) == 1


def test_cancellation_between_creation_and_dispatch_is_preserved(controller: object) -> None:
    scheduler = get_scheduling_service(controller)
    create = scheduler._create_task
    def cancel_after_create(*args: object, **kwargs: object) -> Task:
        task = create(*args, **kwargs)
        controller.db.update_task_status(task.task_id, 'cancelled')
        return task
    scheduler._create_task = cancel_after_create
    scheduler._dispatch_task = Mock(side_effect=AssertionError('cancelled task dispatched'))
    result = call_component(controller, IdempotentSubmissionService, 'submit', event_id='cancel-race',
                            payload={'name': 'cancelled', 'description': 'work'})
    assert result['status'] == 'cancelled' and not scheduler._dispatch_task.called


def test_review_resolution_requires_terminal_task_and_records_user_note(controller: object) -> None:
    row = _schedule(controller)
    repo = PeriodicRepository(controller.db);repo.claim_due(60)
    run = repo.runs(row['schedule_id'])[0]
    controller.db.save_task(Task(task_id='review-task', status='paused'))
    repo.finish(run['run_id'], 'review_required', 'review-task')
    arguments = dict(schedule_id=row['schedule_id'], run_id=run['run_id'], conv_id='conv', payload={'note': '已核对远端任务'})
    with pytest.raises(ServiceError):call_component(controller, PeriodicService, 'resolve_run', **arguments)
    controller.db.update_task_status('review-task', 'cancelled')
    assert call_component(controller, PeriodicService, 'resolve_run', **arguments)['state'] == 'reviewed'
    assert repo.runs(row['schedule_id'])[0]['detail'] == '已核对远端任务'
