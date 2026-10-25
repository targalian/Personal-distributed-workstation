"""Yomk component composition with explicit owners and compatibility method dispatch."""

from typing import Any
from contextlib import nullcontext

from ._vendor.yomkserverpy.YomkServerPy import YomkServer, YomkService
from .service_runtime import get_service_runtime


class ComponentService(YomkService):
    """A domain component sharing its owner's existing state and dependencies."""

    OPERATIONS: tuple[str, ...] = ()

    def __init__(self, server: YomkServer, owner: object):
        super().__init__(server)
        self.owner = owner
        self.set_name(f"/{type(self).__name__}")


def call_component(owner: object, component: type[ComponentService],
                   operation: str, /, **arguments: Any) -> Any:
    """Keep old methods stable while executing a single domain implementation."""
    runtime = get_service_runtime(owner)
    runtime.ensure(f"/{component.__name__}", lambda server: component(server, owner),
                   list(component.OPERATIONS))
    with vars(owner).get("_component_lock", nullcontext()):
        return runtime.request_sync(f"/{component.__name__}/{operation}", **arguments)
