"""Real runtime, coroutine attribution and local viewer lifecycle regression."""

import asyncio
import json
import socket
from types import SimpleNamespace
from urllib.request import urlopen

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lan_mesh._vendor.yomk_graph import GraphServer
from lan_mesh._vendor.yomkserverpy.YomkServerPy import YomkService
from lan_mesh.service_runtime import ServiceRuntime
from lan_mesh.yomk_graph import close_graph_viewer, install_graph_viewer


def register(runtime, name, **operations):
    service = YomkService(runtime.server)
    service.set_name(name)
    runtime.register(service, operations)


def test_async_nested_failure_and_private_payload():
    runtime = ServiceRuntime(graph_enabled=True)
    async def child(secret):
        await asyncio.sleep(0.01)
        raise ValueError(secret)
    async def parent(secret):
        return await runtime.request('/Child/run', secret=secret)
    register(runtime, '/Child', run=child)
    register(runtime, '/Parent', run=parent)
    try:
        with pytest.raises(ValueError):
            asyncio.run(runtime.request('/Parent/run', secret='never-in-snapshot'))
        snapshot = runtime.graph.snapshot()
        assert 'never-in-snapshot' not in json.dumps(snapshot)
        edges = snapshot['edges']
        assert len(edges) == 2
        assert any(e['source'].endswith('::/Parent') and e['target'].endswith('::/Child') for e in edges)
        assert all(e['calls'] == e['errors'] == 1 and e['in_flight'] == 0 for e in edges)
        assert all(e['last_ms'] >= 8 for e in edges)
        assert runtime.graph.snapshot()['edges'] == edges
    finally:
        runtime.close()


def test_sync_thread_context_and_instance_isolation():
    runtime = ServiceRuntime(graph_enabled=True)
    other = ServiceRuntime(graph_enabled=True)
    register(runtime, '/Child', run=lambda: 7)
    register(runtime, '/Parent', run=lambda: runtime.request_sync('/Child/run'))
    try:
        assert asyncio.run(runtime.request('/Parent/run')) == 7
        assert len(runtime.graph.snapshot()['edges']) == 2
        assert other.graph.snapshot()['edges'] == []
    finally:
        runtime.close()
        other.close()


def test_viewer_assets_and_closed_port():
    runtime = ServiceRuntime(graph_enabled=True)
    register(runtime, '/Unused', run=lambda: None)
    viewer = GraphServer(runtime.graph.snapshot, port=0).start()
    port = viewer.httpd.server_port
    try:
        for path in ('/', '/app.js', '/app.css', '/api/snapshot'):
            with urlopen(viewer.url + path, timeout=3) as response:
                assert response.status == 200 and response.read()
        with urlopen(viewer.url + '/api/snapshot') as response:
            snapshot = json.load(response)
        assert any(s['name'] == '/Unused' and s['functions'] for s in snapshot['services'])
        assert snapshot['edges'] == []
    finally:
        viewer.close()
        viewer.close()
        runtime.close()
    with socket.socket() as sock:
        assert sock.connect_ex(('127.0.0.1', port)) != 0


@pytest.mark.parametrize('enabled', [False, True])
def test_app_lifecycle(enabled):
    runtime = ServiceRuntime(graph_enabled=enabled)
    owner = SimpleNamespace(services=runtime, cfg=SimpleNamespace(
        observability=SimpleNamespace(yomk_graph_port=0)))
    app = FastAPI()
    install_graph_viewer(app, owner)
    app.add_event_handler('shutdown', lambda: close_graph_viewer(owner))
    try:
        with TestClient(app):
            assert ('_yomk_graph_viewer' in vars(owner)) is enabled
        assert '_yomk_graph_viewer' not in vars(owner)
    finally:
        runtime.close()


def test_port_conflict_cleans_owner():
    runtime = ServiceRuntime(graph_enabled=True)
    with socket.socket() as occupied:
        occupied.bind(('127.0.0.1', 0))
        occupied.listen()
        owner = SimpleNamespace(services=runtime, cfg=SimpleNamespace(
            observability=SimpleNamespace(yomk_graph_port=occupied.getsockname()[1])), stop=runtime.close)
        app = FastAPI()
        install_graph_viewer(app, owner)
        with pytest.raises(OSError), TestClient(app):
            pass
        assert runtime.closed


def test_cancellation_and_in_flight_during_shutdown():
    runtime = ServiceRuntime(graph_enabled=True)
    async def scenario():
        entered = asyncio.Event()
        async def work():
            entered.set()
            await asyncio.Event().wait()
        register(runtime, '/Work', run=work)
        task = asyncio.create_task(runtime.request('/Work/run'))
        await entered.wait()
        assert runtime.graph.snapshot()['edges'][0]['in_flight'] == 1
        runtime.close()
        assert not runtime.wait_idle(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert runtime.wait_idle(0)
        edge = runtime.graph.snapshot()['edges'][0]
        assert edge['in_flight'] == 0 and edge['errors'] == edge['calls'] == 1
    try:
        asyncio.run(scenario())
    finally:
        runtime.close()


def test_configuration_selects_runtime_and_modules():
    from lan_mesh.config import AppConfig
    from lan_mesh.service_runtime import get_service_runtime
    cfg = AppConfig()
    assert cfg.observability.yomk_graph_enabled is False
    owner = SimpleNamespace(cfg=cfg)
    runtime = get_service_runtime(owner)
    assert runtime.graph is None
    runtime.close()
    cfg.observability.yomk_graph_enabled = True
    runtime = get_service_runtime(owner)
    try:
        register(runtime, '/TaskService', unused=lambda: None)
        service = next(s for s in runtime.graph.snapshot()['services'] if s['name'] == '/TaskService')
        assert service['module'] == '任务与 PM'
        assert service['registered'] and service['functions'][0]['name'] == '/unused'
    finally:
        runtime.close()
