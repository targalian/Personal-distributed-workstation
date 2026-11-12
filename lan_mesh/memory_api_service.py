"""Authenticated Station boundary for explicit memory edits and scoped history reads."""

from typing import Any

from ._vendor.yomkserverpy.YomkServerPy import YomkServer
from .component_service import call_component
from .memory_service import MemoryService
from .service_context import StationService, StationServiceContext
from .service_runtime import ServiceError


class MemoryApiService(StationService):
    """Expose no global search or caller-selected project override."""

    def __init__(self, server: YomkServer, context: StationServiceContext):
        super().__init__(server, context, '/MemoryApiService')

    def _invoke(self, operation: str, **arguments: Any) -> dict:
        self.require_secretary()
        handler = getattr(self.context.controller, 'chat_handler', None)
        if handler is None:
            raise ServiceError(503, '聊天处理器未初始化')
        try:
            return call_component(handler, MemoryService, operation, **arguments)
        except KeyError as exc:
            raise ServiceError(404, str(exc)) from exc
        except ValueError as exc:
            status = 409 if str(exc) == 'revision_conflict' else 400
            raise ServiceError(status, str(exc)) from exc
        except OSError as exc:
            raise ServiceError(503, '历史文件暂不可读，请稍后重试') from exc

    def search_history(self, conv_id: str, query: str, agent_id: str = '',
                       limit: int = 20, before_seq: int = 0) -> dict:
        return self._invoke('search_history', conv_id=conv_id, query=query, agent_id=agent_id,
                            limit=limit, before_seq=before_seq)

    def read_source(self, conv_id: str, anchor: str, agent_id: str = '',
                    offset: int = 0, limit: int = 4000) -> dict:
        return self._invoke('read_source', conv_id=conv_id, anchor=anchor, agent_id=agent_id,
                            offset=offset, limit=limit)

    def rebuild_index(self) -> dict:
        return self._invoke('rebuild_index')

    def list_memories(self, conv_id: str, agent_id: str = '', include_inactive: bool = False,
                      limit: int = 50, offset: int = 0) -> dict:
        return self._invoke('list_memories', conv_id=conv_id, agent_id=agent_id,
                            include_inactive=include_inactive, limit=limit, offset=offset)

    def create_memory(self, conv_id: str, payload: dict, agent_id: str = '') -> dict:
        return self._invoke('create_memory', conv_id=conv_id, payload=payload, agent_id=agent_id)

    def revise_memory(self, conv_id: str, memory_id: str, payload: dict, agent_id: str = '') -> dict:
        return self._invoke('revise_memory', conv_id=conv_id, memory_id=memory_id,
                            payload=payload, agent_id=agent_id)

    def memory_revisions(self, conv_id: str, memory_id: str, agent_id: str = '') -> dict:
        return self._invoke('memory_revisions', conv_id=conv_id, memory_id=memory_id, agent_id=agent_id)

    def execution_context(self, task_id: str, pm_id: str) -> dict:
        """Resolve the current persisted assignment, never a payload-selected conversation."""
        self.require_secretary()
        ctl = self.context.controller
        task = ctl.db.get_task(task_id)
        if not task or not pm_id or task.pm_agent_id != pm_id:
            raise ServiceError(404, '任务与PM绑定不匹配')
        chat = getattr(ctl, 'chat_handler', None)
        matches = [m for m in getattr(chat, '_conv_index', []) if any(
            t.get('pm_id') == pm_id for t in m.get('pm_threads', []))]
        if len(matches) != 1 or matches[0].get('project_id', '') != task.project_id:
            raise ServiceError(409, '任务没有唯一且项目一致的会话绑定')
        cid = matches[0]['id']
        result = self._invoke('list_memories', conv_id=cid, agent_id=pm_id)
        if any(row['project_id'] != task.project_id for row in result['items']):
            raise ServiceError(409, '会话项目已变化，请重新读取记忆')
        return dict(scope=dict(task_id=task_id, pm_id=pm_id, conv_id=cid,
                               project_id=task.project_id), items=result['items'][:12])
