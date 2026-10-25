"""Instance-scoped YomkServerPy runtime and typed service invocation boundary."""

import inspect
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Callable

from starlette.concurrency import run_in_threadpool

from ._vendor.yomkserverpy.YomkServerPy import (
    ResStatus, YomkResponse, YomkServer, YomkService,
)


class ServiceError(Exception):
    """Expected business rejection; the HTTP adapter preserves its status/detail."""

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@dataclass(frozen=True)
class ServiceCall:
    """Named arguments for an explicitly registered service operation."""

    arguments: dict[str, Any] = field(default_factory=dict)


class ServiceRuntime:
    """Own one real Yomk server per application, without process-global state."""

    def __init__(self):
        self.server = YomkServer.create(async_thread_count=2)
        self.server.start_service(["/YomkContext", "/YomkServerInfo"])
        self._operations: dict[str, bool] = {}
        self._services: dict[str, YomkService] = {}
        self._lock = threading.RLock()
        self.closed = False
        self._active = 0
        self._shutdown_started = False
        self._depth = ContextVar(f"yomk_request_{id(self)}", default=0)
        self._idle = threading.Event()
        self._idle.set()

    def register(self, service: YomkService, operations: dict[str, Callable]) -> None:
        """Install an explicit operation table; reject accidental replacement."""
        with self._lock:
            if self.closed and not self._depth.get():
                raise RuntimeError("Service runtime is closed")
            name = service.get_name()
            if name in self.server.service_names():
                raise ValueError(f"Service already registered: {name}")
            for operation, handler in operations.items():
                if not operation.isidentifier() or not callable(handler):
                    raise ValueError(f"Invalid service operation: {operation}")
            if self.server.add_service(service) != 0:
                raise RuntimeError(f"Service initialization failed: {name}")
            for operation, handler in operations.items():
                service.install_func(
                    f"/{operation}", self._wrap(handler), "ServiceCall",
                    inspect.getdoc(handler) or operation,
                )
                self._operations[f"{name}/{operation}"] = inspect.iscoroutinefunction(handler)
            self._services[name] = service

    def ensure(self, name: str, factory: Callable, operations: list[str]) -> YomkService:
        """Atomically compose a service once, including calls before HTTP startup."""
        with self._lock:
            if self.closed and not self._depth.get():
                raise RuntimeError("Service runtime is closed")
            if name not in self._services:
                service = factory(self.server)
                self.register(service, {op: getattr(service, op) for op in operations})
            return self._services[name]

    def request_sync(self, url: str, /, **arguments: Any) -> Any:
        """Dispatch synchronous domain operations from threads or compatibility APIs."""
        with self._invocation(url):
            if self._operations[url]:
                raise TypeError(f"Use await runtime.request for async operation: {url}")
            result = self.server.request(url, ServiceCall(arguments))
            if result.status != ResStatus.eOk:
                raise ServiceError(503, result.msg)
            return result.data

    @contextmanager
    def _invocation(self, url: str):
        with self._lock:
            if self.closed and not self._depth.get():
                raise ServiceError(503, "Service runtime is closed")
            if url not in self._operations:
                raise ServiceError(404, f"Service operation not found: {url}")
            self._active += 1
            self._idle.clear()
        token = self._depth.set(self._depth.get() + 1)
        try:
            yield
        finally:
            self._depth.reset(token)
            with self._lock:
                self._active -= 1
                if not self._active:
                    self._idle.set()
                shutdown = self.closed and not self._active and not self._shutdown_started
                if shutdown:
                    self._shutdown_started = True
            if shutdown:
                self.server.shutdown()

    @staticmethod
    def _wrap(handler: Callable) -> Callable:
        def invoke(call: ServiceCall) -> YomkResponse:
            if not isinstance(call, ServiceCall):
                return YomkResponse(ResStatus.eInvalid, "Expected ServiceCall")
            return YomkResponse(ResStatus.eOk, "ok", handler(**call.arguments))
        return invoke

    async def request(self, url: str, /, **arguments: Any) -> Any:
        """Route through Yomk; keep async work on its loop and sync work off it."""
        with self._invocation(url):
            asynchronous = self._operations[url]
            call = ServiceCall(arguments)
            if asynchronous:
                result = self.server.request(url, call)
            else:
                result = await run_in_threadpool(self.server.request, url, call)
            if result.status != ResStatus.eOk:
                raise ServiceError(503, result.msg)
            if inspect.isawaitable(result.data):
                return await result.data
            return result.data

    def describe(self) -> list[dict]:
        """Return Yomk's service topology without exposing component instances."""
        with self._lock:
            if self.closed:
                return []
            response = self.server.request("/YomkServerInfo/all")
        return [
            {"service": item.service_name,
             "operations": [info.func_name for info in item.funcs]}
            for item in response.data
        ]

    def wait_idle(self, timeout: float | None = None) -> bool:
        """Wait for tracked requests to finish without polling or holding the registry lock."""
        return self._idle.wait(timeout)

    def close(self) -> None:
        """Release framework executors and services; safe to call repeatedly."""
        with self._lock:
            if self.closed:
                return
            self.closed = True
            shutdown = not self._active and not self._shutdown_started
            if shutdown:
                self._shutdown_started = True
        if shutdown:
            self.server.shutdown()


_creation_lock = threading.RLock()


def get_service_runtime(controller: object) -> ServiceRuntime:
    """Reuse this controller's runtime, rebuilding after a completed shutdown."""
    with _creation_lock:
        runtime = vars(controller).get("services")
        if isinstance(runtime, ServiceRuntime) and runtime._depth.get():
            return runtime
        if vars(controller).get("_services_stopping", False):
            raise ServiceError(503, "Application services are stopping")
        if isinstance(runtime, ServiceRuntime) and runtime.closed and runtime._active:
            raise ServiceError(503, "Service runtime is draining")
        if not isinstance(runtime, ServiceRuntime) or runtime.closed:
            runtime = ServiceRuntime()
            controller.services = runtime
        return runtime
