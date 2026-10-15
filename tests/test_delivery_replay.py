"""Concurrent delivery retries must preserve the reviewed artifact and acceptance decision."""
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lan_mesh.chat_handler import ChatHandler
from lan_mesh.database import Database
from lan_mesh.protocol import Task
from lan_mesh.station_routes_tasks import build_task_routes


@pytest.fixture
def store(tmp_path):
    db = Database(str(tmp_path / 'replay.sqlite3'))
    db.save_task(Task(task_id='task', name='项目', pm_agent_id='pm', status='running',
                      input_data={'_brief': {'revision': 3}}, output_data={'existing': 'keep'}))
    return db


def delivery(**changes):
    return {'pm_id': 'pm', 'deliverable': '产物正文', 'summary': '摘要',
            'subtask_stats': {'completed': 2}, 'delivered_at': 100, **changes}


def client_for(store):
    controller = SimpleNamespace(db=store, state=SimpleNamespace(ws_clients=set()),
                                 secretary_active=True, discovery=Mock(), bot_gateway=Mock())
    app = FastAPI()
    app.include_router(build_task_routes(controller))
    return TestClient(app), controller


@pytest.mark.parametrize('accepted', [True, False])
def test_retry_preserves_review_and_first_timestamp(store, accepted):
    store.record_task_delivery('task', 'pm', delivery())
    store.review_task_delivery('task', accepted, '补充验证')
    original = store.get_task('task')
    result = store.record_task_delivery('task', 'pm', delivery(delivered_at=999))
    current = store.get_task('task')
    assert result == {'duplicate': True, 'accepted': accepted}
    assert current.output_data == original.output_data
    assert current.status == original.status
    assert current.input_data['_brief']['revision'] == 3


@pytest.mark.parametrize('field,value', [('deliverable', '新内容'), ('summary', '新摘要'),
                                         ('subtask_stats', {'completed': 3}), ('pm_id', 'other')])
def test_conflicting_delivery_is_rejected_without_overwriting(store, field, value):
    store.record_task_delivery('task', 'pm', delivery())
    with pytest.raises(ValueError):
        store.record_task_delivery('task', 'pm', delivery(**{field: value}))
    assert store.get_task('task').output_data['_delivery']['deliverable'] == '产物正文'


def test_concurrent_duplicate_delivery_is_created_once(store):
    other = Database(str(store.path))
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda index: (store if index % 2 else other).record_task_delivery(
            'task', 'pm', delivery()), range(16)))
    assert sum(not result['duplicate'] for result in results) == 1
    assert store.get_task('task').output_data['existing'] == 'keep'


def test_concurrent_different_deliveries_have_one_winner(store):
    def submit(index):
        try:
            store.record_task_delivery('task', 'pm', delivery(deliverable=f'candidate {index}'))
            return index
        except ValueError:
            return None
    with ThreadPoolExecutor(max_workers=8) as pool:
        winners = [result for result in pool.map(submit, range(8)) if result is not None]
    assert len(winners) == 1
    assert store.get_task('task').output_data['_delivery']['deliverable'] == f'candidate {winners[0]}'


@pytest.mark.parametrize('accepted', [True, False])
def test_concurrent_retry_review_and_stale_task_save_preserve_decision(store, accepted):
    stale = store.get_task('task')
    store.record_task_delivery('task', 'pm', delivery())
    def mutate(index):
        if index == 0:
            return store.review_task_delivery('task', accepted, '检查未通过')
        if index % 2:
            return store.record_task_delivery('task', 'pm', delivery(delivered_at=index))
        # Each writer holds a pre-delivery snapshot, like a delayed startup response.
        task = Task.from_dict(stale.to_dict())
        task.output_data = {'startup': 'late'}
        store.save_task(task)
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(mutate, range(16)))
    current = store.get_task('task')
    assert current.status == ('accepted' if accepted else 'rejected')
    assert current.output_data['_delivery']['accepted'] is accepted
    assert current.output_data['_delivery']['deliverable'] == '产物正文'


def test_replay_survives_database_reopen(store):
    store.record_task_delivery('task', 'pm', delivery())
    store.review_task_delivery('task', False, '需要测试')
    reopened = Database(str(store.path))
    assert reopened.record_task_delivery('task', 'pm', delivery())['duplicate']
    assert reopened.get_task('task').output_data['_delivery']['reject_reason'] == '需要测试'


def test_api_retry_does_not_notify_again_and_conflict_is_visible(store):
    client, controller = client_for(store)
    payload = {'task_id': 'task', **delivery()}
    assert client.post('/api/pm/pm/deliver', json=payload).status_code == 200
    assert client.post('/api/tasks/task/accept').status_code == 200
    repeated = client.post('/api/pm/pm/deliver', json=payload)
    assert repeated.json()['duplicate'] and repeated.json()['accepted']
    assert client.post('/api/pm/pm/deliver', json={**payload, 'deliverable': '不同内容'}).status_code == 409
    assert controller.bot_gateway.notify.call_count == 1
    assert store.get_task('task').status == 'accepted'


def test_accepting_after_rejection_clears_stale_rejection_reason(store):
    store.record_task_delivery('task', 'pm', delivery())
    store.review_task_delivery('task', False, '上次退回')
    store.review_task_delivery('task', True)
    assert 'reject_reason' not in store.get_task('task').output_data['_delivery']


def test_review_and_retry_preserve_concurrent_subtask_updates(store):
    store.record_task_delivery('task', 'pm', delivery())
    subtasks = [{'name': 'A', 'status': 'completed'}]
    with ThreadPoolExecutor(max_workers=3) as pool:
        operations = [pool.submit(store.review_task_delivery, 'task', True),
                      pool.submit(store.update_task_subtasks, 'task', subtasks),
                      pool.submit(store.record_task_delivery, 'task', 'pm', delivery())]
        for operation in operations:
            operation.result()
    task = store.get_task('task')
    assert task.subtasks == subtasks
    assert task.status == 'accepted'
    assert task.output_data['existing'] == 'keep'


@pytest.mark.parametrize('accepted', [True, False])
def test_chat_uses_same_review_states_as_api(store, accepted):
    store.record_task_delivery('task', 'pm', delivery())
    handler = ChatHandler.__new__(ChatHandler)
    handler.controller = SimpleNamespace(db=store, inject_input_to_pm=Mock())
    if accepted:
        reply = handler._action_accept_delivery('验收')
        assert 'accepted' in reply
    else:
        reply = handler._action_reject_delivery('退回 补充测试')
        assert '重新执行' not in reply
    assert store.get_task('task').status == ('accepted' if accepted else 'rejected')
    handler.controller.inject_input_to_pm.assert_not_called()


def test_failed_delivery_transaction_rolls_back_and_next_request_works(store):
    conn = store._get_conn()
    conn.executescript("CREATE TRIGGER fail_write BEFORE UPDATE OF output_data ON tasks "
                       "BEGIN SELECT RAISE(ABORT, 'write failure'); END;")
    with pytest.raises(Exception, match='write failure'):
        store.record_task_delivery('task', 'pm', delivery())
    assert '_delivery' not in store.get_task('task').output_data
    conn.execute('DROP TRIGGER fail_write')
    conn.commit()
    assert store.record_task_delivery('task', 'pm', delivery())['duplicate'] is False
