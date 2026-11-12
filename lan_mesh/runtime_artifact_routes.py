"""Scoped read-only artifact API on the existing authenticated Station/Worker surface."""

from fastapi import APIRouter

from .component_service import ComponentService
from .runtime_artifacts import RuntimeArtifacts, artifact_root
from .service_http import add_service_route
from .service_runtime import ServiceError, get_service_runtime


class RuntimeArtifactService(ComponentService):
    """Use server configuration for storage and require an exact execution scope."""

    OPERATIONS = ('inspect_run', 'read_artifact')

    def _open(self, execution_id: str, scope: dict) -> RuntimeArtifacts:
        state = getattr(self.owner, 'state', self.owner)
        folder = getattr(state, 'shared_folder', None)
        if folder is None:
            raise ServiceError(503, '产物目录尚未初始化')
        return RuntimeArtifacts(artifact_root(str(getattr(folder, 'path', folder))), execution_id, scope)

    def _invoke(self, execution_id: str, scope: dict, artifact_id: str = '', **options: object) -> dict:
        try:
            store = self._open(execution_id, scope)
            return store.read(artifact_id, **options) if artifact_id else store.inspect()
        except (PermissionError, FileNotFoundError) as exc:
            raise ServiceError(404, '未找到此执行范围的产物') from exc
        except (ValueError, TypeError, KeyError) as exc:
            raise ServiceError(400, '产物标识、内容或分页参数无效') from exc
        except OSError as exc:
            raise ServiceError(503, '产物暂不可读') from exc

    def inspect_run(self, execution_id: str, agent_id: str, task_id: str,
                    project_id: str, attempt_id: str) -> dict:
        scope = dict(agent_id=agent_id, task_id=task_id, project_id=project_id, attempt_id=attempt_id)
        return self._invoke(execution_id, scope)

    def read_artifact(self, execution_id: str, artifact_id: str, agent_id: str, task_id: str,
                      project_id: str, attempt_id: str, offset: int = 0, limit: int = 4000,
                      view: str = 'observation') -> dict:
        scope = dict(agent_id=agent_id, task_id=task_id, project_id=project_id, attempt_id=attempt_id)
        return self._invoke(execution_id, scope, artifact_id, offset=offset, limit=limit, view=view)


def build_runtime_artifact_routes(owner: object) -> APIRouter:
    """Register reads through the owner's Yomk service runtime and shutdown boundary."""
    router, runtime = APIRouter(), get_service_runtime(owner)
    name = '/RuntimeArtifactService'
    service = runtime.ensure(name, lambda server: RuntimeArtifactService(server, owner),
                             list(RuntimeArtifactService.OPERATIONS))
    base = '/api/runtime/artifacts/{execution_id}'
    add_service_route(router, runtime, base, 'GET', name, service.inspect_run)
    add_service_route(router, runtime, base + '/{artifact_id}', 'GET', name, service.read_artifact)
    return router
