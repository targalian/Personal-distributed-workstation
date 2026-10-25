"""Real loopback HTTP integration of Worker API, native execution and shutdown."""

import socket
import threading
import time
from types import SimpleNamespace

import requests
import uvicorn

from lan_mesh.agent_runtime import AgentRuntime
from lan_mesh.shared_folder import SharedFolderManager
from lan_mesh.worker import WorkerAgent


def test_worker_http_executes_native_domains_and_closes_owned_scopes(tmp_path,monkeypatch):
    import lan_mesh.station_routes_common as common
    monkeypatch.setattr(common,'_mesh_auth_enabled',False)
    monkeypatch.setattr(common,'_API_KEY','')
    target=tmp_path/'input.txt';target.write_text('network-native-result',encoding='utf-8')
    runtime=AgentRuntime('network-worker',str(tmp_path))
    worker=WorkerAgent.__new__(WorkerAgent)
    worker.state=SimpleNamespace(device_id='network-worker',device_name='Integration',mesh_token='',
        shared_folder=SharedFolderManager(str(tmp_path)),agent_runtime=runtime,pm_agent=None,sub_agents={},cloud_sync=None)
    worker._threads=[];worker._running=True;worker.discovery=None
    app=worker._create_app()
    sock=socket.socket();sock.bind(('127.0.0.1',0));sock.listen(128)
    server=uvicorn.Server(uvicorn.Config(app,log_level='error'))
    worker._server=server
    thread=threading.Thread(target=lambda:server.run(sockets=[sock]),daemon=True)
    thread.start();session=requests.Session();session.trust_env=False
    try:
        deadline=time.monotonic()+5
        while not server.started:
            assert time.monotonic()<deadline,'local Worker did not start'
            time.sleep(.01)
        base=f'http://127.0.0.1:{sock.getsockname()[1]}'
        response=session.post(base+'/tasks/execute',json={
            'required_skill':'file_ops','parent_task_id':'network-task','project_id':'network-project',
            'input_data':{'action':'read','path':str(target)}},timeout=10)
        assert response.status_code==200
        result=response.json()
        assert result['status']=='completed'
        assert result['output']['content']=='network-native-result'
        assert session.get(base+'/role/pm-status',timeout=10).json()=={'running':False}
        runtime.cancel()
        assert session.post(base+'/tasks/execute',json={'required_skill':'file_ops'},timeout=10).json()['status']=='cancelled'
    finally:
        session.close();server.should_exit=True;thread.join(10);sock.close()
    assert not thread.is_alive()
    assert worker._shutdown_report['stopped']
    assert runtime.services.closed and worker.services.closed
