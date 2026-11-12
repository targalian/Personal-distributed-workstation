"""Acceptance provenance and durable per-attempt accounting regression scenarios."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator
import json
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lan_mesh.acceptance_evidence import AcceptanceEvidenceService
from lan_mesh.agent_runtime import AgentRuntime
from lan_mesh.component_service import call_component
from lan_mesh.database import Database
from lan_mesh.model_resources import ModelResource, ModelResourceManager, _get_usage_context, usage_context
from lan_mesh.pm_planner import PMPlanner, _parse_acceptance_review
from lan_mesh.pm_state import PMState
from lan_mesh.runtime_artifacts import RuntimeArtifacts
from lan_mesh.runtime_usage import attempt_scope, call_for_task, current_usage_metadata
from lan_mesh.usage_audit_routes import build_usage_audit_routes


@pytest.fixture
def ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple]:
    monkeypatch.setattr(Database, 'backup', lambda *a, **k: '')
    db = Database(str(tmp_path / 'usage.db'))
    manager = ModelResourceManager()
    manager._db, manager._enabled = db, True
    manager._resources = {'pool': ModelResource('pool', 'local', 'token_plan', 100000, models=['test'])}
    runtime = AgentRuntime('usage-agent', str(tmp_path), resource_manager=manager)
    yield db, manager, runtime
    if vars(runtime).get('services'):
        runtime.services.close()
    if vars(manager).get('services'):
        manager.services.close()


def _response(usage: dict, **message: Any) -> Any:
    return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {
        'choices': [{'message': message or {'content': 'done'}, 'finish_reason': 'stop'}], 'usage': usage})


@pytest.mark.parametrize('outcome', ['completed', 'cancelled', 'invalid_response', 'connection_failed'])
def test_transport_records_once_even_when_failed_or_cancelled(ledger: tuple, monkeypatch: pytest.MonkeyPatch, outcome: str) -> None:
    import lan_mesh.agent_runtime as policy
    db, manager, runtime = ledger
    usage = {'prompt_tokens': 13, 'completion_tokens': 7}
    def post(*a: Any, **k: Any) -> Any:
        if outcome == 'connection_failed':
            raise policy.requests.ConnectionError('offline')
        if outcome == 'cancelled':
            runtime.cancel()
        if outcome == 'invalid_response':
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {'usage': usage, 'choices': []})
        return _response(usage)
    monkeypatch.setattr(policy.requests, 'post', post)
    with usage_context('task', 'project'), attempt_scope('attempt', 'execution'):
        try:
            runtime._call_openai_with_tools([], [], 'test', 'http://unused', '')
        except (IndexError, policy.requests.ConnectionError):
            assert outcome in ('invalid_response', 'connection_failed')
    rows = db.query_unreported_usage()
    assert len(rows) == 1 and rows[0]['task_id'] == 'task'
    metadata = rows[0]['usage_metadata']
    assert metadata['attempt_id'] == 'attempt'
    assert metadata['outcome'] == ('failed' if outcome in ('invalid_response', 'connection_failed') else outcome)
    assert metadata['usage_known'] == (outcome != 'connection_failed')
    assert manager.get_usage('pool')['used'] == (0 if outcome == 'connection_failed' else 20)
    assert current_usage_metadata() == {} and _get_usage_context() == ('', '')


def test_actual_retries_have_distinct_call_ids_but_same_attempt(ledger: tuple, monkeypatch: pytest.MonkeyPatch) -> None:
    import lan_mesh.agent_runtime as policy
    db, manager, runtime = ledger
    monkeypatch.setattr(policy.requests, 'post', lambda *a, **k: _response({'prompt_tokens': 5, 'completion_tokens': 2}))
    with attempt_scope('same-attempt'):
        runtime._call_openai_with_tools([], [], 'test', 'http://unused', '')
        runtime._call_openai_with_tools([], [], 'test', 'http://unused', '')
    rows = db.query_unreported_usage()
    assert len({r['usage_id'] for r in rows}) == 2
    assert {r['usage_metadata']['attempt_id'] for r in rows} == {'same-attempt'}
    assert manager.get_usage('pool')['used'] == 14


def test_usage_report_is_idempotent_across_concurrent_replay_and_restart(ledger: tuple) -> None:
    db, manager, _ = ledger
    uid = uuid.uuid4().hex
    metadata = dict(attempt_id='attempt', purpose='acceptance_review', outcome='cancelled', usage_known=True)
    def write(_: int) -> bool:
        return db.insert_resource_usage('pool', 'test', 'token_plan', 11, 3, 0,
                usage_id=uid, task_id='task', project_id='project', usage_metadata=metadata)
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(write, range(24))) == 1
    reopened = Database(str(db.path))
    assert reopened.insert_resource_usage('pool', 'test', 'token_plan', 11, 3, 0,
                usage_id=uid, task_id='task', project_id='project', usage_metadata=metadata) is False
    assert manager.get_usage('pool')['used'] == 14
    assert reopened.repositories.resources.query_attempt_usage('task', 'wrong') == []
    summary = reopened.repositories.resources.query_attempt_usage('task', 'project')[0]
    assert summary['outcome'] == 'cancelled' and summary['reported_tokens'] == 14


def test_auxiliary_calls_inherit_task_and_restore_outer_scope(ledger: tuple, monkeypatch: pytest.MonkeyPatch) -> None:
    import lan_mesh.agent_runtime as policy
    db, _, runtime = ledger
    monkeypatch.setattr(policy.requests, 'post', lambda *a, **k: _response({'prompt_tokens': 3, 'completion_tokens': 1}))
    monkeypatch.setattr(runtime, '_call_llm_with_routing', lambda *a: runtime._call_openai_with_tools([], [], 'test', 'http://unused', ''))
    with usage_context('outer', 'outer-project'), attempt_scope('outer-attempt'):
        for purpose in ('planning', 'quality_review', 'aggregation', 'acceptance_review'):
            call_for_task(runtime, 'work', {}, {'task_id': 'task', 'project_id': 'project'}, purpose)
        assert _get_usage_context() == ('outer', 'outer-project')
    rows = db.query_unreported_usage()
    assert len(rows) == 4 and {r['task_id'] for r in rows} == {'task'}
    assert {r['usage_metadata']['purpose'] for r in rows} == {'planning', 'quality_review', 'aggregation', 'acceptance_review'}


def test_stream_failure_keeps_reported_usage_and_never_invents_tokens(ledger: tuple, monkeypatch: pytest.MonkeyPatch) -> None:
    import lan_mesh.agent_runtime as policy
    db, manager, runtime = ledger
    def lines(**kw: Any) -> Iterator[str]:
        yield 'data: ' + json.dumps({'choices': [{'delta': {'content': 'partial'}}],
                                    'usage': {'prompt_tokens': 10, 'completion_tokens': 2}})
        raise policy.requests.exceptions.ChunkedEncodingError('connection lost')
    response = SimpleNamespace(raise_for_status=lambda: None, close=lambda: None,
                               headers={}, iter_lines=lines)
    monkeypatch.setattr(policy.requests, 'post', lambda *a, **k: response)
    result = runtime._call_openai_compatible('prompt', 'test', 'http://unused', '')
    assert result['status'] == 'failed' and result['content'] == 'partial'
    assert manager.get_usage('pool')['used'] == 12
    assert db.query_unreported_usage()[0]['usage_metadata']['outcome'] == 'failed'
    response.iter_lines = lambda **k: iter(['data: {"choices":[{"delta":{"content":"hello"}}]}', 'data: [DONE]'])
    result = runtime._call_openai_compatible('prompt', 'test', 'http://unused', '')
    assert result['output_tokens'] == 0
    assert db.query_unreported_usage()[-1]['usage_metadata']['usage_known'] == 0


def test_http_and_ws_ack_only_commit_accepted_ids(ledger: tuple, monkeypatch: pytest.MonkeyPatch) -> None:
    import requests
    db, manager, _ = ledger
    manager.record_usage('test', 2, 1, usage_id='accepted', usage_metadata={'attempt_id': 'a'})
    manager.record_usage('test', 3, 1, usage_id='pending', usage_metadata={'attempt_id': 'a'})
    manager._secretary_url = 'http://unused'
    manager._ws_token = uuid.uuid4().hex
    seen = []
    def post(*a: Any, **k: Any) -> Any:
        seen.append(k)
        return SimpleNamespace(raise_for_status=lambda: None, text='yes', json=lambda: {'accepted_ids': ['accepted']})
    monkeypatch.setattr(requests, 'post', post)
    manager.report_once()
    assert [r['usage_id'] for r in db.query_unreported_usage()] == ['pending']
    assert seen[0]['headers']['Authorization'].endswith(manager._ws_token)
    assert all(r['usage_metadata']['attempt_id'] == 'a' for r in seen[0]['json']['records'])
    conn = SimpleNamespace(send=lambda value: seen.append(json.loads(value)),
                           recv=lambda **k: json.dumps({'ok': True, 'accepted_ids': []}))
    manager._push_once_ws(conn)
    assert [r['usage_id'] for r in db.query_unreported_usage()] == ['pending']


def test_batch_replays_retain_metadata_and_return_ids(ledger: tuple, monkeypatch: pytest.MonkeyPatch) -> None:
    import lan_mesh.model_resources as policy
    from lan_mesh.resource_usage_batch import apply_usage_batch
    db, manager, _ = ledger
    monkeypatch.setattr(policy, '_mgr', manager)
    records = [dict(model='test', input_tokens=8, output_tokens=1, usage_id='call-a',
                    usage_metadata=dict(attempt_id='attempt-a', outcome='failed', usage_known=True))]
    assert apply_usage_batch(records)['recorded'] == 1
    replay = apply_usage_batch(records)
    assert replay['duplicate'] == 1 and replay['accepted_ids'] == ['call-a']
    assert db.query_unreported_usage()[0]['usage_metadata']['outcome'] == 'failed'


def test_each_criterion_kept_and_string_false_is_not_true() -> None:
    result = _parse_acceptance_review(json.dumps({'results': [{'met': 'false'}, {'met': True}]}), ['A', 'B', 'C'])
    assert [r['met'] for r in result['checks']] == [False, True, False]
    assert result['reviewed'] == 3 and result['unmet'] == ['A', 'C']
    result = _parse_acceptance_review('{"results":[{"index":9,"met":true}]}', ['A'])
    assert result['checks'][0]['met'] is False


def test_acceptance_uses_all_criteria_and_labels_self_assessment(ledger: tuple, monkeypatch: pytest.MonkeyPatch) -> None:
    _, _, runtime = ledger
    planner = PMPlanner('pm', runtime, PMState(), SimpleNamespace(secretary_url='http://unused'))
    criteria = [f'验收条件{i}' for i in range(9)]
    monkeypatch.setattr(planner, '_fetch_project_blueprint', lambda _: {'charter': {'acceptance_criteria': criteria}})
    monkeypatch.setattr(runtime, '_call_llm_with_routing', lambda *a: {'content': '{"results":[{"met":true}]}'})
    review = planner.review_against_blueprint({'task_id': 'task', 'project_id': 'project'}, '交付物')
    assert review['reviewed'] == 9 and len(review['unmet']) == 8
    assert review['independent_verification'] == 'not_performed'
    assert all(c['source_refs'] and c['assessment_kind'] == 'model_self_review' for c in review['checks'])
    ref = review['checks'][0]['source_refs'][0]
    from lan_mesh.runtime_artifacts import artifact_root
    store = RuntimeArtifacts(artifact_root(runtime.shared_folder), ref['execution_id'], ref['scope'])
    assert store.read(ref['artifact_id'])['content'] == '交付物'


def test_only_matching_checksum_verified_artifacts_enter_evidence_catalog(ledger: tuple) -> None:
    _, _, runtime = ledger
    scope = dict(agent_id='worker', task_id='task', project_id='project', attempt_id='try')
    store = RuntimeArtifacts.create(runtime.shared_folder, scope)
    reference = store.save('tool_result', {'observation': 'pytest: 5 passed'})
    output = dict(execution={'execution_id': store.execution_id, 'scope': scope},
                  steps=[{'artifact': reference, 'tool': 'shell_exec'}])
    task = dict(task_id='task', project_id='project')
    catalog = call_component(runtime, AcceptanceEvidenceService, 'catalog', task=task, outputs={'sub': output})
    assert len(catalog) == 1 and catalog[0]['checksum_verified']
    review = {'checks': [{'met': True, 'evidence_ids': [reference['artifact_id'], 'invented']}]}
    bound = call_component(runtime, AcceptanceEvidenceService, 'bind', task=task, criteria=['A'],
                           deliverable='结果', review=review, catalog=catalog)
    assert len(bound['checks'][0]['source_refs']) == 2
    assert bound['checks'][0]['verification_status'] == 'observations_linked'
    assert bound['independent_verification'] == 'not_performed'
    assert call_component(runtime, AcceptanceEvidenceService, 'catalog', task={**task, 'project_id': 'other'}, outputs={'sub': output}) == []
    (store.directory / (reference['artifact_id'] + '.json')).write_text('{}')
    assert call_component(runtime, AcceptanceEvidenceService, 'catalog', task=task, outputs={'sub': output}) == []


def test_review_model_failure_marks_all_criteria_unverified(ledger: tuple, monkeypatch: pytest.MonkeyPatch) -> None:
    _, _, runtime = ledger
    planner = PMPlanner('pm', runtime, PMState(), SimpleNamespace())
    monkeypatch.setattr(planner, '_blueprint_acceptance_criteria', lambda _: ['A', 'B'])
    monkeypatch.setattr(runtime, '_call_llm_with_routing', lambda *a: (_ for _ in ()).throw(TimeoutError()))
    result = planner.review_against_blueprint({'task_id': 't'}, 'result')
    assert result['reviewed'] == 2 and result['unmet'] == ['A', 'B']
    assert all(not c['evidence_verified'] for c in result['checks'])


def test_attempt_audit_api_filters_task_and_project(ledger: tuple) -> None:
    db, manager, _ = ledger
    manager.record_usage('test', 5, 1, task_id='task', project_id='project',
                         usage_metadata={'attempt_id': 'attempt', 'outcome': 'failed', 'usage_known': False})
    ctl = SimpleNamespace(db=db, secretary_active=True)
    app = FastAPI()
    app.include_router(build_usage_audit_routes(ctl))
    try:
        with TestClient(app) as client:
            path = '/api/resources/usage/attempts'
            assert client.get(path, params=dict(task_id='task', project_id='other')).json()['items'] == []
            row = client.get(path, params=dict(task_id='task', project_id='project')).json()['items'][0]
            assert row['unknown_usage_calls'] == 1 and row['outcome'] == 'failed'
            ctl.secretary_active = False
            assert client.get(path, params=dict(task_id='task', project_id='project')).status_code == 503
    finally:
        ctl.services.close()
