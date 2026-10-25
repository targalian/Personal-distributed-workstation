"""Worker concurrency, responsive execution API and native execution boundaries."""

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import httpx

from fastapi import FastAPI
from lan_mesh.agent_runtime import AgentRuntime
from lan_mesh.worker import WorkerAgent
from lan_mesh.worker_routes_basic import build_basic_routes
from lan_mesh.shared_folder import SharedFolderManager


def test_worker_pm_constructor_reserved_across_concurrent_calls(monkeypatch):
    import lan_mesh.worker as policy
    worker = WorkerAgent.__new__(WorkerAgent)
    worker.state = SimpleNamespace(pm_agent=None, agent_runtime=object(), device_id='w', device_name='worker', sub_agents={})
    entered, release = threading.Event(), threading.Event()
    constructed = []

    class Pm:
        def __init__(self, **kwargs):
            constructed.append(self)
            self.pm_id=kwargs['pm_id'];self._running=False
            entered.set()
            assert release.wait(3)

        def start_task(self, task):
            self._running=True

        def cancel(self):
            self._running=False

    monkeypatch.setattr(policy, 'ProjectManagerAgent', Pm)
    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            first=pool.submit(worker.start_pm, 'task', 'http://secretary', {'name':'task'})
            assert entered.wait(2)
            others=[pool.submit(worker.start_pm, 'other', 'http://secretary', {'name':'task'}) for _ in range(7)]
            assert all(not item.result(timeout=2)['ok'] for item in others)
            release.set()
            assert first.result(timeout=2)['ok']
        assert len(constructed)==1
        assert worker.stop_pm()['ok']
    finally:
        release.set()
        worker.services.close()


def test_worker_execution_does_not_block_status_loop(tmp_path):
    runtime=AgentRuntime('worker',str(tmp_path))
    entered,release=threading.Event(),threading.Event()

    def slow(data):
        entered.set()
        assert release.wait(3)
        return {'result':'done'}

    runtime._handlers['slow']=slow
    runtime.list_cli_agents=lambda: {'agents':[]}
    app=FastAPI()
    app.include_router(build_basic_routes(lambda: None,SharedFolderManager(str(tmp_path)),runtime))

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://worker') as client:
            task=asyncio.create_task(client.post('/tasks/execute',json={'required_skill':'slow'}))
            try:
                assert await asyncio.to_thread(entered.wait,2)
                response=await asyncio.wait_for(client.get('/agents/cli-status'),1)
                assert response.status_code==200
            finally:
                release.set()
            assert (await task).json()['status']=='completed'

    try:
        asyncio.run(exercise())
        assert '/RuntimeExecutionService' in {item['service'] for item in runtime.services.describe()}
    finally:
        release.set()
        runtime.services.close()
