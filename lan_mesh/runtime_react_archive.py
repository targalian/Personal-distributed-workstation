"""Execution-local archive integration and bounded re-reading of full observations."""

import copy
from typing import Any

from .protocol import ToolDef
from .runtime_artifacts import RuntimeArtifacts


class ReactArchive:
    """Retain the full transcript separately from the mutable model context."""

    def __init__(self, owner: object, data: dict, registry: Any):
        scope = dict(agent_id=owner.agent_id, task_id=data.get('_task_id', ''),
                     project_id=data.get('_project_id', ''), attempt_id=data.get('_attempt_id', ''))
        self.store = RuntimeArtifacts.create(owner.shared_folder, scope)
        self.transcript: list[dict] = []
        registry.register_tool(ToolDef(name='artifact_read', description=(
            '读取本次执行保存的完整工具结果或检查点。返回字符分页；需要时用next_offset继续。'),
            input_schema={'type': 'object', 'properties': {
                'artifact_id': {'type': 'string'}, 'offset': {'type': 'integer', 'minimum': 0},
                'limit': {'type': 'integer', 'minimum': 1, 'maximum': 16000},
                'view': {'type': 'string', 'enum': ['observation', 'record']}},
                'required': ['artifact_id']}), self.read_tool)

    def read_tool(self, params: dict) -> dict:
        """The model cannot choose another run, path, project, or attempt."""
        return self.store.read(params['artifact_id'], int(params.get('offset', 0)),
                               int(params.get('limit', 4000)), params.get('view', 'observation'))

    def append(self, message: dict) -> None:
        self.transcript.append(copy.deepcopy(message))

    def checkpoint(self, state: dict, phase: str, **details: object) -> dict:
        """Save original messages and observations before attempting any compaction."""
        snapshot = {k: v for k, v in state.items() if not k.startswith('_')}
        return self.store.checkpoint(self.transcript, snapshot, phase=phase, **details)
