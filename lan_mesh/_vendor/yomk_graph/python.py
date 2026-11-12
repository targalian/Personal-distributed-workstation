"""YomkServerPy adapter; framework import is deliberately lazy."""
from contextlib import contextmanager
from contextvars import ContextVar
import threading

from .collector import GraphCollector, write_snapshot


class PythonObserver:
    """Read ServerInfo and optionally observe this server instance's request entry.

    Attach/detach on the application control thread, before producers start / after
    they drain. Never owns init/shutdown. Async workers enter as @caller because
    the framework does not propagate the submitting handler's context.
    """
    def __init__(self, api, *, collector=None, project="Yomk Project", capture=True, resources=True):
        if api.server() is None:
            raise RuntimeError("initialize YomkServerPy before attaching the observer")
        self.api = api
        self.collector = collector or GraphCollector(project)
        self.server = api.server()
        self._caller = ContextVar(f"yomk_graph_caller_{id(self)}", default="@caller")
        self._suppress = ContextVar(f"yomk_graph_suppress_{id(self)}", default=False)
        self._closed = False
        self.include_resources = resources
        self._owner = threading.get_ident()
        self._hook = None
        if capture:
            if getattr(self.server, "_yomk_graph_observer", None) is not None:
                raise RuntimeError("a graph observer is already attached to this server")
            self._original = self.server.request
            self._had_instance_request = "request" in self.server.__dict__
            self._previous_instance_request = self.server.__dict__.get("request")
            self._hook = self._request
            try:
                self.server.request = self._hook
                self.server._yomk_graph_observer = self
            except (AttributeError, TypeError) as error:
                if self.server.__dict__.get("request") is self._hook:
                    self._restore()
                raise RuntimeError("runtime cannot attach request observer; use capture=False and explicit spans") from error

    def _restore(self):
        if self._had_instance_request:
            self.server.request = self._previous_instance_request
        else:
            del self.server.request
        if getattr(self.server, "_yomk_graph_observer", None) is self:
            del self.server._yomk_graph_observer

    def _request(self, url, pkg=None):
        if self._suppress.get() or not isinstance(url, str) or url.startswith("/YomkServerInfo/"):
            return self._original(url, pkg)
        parts = url.split("/", 2)
        if len(parts) != 3 or not parts[1] or not parts[2]:
            return self._original(url, pkg)
        target, endpoint = "/" + parts[1], "/" + parts[2]
        ticket = self.collector.begin(self._caller.get(), target, endpoint)
        token = self._caller.set(target)
        failed = True
        try:
            response = self._original(url, pkg)
            failed = response.status != self.api.ResStatus.eOk
            return response
        finally:
            self._caller.reset(token)
            self.collector.finish(ticket, failed)

    @contextmanager
    def caller(self, service):
        """Attribute a user-owned worker/timer entry explicitly; context is thread-local."""
        token = self._caller.set(service)
        try:
            yield
        finally:
            self._caller.reset(token)

    def snapshot(self):
        if self._closed or self.api.server() is not self.server:
            raise RuntimeError("observer is detached or Yomk runtime has shut down")
        token = self._suppress.set(True)
        try:
            response = self.api.server_info_all()
            if response.status != self.api.ResStatus.eOk:
                raise RuntimeError("YomkServerInfo /all query failed")
            infos = [dict(name=item.service_name, functions=[
                dict(name=func.func_name, message=func.msg_name, description=func.description)
                for func in item.funcs]) for item in response.data]
            resources, resource_warnings = self._resources() if self.include_resources else ({}, [])
            return self.collector.snapshot(infos, version=self.api.version(), resources=resources, warnings=resource_warnings + [
                "连线表示请求方向；返回值沿相反方向，不代表采集了消息内容。",
                "@caller 表示外部或未知来源；异步提交者、EventLoop/Monitor回调需显式 caller/span 标注。",
                "只覆盖采集期间实际执行的请求；没有连线不表示没有依赖。",
                "框架状态码失败不等于业务失败；eOk 内的业务拒绝需项目显式统计。",
            ])
        finally:
            self._suppress.reset(token)

    def _resources(self):
        # Only whitelisted metadata. Never call Context GET or serialize values/callbacks.
        resources, warnings = {}, []
        queries = [
            ("/YomkContext", "context_info_all", lambda data: [
                dict(group=group.group_name, keys=[dict(name=key.key, message=key.type_name,
                     monitors=key.monitor_count, async_monitors=key.async_monitor_count,
                     checker_on=key.checker_on) for key in group.keys]) for group in data]),
            ("/YomkEventLoop", "event_loop_info_all", lambda data: [
                dict(name=loop.loop_name, running=loop.running, pending=loop.pending,
                     message=loop.msg_name, has_default_func=loop.has_default_func) for loop in data]),
            ("/YomkFunctionPool", "function_pool_info_all", lambda data: [
                dict(name=func.func_name, message=func.msg_name, description=func.description)
                for func in data]),
        ]
        for service, method, convert in queries:
            try:
                query = getattr(self.api, method)
                response = query()
                if response.status != self.api.ResStatus.eOk:
                    raise RuntimeError("metadata query rejected")
                resources[service] = convert(response.data)
            except (AttributeError, TypeError, RuntimeError):
                warnings.append(service + " 的资源元信息暂不可用；服务清单仍可查看。")
        return resources, warnings

    def export(self, path):
        write_snapshot(path, self.snapshot())

    def close(self):
        if threading.get_ident() != self._owner:
            raise RuntimeError("detach observer on the attaching control thread after producers drain")
        if self._closed:
            return
        if self._hook:
            if self.server.__dict__.get("request") is not self._hook:
                raise RuntimeError("request hook was replaced by another component; coordinate detach first")
            self._restore()
        self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
