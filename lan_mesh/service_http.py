"""FastAPI adapter preserving endpoint signatures over Yomk service requests."""

from functools import wraps
from typing import Any, Callable

from fastapi import APIRouter, HTTPException

from .service_runtime import ServiceError, ServiceRuntime


def add_service_route(router: APIRouter, runtime: ServiceRuntime, path: str,
                      method: str, service: str, handler: Callable) -> None:
    """Keep FastAPI body/query/path validation while dispatching via the bus."""
    operation = f"{service}/{handler.__name__}"

    @wraps(handler)
    async def endpoint(**arguments: Any) -> Any:
        try:
            return await runtime.request(operation, **arguments)
        except ServiceError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    router.add_api_route(path, endpoint, methods=[method], response_model=None)
