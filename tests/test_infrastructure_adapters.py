"""Compatibility and event-loop isolation for remaining infrastructure HTTP adapters."""

import asyncio
import json
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
from fastapi import FastAPI

from lan_mesh.station_routes_basic import build_basic_routes
from lan_mesh.station_routes_pm import build_pm_routes
from lan_mesh.station_routes_worker import build_worker_routes


def controller() -> SimpleNamespace:
    class Controller(SimpleNamespace):
        def __getattr__(self,name):return Mock()
    return Controller(db=Mock(),state=SimpleNamespace(ws_clients=set(),shared_folder=Mock()),
                      discovery=Mock(),station_director=Mock())


def test_infrastructure_api_contract_snapshot():
    app=FastAPI();ctl=controller()
    for builder in (build_basic_routes,build_pm_routes,build_worker_routes):app.include_router(builder(ctl))
    fixture=json.loads((Path(__file__).parent/'fixtures/infrastructure_api_contract.json').read_text(encoding='utf-8'))
    assert app.openapi()==fixture


def test_healing_io_does_not_block_status_queries():
    ctl=controller();entered,release=threading.Event(),threading.Event()
    def heal(action,category):
        entered.set();assert release.wait(3)
        return {'ok':True}
    ctl.run_heal_action=heal;ctl.get_auto_heal_status=lambda: {'running':True}
    app=FastAPI();app.include_router(build_basic_routes(ctl))
    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://station') as client:
            task=asyncio.create_task(client.post('/api/errors/heal',params={'action':'check'}))
            try:
                assert await asyncio.to_thread(entered.wait,2)
                response=await asyncio.wait_for(client.get('/api/errors/heal/status'),1)
                assert response.json()=={'running':True}
            finally:release.set()
            assert (await task).json()=={'ok':True}
    try:asyncio.run(exercise())
    finally:release.set()


def test_network_query_uses_discovery_replaced_after_route_assembly():
    from fastapi.testclient import TestClient
    ctl=controller()
    def discovery(port):
        return SimpleNamespace(network_status=lambda:SimpleNamespace(
            udp_port=port,api_port=8000,local_ips=[],broadcast_targets=[]))
    ctl.discovery=discovery(1111)
    app=FastAPI();app.include_router(build_basic_routes(ctl))
    ctl.discovery=discovery(2222)
    with TestClient(app) as client:
        assert client.get('/api/network').json()['udp_port']==2222
