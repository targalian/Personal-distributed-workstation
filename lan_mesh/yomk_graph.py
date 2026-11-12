"""Optional instance-scoped YomkGraph metadata and asynchronous request observation."""

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

from ._vendor.yomk_graph import GraphCollector, GraphServer
from .version import __version__


class RuntimeGraph:
    """Observe the owning runtime without patching Yomk or retaining request bodies."""

    def __init__(self):
        self.collector = GraphCollector("LAN Mesh — 主服务实例")
        self._caller = ContextVar(f"graph_caller_{id(self)}", default="@caller")
        self._infos = []
        groups = {
            "任务与 PM": ["TaskService", "TaskSubmissionService", "TaskSchedulingService",
                          "TaskMemoryService", "PmLifecycleService", "PmControlService",
                          "AgentService", "DeliveryService", "WorkerPmService", "WorkerSubagentService"],
            "对话与项目": ["ConversationService", "ConversationApiService", "ProjectApiService"],
            "资源与能力": ["ResourceApiService", "SecretExchangeService", "CapabilityApiService"],
            "工作站与同步": ["StationHostService", "StationSyncService", "StationHealingService",
                              "OptimizationQueueService", "ShadowQueueService", "WorkerSecretaryService"],
        }
        for module, names in groups.items():
            self.collector.module(module, ['/' + name for name in names])

    def refresh(self, server: object) -> None:
        """Cache registered metadata during serialized service composition."""
        response = server.request("/YomkServerInfo/all")
        self._infos = [dict(name=item.service_name, functions=[
            dict(name=f.func_name, message=f.msg_name, description=f.description)
            for f in item.funcs]) for item in response.data]

    @contextmanager
    def invocation(self, url: str) -> Iterator[None]:
        """Keep the caller and span active until an awaited operation finishes."""
        target, endpoint = url.rsplit('/', 1)
        source = self._caller.get()
        token = self._caller.set(target)
        try:
            with self.collector.span(source, target, '/' + endpoint, kind='service-runtime'):
                yield
        finally:
            self._caller.reset(token)

    def snapshot(self) -> dict:
        """Read bounded cached metadata; never call application handlers from HTTP."""
        return self.collector.snapshot(self._infos, version=__version__, warnings=[
            "仅当前 Station/Worker 主 ServiceRuntime；独立组件实例与远端节点未合并。",
            "实线仅覆盖 ServiceRuntime 请求；直接对象调用和原生 server.request 未采集。",
            "耗时包含协程执行与等待；异常计入失败，正常返回中的业务拒绝不自动计入。",
            "自建线程入口显示 @caller；不采集请求载荷、响应正文、Context 值或凭据。",
        ])


def install_graph_viewer(app: object, owner: object) -> None:
    """Tie the local viewer to the app; disabled configuration opens no socket."""
    from .service_runtime import get_service_runtime
    runtime = get_service_runtime(owner)
    if runtime.graph is None:
        return
    app.state.yomk_graph = None

    async def startup() -> None:
        try:
            viewer = GraphServer(runtime.graph.snapshot, port=owner.cfg.observability.yomk_graph_port)
            app.state.yomk_graph = viewer.start()
            vars(owner)['_yomk_graph_viewer'] = viewer
            print(f"[Station] YomkGraph: {viewer.url}")
        except BaseException:
            from starlette.concurrency import run_in_threadpool
            await run_in_threadpool(owner.stop)
            raise

    app.add_event_handler('startup', startup)


def close_graph_viewer(owner: object) -> None:
    """Drain viewer HTTP before application producers and services stop."""
    viewer = vars(owner).pop('_yomk_graph_viewer', None)
    if viewer is not None:
        viewer.close()
