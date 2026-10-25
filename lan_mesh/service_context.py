"""Explicit Station service dependencies and Secretary availability policy."""

from dataclasses import dataclass
from typing import Awaitable, Callable

from ._vendor.yomkserverpy.YomkServerPy import YomkServer, YomkService
from .service_runtime import ServiceError


@dataclass(frozen=True)
class StationServiceContext:
    """Migration boundary: resolve mutable controller components at call time."""

    controller: object
    broadcast: Callable[[object, str, dict], Awaitable[None]]


class StationService(YomkService):
    """Shared framework base; business services do not import FastAPI routes."""

    def __init__(self, server: YomkServer, context: StationServiceContext, name: str):
        super().__init__(server)
        self.context = context
        self.set_name(name)

    def require_secretary(self) -> None:
        """Guard internal calls as well as HTTP calls, using live role state."""
        if not self.context.controller.secretary_active:
            raise ServiceError(
                503, "Secretary 未激活,请在 Station 面板中点击「启动秘书」")
