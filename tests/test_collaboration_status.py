"""Real Database semantics; HTTP cases must run outside the socket-restricted sandbox."""
import json
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from lan_mesh.database import Database
from lan_mesh.protocol import Task
from lan_mesh.collaboration_repository import CollaborationRepository
from lan_mesh.collaboration_service import CollaborationService


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(Database, 'backup', lambda self: '')
    return Database(str(tmp_path / 'status.db'))


def status(db, raw='completed', delivery=None, state=None):
    task = Task(task_id='t', status=raw, pm_agent_id='pm', output_data={'_delivery': delivery or {}})
    return CollaborationService._status(task, state or {}, db, {})


@pytest.mark.parametrize('raw,code', [('completed','undelivered'),('failed','failed'),
    ('cancelled','cancelled'),('running','running'),('pending','pending'),('paused','paused')])
def test_execution_is_independent(db, raw, code):
    result = status(db, raw)
    assert result['execution'] == raw
    assert result['code'] == code
    assert result['acceptance'] == 'unknown'


@pytest.mark.parametrize('accepted,expected', [(None,'unknown'),(True,'accepted'),(False,'rejected')])
def test_station_receipt_is_not_acceptance_or_read(db, accepted, expected):
    result = status(db, delivery=dict(pm_id='pm', delivered_at=1, deliverable='x', accepted=accepted))
    assert result['delivery'] == 'station_received'
    assert result['acceptance'] == expected
    assert result['read'] == 'unknown'


def test_old_pm_delivery_excluded(db):
    assert status(db, delivery=dict(pm_id='old', delivered_at=1, deliverable='x'))['delivery'] == 'undelivered'


@pytest.mark.parametrize('state,code', [({'review_required': {'sub':'ambiguous'}},'review_required'),
    ({'clarification_question':'which?'},'awaiting_input'), ({'interrupted':True},'interrupted')])
def test_snapshot_priority(db, state, code):
    assert status(db, state=state)['code'] == code


def test_transport_only_current_parent_and_attempt(db):
    conn = db._get_conn()
    for i, parent, attempt in [('good','t','a'),('other','other','a'),('old','t','old')]:
        conn.execute('INSERT INTO mesh_delivery_outbox(delivery_id,payload_json,state) VALUES (?,?,?)',
            (i,json.dumps(dict(parent_task_id=parent,subtask_id='s',attempt_id=attempt)), 'acked'))
    conn.commit()
    result = status(db, state={'active_attempts': {'sub': {'subtask_id':'s','attempt_id':'a'}}})
    assert [r['delivery_id'] for r in result['subtask_transport']] == ['good']
    assert result['delivery'] == 'undelivered'


def test_dedup_read_reopen_and_recurrence(db):
    repo = CollaborationRepository(db)
    assert repo.transition('t','execution','completed','done')
    assert not repo.transition('t','execution','completed','done')
    assert repo.mark_read('t') == 1
    reopened = CollaborationRepository(Database(str(db.path)))
    assert reopened.unread_count('t') == 0
    reopened.transition('t','execution','running','running')
    reopened.mark_read('t')
    assert reopened.transition('t','execution','completed','done')
    assert reopened.unread_count('t') == 1


def test_save_task_notifies_and_failure_isolated(db, monkeypatch):
    task = Task(task_id='t', status='completed')
    db.save_task(task)
    assert CollaborationRepository(db).unread_count('t') == 1
    db.save_task(task)
    assert CollaborationRepository(db).unread_count('t') == 1
    monkeypatch.setattr(CollaborationRepository, 'transition', Mock(side_effect=RuntimeError('broken')))
    db.update_task_status('t','failed')
    assert db.get_task('t').status == 'failed'
    task.status = 'cancelled'
    db.save_task(task)
    assert db.get_task('t').status == 'cancelled'


def test_status_read_no_writes(db):
    conn = db._get_conn()
    before = conn.total_changes
    status(db)
    status(db)
    assert conn.total_changes == before
    assert not conn.in_transaction


def test_http_overview_delivery_and_permissions(db, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from lan_mesh import station_routes_common as common
    from lan_mesh.collaboration_routes import build_collaboration_routes
    from lan_mesh.station_routes_tasks import build_task_routes
    from unittest.mock import AsyncMock
    owner = SimpleNamespace(db=db, secretary_active=True, state=SimpleNamespace(),
        get_task_graph_data=lambda _: {}, chat_handler=None, bot_gateway=SimpleNamespace(notify=Mock(side_effect=RuntimeError('offline'))))
    db.save_task(Task(task_id='t',status='completed',pm_agent_id='pm'))
    for key, value in {'_mesh_auth_enabled':True, '_mesh_auth_token':'mesh', '_API_KEY':'',
        '_users':{'configured':{}}, '_user_tokens':{'configured':{}},
        '_rate_limiter':SimpleNamespace(is_allowed=lambda *a, **k: True)}.items():
        monkeypatch.setattr(common, key, value)
    monkeypatch.setattr(common,'resolve_role',lambda token: {'role':token} if token in {'viewer','operator'} else None)
    app = FastAPI()
    app.middleware('http')(common.api_guard_middleware)
    app.include_router(build_collaboration_routes(owner))
    app.include_router(build_task_routes(owner))
    try:
        with TestClient(app) as client:
            viewer={'Authorization':'Bearer viewer'}
            operator={'Authorization':'Bearer operator'}
            before = len(CollaborationRepository(db).notices('t'))
            assert client.get('/api/collaboration/t',headers=viewer).status_code == 200
            assert len(CollaborationRepository(db).notices('t')) == before
            assert client.post('/api/collaboration/t/read',json={},headers=viewer).status_code == 403
            assert client.post('/api/collaboration/t/read',json={},headers=operator).status_code == 200
            response = client.post('/api/pm/pm/deliver',json={'task_id':'t','deliverable':'result'},headers=operator)
            assert response.status_code == 200
            assert db.get_task('t').output_data['_delivery']['accepted'] is None
            assert client.get('/api/collaboration/t',headers=viewer).json()['collaboration_status']['delivery'] == 'station_received'
    finally:
        owner.services.close()
