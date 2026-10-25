"""Explicit background ownership, cooperative stop and hierarchical service shutdown."""

import threading
import time
from typing import Any

from .service_runtime import ServiceRuntime

_ownership_lock = threading.RLock()


def stop_signal(owner: object) -> threading.Event:
    """Return the owner's persistent stop token without relying on dynamic attributes."""
    with _ownership_lock:
        return vars(owner).setdefault('_stop_event', threading.Event())


def wait_for_stop(owner: object, timeout: float) -> bool:
    """Interrupt an owned background delay when shutdown begins."""
    return stop_signal(owner).wait(timeout)


def join_threads(threads: list, timeout: float = 2.0) -> list[str]:
    """Use one deadline and report unfinished work instead of claiming it stopped."""
    deadline = time.monotonic() + timeout
    pending = []
    for thread in threads:
        if thread is None or thread is threading.current_thread():
            continue
        if thread.is_alive():
            thread.join(max(0.0, deadline-time.monotonic()))
        if thread.is_alive():
            pending.append(thread.name)
    return pending


def _component_threads(component: object) -> list:
    values = vars(component)
    threads = list(values.get('_threads', []))
    threads.extend(values.get(name) for name in (
        '_thread', '_progress_thread', '_health_thread', '_tg_poll_thread', '_sync_thread'))
    return [thread for thread in threads if thread is not None]


def close_component_runtime(component: object, defer: bool = True) -> None:
    """Reject new calls while existing native requests drain to completion."""
    if component is None or not hasattr(component, '__dict__'):
        return
    threads = [thread for thread in _component_threads(component) if thread.is_alive()]
    if defer and threads:
        with _ownership_lock:
            if vars(component).get('_service_retire_thread'):
                return
            def retire() -> None:
                for thread in threads:
                    thread.join()
                close_component_runtime(component, defer=False)
            waiter = threading.Thread(target=retire, name='service-retirement', daemon=True)
            vars(component)['_service_retire_thread'] = waiter
            waiter.start()
        return
    vars(component)['_services_stopping'] = True
    runtime = vars(component).get('services')
    if isinstance(runtime, ServiceRuntime):
        runtime.close()


def close_secretary_components(owner: object) -> None:
    """Dispose Secretary-owned components before dropping their references."""
    gateway = vars(owner).get('mcp_gateway')
    stop_gateway = getattr(gateway, 'stop', None)
    if callable(stop_gateway):
        stop_gateway()
    for name in ('project_manager', 'model_router', 'mcp_gateway', 'chat_handler', 'chat_runtime'):
        close_component_runtime(vars(owner).get(name))


def shutdown_application(owner: object) -> dict:
    """Stop producers, cancel execution, join owned threads and close service scopes."""
    with _ownership_lock:
        previous = vars(owner).get('_shutdown_report')
        if previous is not None and (previous.get('stopped') or vars(owner).get('_shutdown_in_progress')):
            return previous
        report: dict[str, Any] = {'stopped': False, 'pending': [], 'errors': []}
        vars(owner)['_shutdown_report'] = report
        vars(owner)['_shutdown_in_progress'] = True
    vars(owner)['_running'] = False
    stop_signal(owner).set()
    server = vars(owner).get('_server')
    if server is not None:
        server.should_exit = True
    state = vars(owner).get('state')
    state_values = vars(state) if state is not None else {}
    sink = state_values.get('_event_sink')
    if sink is not None:
        from .event_bus import get_event_bus
        get_event_bus().detach(sink)
    actions = [(vars(owner).get(name), method) for name, method in (
        ('workstation_optimizer', 'stop_guardian'), ('shadow_dev_manager', 'stop_guardian'),
        ('bot_gateway', 'stop'), ('discovery', 'stop'), ('mcp_gateway', 'stop'))]
    actions.append((state_values.get('cloud_sync'), 'stop'))
    pms = [vars(owner).get('_local_pm_agent'), state_values.get('pm_agent')]
    actions.extend((pm, 'cancel') for pm in pms)
    runtimes = [state_values.get('agent_runtime'), vars(owner).get('chat_runtime')]
    for mapping in (vars(owner).get('_local_sub_agents', {}), state_values.get('sub_agents', {})):
        runtimes.extend(item.get('runtime') for item in list(mapping.values()) if isinstance(item, dict))
    actions.extend((runtime, 'cancel') for runtime in runtimes)
    for component, operation in actions:
        if component is None:
            continue
        try:
            getattr(component, operation)()
        except Exception as exc:
            report['errors'].append(f'{operation}: {exc}')
    _stop_resources(report)
    threads = list(vars(owner).get('_threads', []))
    threads.extend(vars(pm).get(name) for pm in pms if pm is not None
                   for name in ('_thread', '_progress_thread'))
    for component, _ in actions:
        if component is not None:
            threads.extend(_component_threads(component))
    report['pending'].extend(join_threads(threads, 10.0))
    components = runtimes + pms + [component for component, _ in actions]
    components.extend(vars(owner).get(name) for name in (
        'project_manager', 'model_router', 'chat_handler', 'skill_registry', 'station_director'))
    for component in components:
        close_component_runtime(component)
    close_component_runtime(owner, defer=False)
    _retire_resources(components + [owner], threads, report)
    report['stopped'] = not report['pending'] and not report['errors']
    vars(owner)['_shutdown_in_progress'] = False
    print(f"[Station] 服务关闭: stopped={report['stopped']}, pending={report['pending']}")
    return report


def _stop_resources(report: dict) -> None:
    try:
        from .model_resources import _mgr
        if not _mgr.stop_reporter():
            report['pending'].append('resource-background')
    except Exception as exc:
        report['errors'].append(f'resources: {exc}')


def _retire_resources(components: list, threads: list, report: dict) -> None:
    """Keep the default ledger alive until the last producer can no longer write usage."""
    from .model_resources import _mgr
    runtimes = [vars(component).get('services') for component in components if component is not None]
    runtimes = [runtime for runtime in runtimes if isinstance(runtime, ServiceRuntime)]
    pending = [thread for thread in threads if thread is not None and thread.is_alive()]
    busy = any(not runtime.wait_idle(0) for runtime in runtimes)
    def retire() -> None:
        for thread in pending:
            if thread is not threading.current_thread():
                thread.join()
        for runtime in runtimes:
            runtime.wait_idle()
        resource_runtime = vars(_mgr).get('services')
        if isinstance(resource_runtime, ServiceRuntime):
            resource_runtime.close()
    if pending or busy:
        if busy:
            report['pending'].append('service-requests')
        threading.Thread(target=retire, name='resource-service-retirement', daemon=True).start()
    else:
        retire()
