"""Cooperative lifecycle and draining regressions with controlled blocking windows."""

import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from lan_mesh._vendor.yomkserverpy.YomkServerPy import YomkService
from lan_mesh.application_lifecycle import shutdown_application, stop_signal, wait_for_stop
from lan_mesh.event_bus import EventBus
from lan_mesh.mcp_gateway import MCPGateway
from lan_mesh.model_resources import ModelResourceManager
from lan_mesh.service_runtime import ServiceError, ServiceRuntime, get_service_runtime


def test_runtime_close_drains_inflight_and_allows_nested_completion():
    runtime=ServiceRuntime()
    entered,release=threading.Event(),threading.Event()
    disposed=[]

    class Service(YomkService):
        def deinit(self):
            disposed.append(True)
            return 0

    service=Service(runtime.server);service.set_name('/Work')
    def outer():
        entered.set()
        assert release.wait(3)
        return runtime.request_sync('/Work/inner')
    runtime.register(service,{'outer':outer,'inner':lambda: 'done'})
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            result=pool.submit(runtime.request_sync,'/Work/outer')
            assert entered.wait(2)
            runtime.close()
            assert not disposed
            with pytest.raises(ServiceError):runtime.request_sync('/Work/inner')
            with pytest.raises(ServiceError):get_service_runtime(SimpleNamespace(services=runtime))
            release.set()
            assert result.result(timeout=2)=='done'
        assert disposed==[True]
    finally:
        release.set();runtime.close()


def test_resource_reload_drains_old_reporter_before_reusing_token(tmp_path):
    manager=ModelResourceManager()
    manager._report_interval=3600
    entered=threading.Event()
    def run():
        entered.set()
        manager._report_loop()
    thread=threading.Thread(target=run)
    manager._reporter=thread
    thread.start();assert entered.wait(2)
    config=tmp_path/'resources.yaml';config.write_text('resources: []\nalert_check: false\n')
    try:
        assert manager.load(config) is False
        assert not thread.is_alive()
        assert manager._reporter is None
    finally:
        manager._stop_evt.set();thread.join(2);manager.services.close()


def test_mcp_health_checker_has_stop_and_idempotent_start():
    gateway=MCPGateway()
    gateway.start_health_check_loop(3600)
    first=gateway._health_thread
    gateway.start_health_check_loop(3600)
    assert gateway._health_thread is first
    gateway.stop()
    assert not first.is_alive()


def test_shutdown_wakes_owner_and_prevents_runtime_resurrection(monkeypatch):
    import lan_mesh.model_resources as resources
    monkeypatch.setattr(resources._mgr,'stop_reporter',lambda: True)
    owner=SimpleNamespace(state=SimpleNamespace(),_running=True,_threads=[])
    runtime=get_service_runtime(owner)
    result=shutdown_application(owner)
    assert result['stopped'] and runtime.closed
    assert stop_signal(owner).is_set() and wait_for_stop(owner,3600)
    with pytest.raises(ServiceError):get_service_runtime(owner)
    assert shutdown_application(owner) is result


def test_old_event_sink_cannot_detach_new_owner():
    bus=EventBus()
    old,new=lambda event: None,lambda event: None
    bus.attach(None,old);bus.attach(None,new)
    bus.detach(old)
    assert bus.has_sink
    bus.detach(new)
    assert not bus.has_sink
