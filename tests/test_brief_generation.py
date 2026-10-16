"""Background generation: paid request deduplication, crash evidence and edit safety."""
import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lan_mesh.brief_generation import BriefGeneration
from lan_mesh.chat_handler import ChatHandler
from lan_mesh.project_brief import RevisionConflict
from lan_mesh.station_routes_briefs import build_brief_routes
from tests.test_project_brief import chat, document


def finish(chat, cid, request_id='request-one'):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        job = chat.brief_generation.get(cid, request_id)['job']
        if job['status'] not in {'queued', 'running'}:
            return job
        time.sleep(.01)
    pytest.fail('generation did not reach a terminal state')


def hold_model(chat):
    entered, release = threading.Event(), threading.Event()
    def call(*args):
        entered.set()
        assert release.wait(5)
        return {'content': json.dumps({'reply': '建议', 'project_brief': document()})}
    chat.runtime._call_llm_with_routing.side_effect = call
    return entered, release


def test_concurrent_replays_and_new_handler_do_not_repeat_model(chat):
    cid = chat._active_conv_id
    entered, release = hold_model(chat)
    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            replies = list(pool.map(lambda _: chat.brief_generation.start(cid, 'request-one', 0), range(8)))
        assert entered.wait(2)
        assert {r['job']['id'] for r in replies} == {'request-one'}
        other = ChatHandler(chat.runtime, chat.controller, shared_folder=chat._shared_folder)
        assert other.briefs.lock is chat.briefs.lock
        assert other.brief_generation.get(cid)['job']['status'] == 'running'
        assert other.brief_generation.start(cid, 'request-one', 0)['job']['id'] == 'request-one'
        with pytest.raises(RevisionConflict):
            other.brief_generation.start(cid, 'request-two', 0)
        with pytest.raises(RevisionConflict):
            other.brief_generation.start(cid, 'request-one', 1)
    finally:
        release.set()
    job = finish(chat, cid)
    assert job['status'] == 'review'
    assert chat.runtime._call_llm_with_routing.call_count == 1
    current = chat.briefs.get(cid)
    assert current['revision'] == 0
    assert current['proposal']['id'] == job['proposal_id']
    assert '_generation_jobs' not in current and 'owner' not in job
    chat.briefs.review(cid, job['proposal_id'], 0, ['title'])
    # Late replay after review still returns the original job; it cannot regenerate.
    assert chat.brief_generation.start(cid, 'request-one', 0)['job']['id'] == 'request-one'
    assert chat.runtime._call_llm_with_routing.call_count == 1


@pytest.mark.parametrize('mutation', ['save', 'proposal', 'delete'])
def test_changes_during_generation_preserve_current_document(chat, mutation):
    cid = chat._active_conv_id
    entered, release = hold_model(chat)
    try:
        chat.brief_generation.start(cid, 'request-one', 0)
        assert entered.wait(2)
        if mutation == 'save':
            chat.briefs.save(cid, {**document(), 'title': '人工新稿'}, 0)
        elif mutation == 'proposal':
            chat.briefs.propose(cid, {**document(), 'title': '聊天新建议'}, 0)
        else:
            chat.delete_conversation(cid)
        before = chat.briefs.get(cid)
    finally:
        release.set()
    job = finish(chat, cid)
    assert chat.briefs.get(cid) == before
    if mutation == 'delete':
        assert job['status'] == 'failed'
    else:
        assert job['status'] == 'conflict'
        assert '协作工作台' in chat.brief_generation.candidate_markdown(cid, 'request-one')
        # Manual save and revision restore must not drop the idempotency ledger.
        current = chat.briefs.save(cid, before['document'], before['revision'])
        chat.briefs.restore(cid, current['revision'], current['revision'])
        assert chat.brief_generation.get(cid)['job']['status'] == 'conflict'


@pytest.mark.parametrize('reply', ['', '{invalid', json.dumps({'reply': 'ok', 'project_brief': {'nodes': [{'id': 'bad id'}]}})])
def test_invalid_model_reply_is_terminal_and_never_applies(chat, reply):
    cid = chat._active_conv_id
    chat.runtime._call_llm_with_routing.return_value = {'content': reply}
    chat.brief_generation.start(cid, 'request-one', 0)
    assert finish(chat, cid)['status'] == 'failed'
    assert chat.briefs.get(cid)['proposal'] is None
    chat.brief_generation.start(cid, 'request-one', 0)
    assert chat.runtime._call_llm_with_routing.call_count == 1


def test_no_changes_and_provider_error_do_not_expose_secrets(chat):
    cid = chat._active_conv_id
    chat.briefs.save(cid, document(), 0)
    chat.runtime._call_llm_with_routing.return_value = {'content': json.dumps({'reply': 'ok', 'project_brief': document()})}
    chat.brief_generation.start(cid, 'request-one', 1)
    assert finish(chat, cid)['status'] == 'no_changes'
    chat.runtime._call_llm_with_routing.side_effect = RuntimeError('private-provider-credential')
    chat.brief_generation.start(cid, 'request-two', 1)
    result = finish(chat, cid, 'request-two')
    assert result['status'] == 'failed'
    assert 'private-provider-credential' not in json.dumps(result)


def test_failed_atomic_completion_never_publishes_half_result(chat, monkeypatch):
    cid = chat._active_conv_id
    entered, release = hold_model(chat)
    original = chat.briefs._write
    def fail_completion(*args, **kwargs):
        if any(j['status'] == 'review' for j in kwargs.get('generation_jobs', {}).values()):
            raise OSError('disk full')
        return original(*args, **kwargs)
    try:
        chat.brief_generation.start(cid, 'request-one', 0)
        assert entered.wait(2)
        monkeypatch.setattr(chat.briefs, '_write', fail_completion)
    finally:
        release.set()
    assert finish(chat, cid)['status'] == 'failed'
    assert chat.briefs.get(cid)['proposal'] is None
    assert chat.briefs.get(cid)['revision'] == 0


def test_failed_submission_does_not_call_model(chat, monkeypatch):
    monkeypatch.setattr(chat.briefs, '_write', Mock(side_effect=OSError('disk full')))
    with pytest.raises(OSError):
        chat.brief_generation.start(chat._active_conv_id, 'request-one', 0)
    chat.runtime._call_llm_with_routing.assert_not_called()


def test_api_status_validation_and_conversation_scope(chat):
    app = FastAPI()
    app.include_router(build_brief_routes(chat.controller))
    with TestClient(app) as client:
        base = f'/api/conversations/{chat._active_conv_id}/brief/generation'
        assert client.get(base).json() == {'job': None}
        assert client.get(base, params={'request_id': 'missing'}).status_code == 404
        assert client.post(base, json={'request_id': '../x', 'expected_revision': 0}).status_code == 422
        chat.runtime._call_llm_with_routing.return_value = {'content': json.dumps({'reply': 'ok', 'project_brief': document()})}
        response = client.post(base, json={'request_id': 'request-one', 'expected_revision': 0})
        assert response.status_code == 202
        finish(chat, chat._active_conv_id)
        assert client.get(base).json()['job']['status'] == 'review'
        assert client.get(base+'/request-one/markdown').status_code == 404
        other = chat.create_conversation('other')['id']
        assert client.get(f'/api/conversations/{other}/brief/generation').json() == {'job': None}
        chat.controller.secretary_active = False
        assert client.get(base).status_code == 503


def test_real_process_exit_records_unknown_and_never_replays(tmp_path):
    script = r'''
import json, sys, threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from lan_mesh.chat_handler import ChatHandler
root=Path(sys.argv[1])
def model(*args):
    (root/'entered').write_text('yes')
    threading.Event().wait(60)
runtime=Mock(); runtime._call_llm_with_routing.side_effect=model
chat=ChatHandler(runtime,SimpleNamespace(_default_model=''),shared_folder=SimpleNamespace(path=str(root)))
chat._build_status_context=lambda: ''
(root/'cid').write_text(chat._active_conv_id)
chat.brief_generation.start(chat._active_conv_id,'request-one',0)
threading.Event().wait(60)
'''
    env = {k: v for k, v in os.environ.items() if k not in {'PYTHONPATH', 'PYTHONHOME'}}
    child = subprocess.Popen([sys.executable, '-c', script, str(tmp_path)], env=env)
    try:
        deadline = time.monotonic() + 10
        while not (tmp_path/'entered').exists() and time.monotonic() < deadline:
            assert child.poll() is None
            time.sleep(.02)
        assert (tmp_path/'entered').exists()
        runtime = Mock()
        chat = ChatHandler(runtime, SimpleNamespace(_default_model=''), shared_folder=SimpleNamespace(path=str(tmp_path)))
        cid = (tmp_path/'cid').read_text()
        with pytest.raises(RevisionConflict, match='另一个进程'):
            chat.brief_generation.get(cid)
        child.kill()
        child.wait(timeout=5)
        job = chat.brief_generation.get(cid)['job']
        assert job['status'] == 'interrupted'
        assert chat.brief_generation.start(cid, 'request-one', 0)['job']['status'] == 'interrupted'
        runtime._call_llm_with_routing.assert_not_called()
        assert chat.briefs.get(cid)['proposal'] is None
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=5)


def test_generation_capacity_is_bounded_and_released(chat):
    first = chat._active_conv_id
    second = chat.create_conversation('two')['id']
    third = chat.create_conversation('three')['id']
    entered, release = hold_model(chat)
    try:
        chat.brief_generation.start(first, 'request-one', 0)
        chat.brief_generation.start(second, 'request-two', 0)
        with pytest.raises(RevisionConflict, match='上限'):
            chat.brief_generation.start(third, 'request-three', 0)
        assert chat.brief_generation.get(third)['job'] is None
    finally:
        release.set()
    finish(chat, first)
    finish(chat, second, 'request-two')
    chat.brief_generation.start(third, 'request-three', 0)
    assert finish(chat, third, 'request-three')['status'] == 'review'


def test_generation_uses_submission_snapshot(chat):
    cid = chat._active_conv_id
    chat.briefs.save(cid, {**document(), 'title': '提交时版本'}, 0)
    chat._conversations[cid] = [{'role': 'user', 'content': '提交时对话'}]
    entered, release = hold_model(chat)
    try:
        chat.brief_generation.start(cid, 'request-one', 1)
        assert entered.wait(2)
        chat._conversations[cid].append({'role': 'user', 'content': '后续新对话'})
        chat.briefs.save(cid, {**document(), 'title': '后续新稿'}, 1)
    finally:
        release.set()
    assert finish(chat, cid)['status'] == 'conflict'
    prompt, options = chat.runtime._call_llm_with_routing.call_args.args
    assert '提交时对话' in str(prompt) and '后续新对话' not in str(prompt)
    assert '提交时版本' in options['_system_prompt'] and '后续新稿' not in options['_system_prompt']


def test_thread_start_failure_is_terminal_without_model_call(chat, monkeypatch):
    with monkeypatch.context() as context:
        context.setattr('lan_mesh.brief_generation.threading.Thread.start', Mock(side_effect=RuntimeError('no threads')))
        with pytest.raises(RuntimeError, match='no threads'):
            chat.brief_generation.start(chat._active_conv_id, 'request-one', 0)
    assert chat.brief_generation.get(chat._active_conv_id)['job']['status'] == 'failed'
    chat.runtime._call_llm_with_routing.assert_not_called()


def test_unwritable_terminal_status_is_reconciled_after_io_recovers(chat, monkeypatch):
    import lan_mesh.brief_generation as generation
    cid = chat._active_conv_id
    entered, release = hold_model(chat)
    try:
        chat.brief_generation.start(cid, 'request-one', 0)
        assert entered.wait(2)
        with monkeypatch.context() as context:
            context.setattr(chat.briefs, '_write', Mock(side_effect=OSError('disk full')))
            release.set()
            deadline = time.monotonic() + 5
            key = (str(chat.briefs.root.resolve()), cid, 'request-one')
            while key in generation._LIVE and time.monotonic() < deadline:
                time.sleep(.01)
            assert key not in generation._LIVE
    finally:
        release.set()
    assert chat.brief_generation.get(cid)['job']['status'] == 'interrupted'
    chat.brief_generation.start(cid, 'request-one', 0)
    assert chat.runtime._call_llm_with_routing.call_count == 1
    assert chat.briefs.get(cid)['proposal'] is None


def test_offline_backup_restore_keeps_generation_identity(chat, tmp_path_factory):
    from lan_mesh.backup import create_backup, restore_backup
    cid = chat._active_conv_id
    chat.brief_generation.start(cid, 'request-one', 0)
    original = finish(chat, cid)
    backup_dir = tmp_path_factory.mktemp('generation-backup')
    archive = backup_dir / 'generation.zip'
    create_backup({'shared': Path(chat._shared_folder.path)}, archive, offline_confirmed=True)
    destination = backup_dir / 'restored'
    restore_backup(archive, destination)
    restored = ChatHandler(Mock(), chat.controller, shared_folder=SimpleNamespace(path=destination / 'shared'))
    assert restored.brief_generation.get(cid)['job'] == original
    assert restored.brief_generation.start(cid, 'request-one', 0)['job'] == original
    restored.runtime._call_llm_with_routing.assert_not_called()
    assert restored.briefs.get(cid)['proposal']['id'] == original['proposal_id']
