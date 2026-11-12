"""Private memory replication, interruption recovery and scope-preserving failover."""

import base64
import hashlib
import json
import socket
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import Iterator

import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lan_mesh.chat_handler import ChatHandler
from lan_mesh.component_service import call_component
from lan_mesh.database import Database
from lan_mesh.memory_backup import export_memory
from lan_mesh.memory_restore import restore_memory
from lan_mesh.private_backup_routes import build_private_backup_routes
from lan_mesh.private_backup_service import PrivateBackupService
from lan_mesh.private_bundle import CHUNK_BYTES, PrivateBundleStore, file_digest
from lan_mesh.protocol import HostRecord
from lan_mesh.shared_folder import SharedFolderManager
from tests.test_scoped_memory import chat, _call, _message, _create


@pytest.fixture
def standby(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[ChatHandler]:
    folder = tmp_path / 'standby'; folder.mkdir()
    db = Database(str(folder / 'station.db'))
    shared = SharedFolderManager(str(folder))
    ctl = SimpleNamespace(db=db, secretary_active=True, state=SimpleNamespace(shared_folder=shared, device_id='backup'))
    handler = ChatHandler(SimpleNamespace(), ctl, db, shared);ctl.chat_handler=handler
    handler._conv_index=[];handler._conversations={};handler._save_conv_index()
    yield handler
    for owner in (handler, ctl):
        if vars(owner).get('services'):owner.services.close()


def _store(chat: ChatHandler) -> PrivateBundleStore:
    return PrivateBundleStore(Path(chat._shared_folder.path))


def _transfer(source: PrivateBundleStore, dest: PrivateBundleStore, bundle_id: str) -> None:
    path = source.path(bundle_id);total=path.stat().st_size
    with path.open('rb') as handle:
        offset=0
        while block:=handle.read(CHUNK_BYTES):
            reply=dest.receive(bundle_id,dict(offset=offset,total=total,data=base64.b64encode(block).decode(),sha256=hashlib.sha256(block).hexdigest()))
            offset+=len(block)
            assert reply['offset']==offset
    assert reply['complete']


def test_project_failover_preserves_raw_sources_and_revisions(chat: ChatHandler, standby: ChatHandler) -> None:
    anchor=_message(chat,'最初用英文');memory=_create(chat,anchor,scope='project',content='英文')
    replacement=_message(chat,'现在使用中文',cid='conv-a2')
    _call(chat,'revise_memory',conv_id='conv-a2',memory_id=memory['memory_id'],payload=dict(
        expected_revision=1,confirmed=True,content='中文',source_anchor=replacement))
    _create(chat,_message(chat,'仅PM偏好',agent='pm-a'),scope='agent',agent='pm-a')
    _message(chat,'另一项目不能复制',cid='conv-b')
    raw=Path(chat._get_conv_dir())/'conv-a.jsonl'
    with raw.open('ab') as handle:handle.write(b'broken\n\xff\n')
    source,target=_store(chat),_store(standby)
    exported=export_memory(chat,source,'conv-a');bid=exported['bundle_id']
    assert exported['manifest']['scope']['conv_ids']==['conv-a','conv-a2']
    _transfer(source,target,bid)
    result=restore_memory(standby,target,bid)
    assert result['state']=='completed'
    assert (Path(standby._get_conv_dir())/'conv-a.jsonl').read_bytes()==raw.read_bytes()
    assert {m['id'] for m in standby._conv_index}=={'conv-a','conv-a2'}
    items=_call(standby,'list_memories',conv_id='conv-a')['items']
    assert items[0]['content']=='中文' and items[0]['source_conv_id']=='conv-a2'
    assert _call(standby,'read_source',conv_id='conv-a',anchor=anchor)['content']=='最初用英文'
    assert len(_call(standby,'memory_revisions',conv_id='conv-a',memory_id=memory['memory_id'])['items'])==2
    standby._save_message_to_file('conv-a',dict(role='user',content='恢复后的新消息',timestamp=3))
    assert restore_memory(standby,target,bid)['already_restored']
    assert _call(standby,'search_history',conv_id='conv-a',query='新消息')['items']


def test_unprojected_snapshot_does_not_copy_other_empty_project_conversations(chat: ChatHandler, standby: ChatHandler) -> None:
    chat._conv_index.append(dict(id='conv-none2',project_id='',pm_threads=[]))
    _create(chat,_message(chat,'当前无项目来源',cid='conv-none'),cid='conv-none')
    _create(chat,_message(chat,'另一无项目来源',cid='conv-none2'),cid='conv-none2')
    source,target=_store(chat),_store(standby)
    item=export_memory(chat,source,'conv-none');_transfer(source,target,item['bundle_id'])
    restore_memory(standby,target,item['bundle_id'])
    assert [m['id'] for m in standby._conv_index]==['conv-none']
    assert standby._db._get_conn().execute('SELECT COUNT(*) FROM context_memories').fetchone()[0]==1


def test_restore_rejects_active_project_without_writing_journals(chat: ChatHandler, standby: ChatHandler) -> None:
    _create(chat,_message(chat,'受保护数据'))
    source,target=_store(chat),_store(standby)
    bid=export_memory(chat,source,'conv-a')['bundle_id'];_transfer(source,target,bid)
    standby._conv_index=[dict(id='live',project_id='project-a',pm_threads=[])]
    with pytest.raises(ValueError,match='拒绝覆盖'):restore_memory(standby,target,bid)
    assert standby._db._get_conn().execute('SELECT COUNT(*) FROM context_memories').fetchone()[0]==0
    assert not (Path(standby._get_conv_dir())/'conv-a.jsonl').exists()


def test_restore_resumes_after_journals_committed_before_files(chat: ChatHandler, standby: ChatHandler, monkeypatch: pytest.MonkeyPatch) -> None:
    import lan_mesh.memory_restore as module
    _create(chat,_message(chat,'崩溃恢复来源'))
    source,target=_store(chat),_store(standby)
    bid=export_memory(chat,source,'conv-a')['bundle_id'];_transfer(source,target,bid)
    original=module._install_sources
    def interrupt(*args: object) -> None:raise OSError('interrupted')
    monkeypatch.setattr(module,'_install_sources',interrupt)
    with pytest.raises(OSError):restore_memory(standby,target,bid)
    assert not standby._conv_index
    assert standby._db._get_conn().execute('SELECT state FROM private_restore_jobs').fetchone()[0]=='prepared'
    monkeypatch.setattr(module,'_install_sources',original)
    standby._db=Database(str(standby._db.path));standby.controller.db=standby._db
    assert restore_memory(standby,target,bid)['state']=='completed'
    assert len(_call(standby,'list_memories',conv_id='conv-a')['items'])==1


def test_retry_after_index_publish_does_not_overwrite_new_messages(chat: ChatHandler, standby: ChatHandler, monkeypatch: pytest.MonkeyPatch) -> None:
    import lan_mesh.memory_restore as module
    _message(chat,'原始内容')
    source,target=_store(chat),_store(standby)
    bid=export_memory(chat,source,'conv-a')['bundle_id'];_transfer(source,target,bid)
    original=module.synchronize_history
    def interrupt(*args: object) -> None:raise OSError('index failed')
    monkeypatch.setattr(module,'synchronize_history',interrupt)
    with pytest.raises(OSError):restore_memory(standby,target,bid)
    standby._save_message_to_file('conv-a',dict(role='user',content='发布后新增',timestamp=4))
    monkeypatch.setattr(module,'synchronize_history',original)
    restore_memory(standby,target,bid)
    assert _call(standby,'search_history',conv_id='conv-a',query='发布后新增')['items']


def test_chunk_retry_reopen_conflict_and_crash_before_rename(tmp_path: Path) -> None:
    source,target=PrivateBundleStore(tmp_path/'a'),PrivateBundleStore(tmp_path/'b')
    content=tmp_path/'input';content.write_bytes(b'x'*(CHUNK_BYTES+123))
    bid=source.create('test',{}, {'input.bin':content})['bundle_id'];raw=source.path(bid).read_bytes()
    payload=dict(offset=0,total=len(raw),data=base64.b64encode(raw[:CHUNK_BYTES]).decode(),sha256=hashlib.sha256(raw[:CHUNK_BYTES]).hexdigest())
    assert target.receive(bid,payload)['offset']==CHUNK_BYTES
    target=PrivateBundleStore(tmp_path/'b')
    assert target.receive(bid,payload)['offset']==CHUNK_BYTES
    wrong=b'z'*CHUNK_BYTES
    with pytest.raises(ValueError,match='冲突'):
        target.receive(bid,dict(payload,data=base64.b64encode(wrong).decode(),sha256=hashlib.sha256(wrong).hexdigest()))
    target.path(bid,'.part').write_bytes(raw)  # Simulate durable final bytes before atomic rename.
    assert target.status(bid)['complete'] and not target.path(bid,'.part').exists()
    assert target.validate(bid)['kind']=='test'


def test_backup_tampering_and_zip_traversal_are_rejected(chat: ChatHandler, standby: ChatHandler) -> None:
    source,target=_store(chat),_store(standby)
    bid=export_memory(chat,source,'conv-a')['bundle_id'];_transfer(source,target,bid)
    with target.path(bid).open('ab') as handle:handle.write(b'tampered')
    with pytest.raises(ValueError,match='校验失败'):restore_memory(standby,target,bid)
    bad=target.root/'bad.tmp'
    with zipfile.ZipFile(bad,'w') as archive:
        archive.writestr('../escape','bad');archive.writestr('manifest.json','{}')
    bad_id=file_digest(bad);bad.rename(target.path(bad_id))
    with pytest.raises(ValueError,match='文件名'):target.validate(bad_id)
    assert not (target.root.parent/'escape').exists()


def test_real_http_replication_to_standby_and_manual_promotion(chat: ChatHandler, standby: ChatHandler, monkeypatch: pytest.MonkeyPatch) -> None:
    chat.controller.state.shared_folder=chat._shared_folder;chat.controller.state.device_id='primary'
    _create(chat,_message(chat,'HTTP跨站恢复'))
    app=FastAPI();app.include_router(build_private_backup_routes(standby.controller))
    standby.controller.secretary_active=False
    sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    server=uvicorn.Server(uvicorn.Config(app,log_level='error'))
    thread=threading.Thread(target=lambda:server.run(sockets=[sock]),daemon=True);thread.start()
    deadline=time.monotonic()+5
    while not server.started and time.monotonic()<deadline:time.sleep(.01)
    assert server.started
    chat._db.upsert_host(HostRecord(device_id='backup',device_name='standby',ip='127.0.0.1',api_port=port))
    try:
        snapshot=call_component(chat.controller,PrivateBackupService,'export_memory',conv_id='conv-a')
        bid=snapshot['bundle_id']
        original=PrivateBackupService._request;posts=[]
        def lose_ack(method: str, url: str, payload: dict | None=None) -> dict:
            response=original(method,url,payload)
            if method=='POST':
                posts.append(url)
                if len(posts)==1:raise OSError('response lost after peer committed')
            return response
        monkeypatch.setattr(PrivateBackupService,'_request',staticmethod(lose_ack))
        from lan_mesh.service_runtime import ServiceError
        with pytest.raises(ServiceError):
            call_component(chat.controller,PrivateBackupService,'push',bundle_id=bid,payload={'peer_id':'backup'})
        for _ in range(2):
            assert call_component(chat.controller,PrivateBackupService,'push',bundle_id=bid,payload={'peer_id':'backup'})['state']=='replicated'
        assert len(posts)==1
        with TestClient(app) as client:
            assert client.post(f'/api/station/memory-backups/{bid}/restore',json={'confirmed':True}).status_code==503
            standby.controller.secretary_active=True
            assert client.post(f'/api/station/memory-backups/{bid}/restore',json={}).status_code==422
            assert client.post(f'/api/station/memory-backups/{bid}/restore',json={'confirmed':True}).status_code==200
        assert _call(standby,'list_memories',conv_id='conv-a')['items']
    finally:
        server.should_exit=True;thread.join(5)


def test_private_bundle_is_unavailable_through_shared_paths(chat: ChatHandler) -> None:
    store=_store(chat);bid=export_memory(chat,store,'conv-a')['bundle_id']
    with pytest.raises(ValueError):chat._shared_folder.resolve_path(f'.private_replicas/{bid}.zip')
    alias=Path(chat._shared_folder.path)/'backup-alias';alias.symlink_to(store.root,target_is_directory=True)
    with pytest.raises(ValueError):chat._shared_folder.resolve_path(f'backup-alias/{bid}.zip')


def test_deleted_agent_revision_history_survives_snapshot(chat: ChatHandler, standby: ChatHandler) -> None:
    anchor=_message(chat,'已解绑PM的历史偏好',agent='pm-a')
    memory=_create(chat,anchor,scope='agent',agent='pm-a')
    chat.detach_pm_thread('conv-a','pm-a')
    source,target=_store(chat),_store(standby)
    bid=export_memory(chat,source,'conv-a')['bundle_id'];_transfer(source,target,bid)
    restore_memory(standby,target,bid)
    row=standby._db._get_conn().execute('SELECT state,revision FROM context_memories WHERE memory_id=?',(memory['memory_id'],)).fetchone()
    assert tuple(row)==('revoked',2)


def test_source_change_during_snapshot_publishes_no_bundle(chat: ChatHandler, monkeypatch: pytest.MonkeyPatch) -> None:
    _message(chat,'并发写入来源')
    source=_store(chat);path=Path(chat._get_conv_dir())/'conv-a.jsonl'
    original=PrivateBundleStore._copy
    def changing(src: object, dst: object, digest: object=None) -> None:
        original(src,dst,digest)
        if getattr(src,'name','')==str(path):
            with path.open('ab') as handle:handle.write(b'{}\n')
    monkeypatch.setattr(PrivateBundleStore,'_copy',staticmethod(changing))
    with pytest.raises(OSError,match='发生变化'):export_memory(chat,source,'conv-a')
    assert not list(source.root.glob('*.zip'))


def test_backup_write_permissions_and_prepared_cleanup_guard(chat: ChatHandler, monkeypatch: pytest.MonkeyPatch) -> None:
    import lan_mesh.station_routes_common as common
    ctl=chat.controller;ctl.state.shared_folder=chat._shared_folder;ctl.state.device_id='a'
    monkeypatch.setattr(common,'_mesh_auth_enabled',True);monkeypatch.setattr(common,'_mesh_auth_token','mesh')
    monkeypatch.setattr(common,'_API_KEY','');monkeypatch.setattr(common,'_users',{'configured':{}})
    monkeypatch.setattr(common,'_user_tokens',{'configured':{}})
    monkeypatch.setattr(common,'_rate_limiter',SimpleNamespace(is_allowed=lambda *a,**k:True))
    monkeypatch.setattr(common,'resolve_role',lambda token:{'role':token} if token in {'boss','operator','viewer'} else None)
    app=FastAPI();app.middleware('http')(common.api_guard_middleware);app.include_router(build_private_backup_routes(ctl))
    with TestClient(app) as client:
        endpoint='/api/station/memory-backups/export?conv_id=conv-a'
        assert client.post(endpoint).status_code==401
        for role in ['viewer','operator']:
            assert client.post(endpoint,headers={'Authorization':'Bearer '+role}).status_code==403
        client.headers['Authorization']='Bearer boss'
        bid=client.post(endpoint).json()['bundle_id']
        with chat._db._get_conn() as conn:
            conn.execute('INSERT INTO private_restore_jobs VALUES (?,?,?,?,?,?)',(bid,'memory','{}','prepared',1,0))
        delete='/api/station/private-bundles/'+bid+'/delete'
        assert client.post(delete,json={'confirmed':True}).status_code==409
        with chat._db._get_conn() as conn:conn.execute("UPDATE private_restore_jobs SET state='completed'")
        assert client.post(delete,json={'confirmed':True}).json()['deleted']
        assert not _store(chat).path(bid).exists()


def test_cloud_cannot_upload_or_overwrite_private_backup(tmp_path: Path) -> None:
    from lan_mesh.cloud_sync import CloudTransferService
    store=PrivateBundleStore(tmp_path);document=tmp_path/'document';document.write_text('shareable')
    bid=store.create('test',{}, {'document':document})['bundle_id']
    uploads,downloads=[],[]
    relative=f'.private_replicas/{bid}.zip'
    (tmp_path/'alias').symlink_to(store.root,target_is_directory=True)
    owner=SimpleNamespace(local_path=tmp_path,bucket='b',_client=SimpleNamespace(
        upload_file=lambda *a:uploads.append(a),download_file=lambda *a:downloads.append(a)),
        _cloud_key=lambda x:x,_list_cloud_objects=lambda:{relative:{'key':relative,'mtime':99999999999}})
    try:
        assert call_component(owner,CloudTransferService,'_upload_all')[0]==1
        assert call_component(owner,CloudTransferService,'_download_all')[0]==0
        assert uploads[0][0]==str(document) and not downloads
    finally:owner.services.close()


def test_real_process_exit_after_prepare_recovers_without_duplicate_memories(chat: ChatHandler, standby: ChatHandler) -> None:
    _create(chat,_message(chat,'真实进程退出后保留来源'))
    source,target=_store(chat),_store(standby)
    bid=export_memory(chat,source,'conv-a')['bundle_id'];_transfer(source,target,bid)
    code=f'''
import os
from pathlib import Path
from types import SimpleNamespace
from lan_mesh.database import Database
from lan_mesh.chat_handler import ChatHandler
from lan_mesh.shared_folder import SharedFolderManager
from lan_mesh.private_bundle import PrivateBundleStore
import lan_mesh.memory_restore as module
Database.backup=lambda *a,**k:''
db=Database({str(standby._db.path)!r})
folder=SharedFolderManager({str(standby._shared_folder.path)!r})
ctl=SimpleNamespace(db=db)
chat=ChatHandler(SimpleNamespace(),ctl,db,folder)
module._install_sources=lambda *args:os._exit(77)
module.restore_memory(chat,PrivateBundleStore(Path(folder.path)),{bid!r})
'''
    result=subprocess.run([sys.executable,'-c',code],capture_output=True,text=True,timeout=20)
    assert result.returncode==77,result.stderr
    assert standby._db._get_conn().execute('SELECT state FROM private_restore_jobs').fetchone()[0]=='prepared'
    standby._load_conv_index()
    restore_memory(standby,target,bid)
    assert len(_call(standby,'list_memories',conv_id='conv-a')['items'])==1
    assert len(_call(standby,'search_history',conv_id='conv-a',query='真实进程退出')['items'])==1
