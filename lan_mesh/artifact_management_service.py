"""Yomk service for scoped artifact retention and private execution replicas."""

from pathlib import Path
from typing import Any

from .artifact_backup import export_artifacts, restore_artifacts
from .artifact_retention import execute_cleanup, list_runs, preview_cleanup, set_retention
from .component_service import ComponentService
from .private_bundle import PrivateBundleStore
from .runtime_artifacts import artifact_root
from .service_runtime import ServiceError


class ArtifactManagementService(ComponentService):
    OPERATIONS = ("inventory", "retention", "preview", "cleanup", "export", "restore")

    def _folder(self) -> Path:
        folder = getattr(getattr(self.owner, "state", None), "shared_folder", None)
        if folder is None:
            raise ServiceError(503, "产物工作目录未初始化")
        return Path(getattr(folder, "path", folder))

    def _invoke(self, operation: Any, *args: Any) -> dict:
        try:
            return operation(*args)
        except (ValueError, KeyError, TypeError) as exc:
            raise ServiceError(409, str(exc)) from exc
        except FileNotFoundError as exc:
            raise ServiceError(404, "产物或预览不存在") from exc
        except PermissionError as exc:
            raise ServiceError(404, "未找到此范围的产物") from exc
        except OSError as exc:
            raise ServiceError(503, "产物操作未完成，可核对后重试") from exc

    def _scope(self, payload: dict) -> dict:
        scope = payload.get("scope", {})
        if (
            not isinstance(scope, dict)
            or set(scope) != {"agent_id", "task_id", "project_id", "attempt_id"}
            or any(not isinstance(v, str) for v in scope.values())
        ):
            raise ServiceError(422, "需要完整的执行作用域")
        return scope

    def inventory(self, task_id: str, project_id: str) -> dict:
        return self._invoke(list_runs, self.owner, artifact_root(str(self._folder())), task_id, project_id)

    def retention(self, execution_id: str, payload: dict) -> dict:
        return self._invoke(
            set_retention, artifact_root(str(self._folder())), execution_id, self._scope(payload), payload
        )

    def preview(self, payload: dict) -> dict:
        if not isinstance(payload.get("task_id"), str) or not isinstance(payload.get("project_id"), str):
            raise ServiceError(422, "需要task_id和project_id")
        return self._invoke(
            preview_cleanup,
            self.owner,
            artifact_root(str(self._folder())),
            payload["task_id"],
            payload["project_id"],
            payload.get("older_than_days", 7),
        )

    def cleanup(self, payload: dict) -> dict:
        return self._invoke(
            execute_cleanup,
            self.owner,
            artifact_root(str(self._folder())),
            str(payload.get("preview_id", "")),
            payload.get("confirmed", False),
        )

    def export(self, execution_id: str, payload: dict) -> dict:
        folder = self._folder()
        return self._invoke(
            export_artifacts, artifact_root(str(folder)), PrivateBundleStore(folder), execution_id, self._scope(payload)
        )

    def restore(self, bundle_id: str, payload: dict) -> dict:
        if payload.get("confirmed") is not True:
            raise ServiceError(422, "恢复执行副本需要明确确认")
        folder = self._folder()
        return self._invoke(restore_artifacts, artifact_root(str(folder)), PrivateBundleStore(folder), bundle_id)
