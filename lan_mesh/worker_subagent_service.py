"""WorkerSubagentService: domain behavior separated from the public compatibility facade."""

from __future__ import annotations
from . import worker as policy
from .component_service import ComponentService

class WorkerSubagentService(ComponentService):
    """WorkerSubagentService business operations with explicit state ownership."""
    OPERATIONS = ('create_subagent', 'cancel_subagent', 'get_subagent_status', 'update_subagent_prompt')

    def create_subagent(self, agent_name: str, skills: list, task_description: str='', system_prompt: str='', preferred_agent_id: str='') -> dict:
        """在本 Worker 上为 PM 创建子 Agent (新 AgentRuntime 实例)。

        Args:
            agent_name: 子 Agent 名称
            skills: 技能列表
            task_description: 任务描述
            system_prompt: PM 生成的定制 system prompt (角色/团队/依赖/质量要求)
            preferred_agent_id: PM 预先生成的 agent_id (用于 prompt 中引用)

        Returns:
            {agent_id, agent_name} 或 {ok: False, message}
        """
        import uuid as _uuid
        agent_id = preferred_agent_id or f'sub-{_uuid.uuid4().hex[:10]}'
        sub_runtime = policy.AgentRuntime(agent_id=agent_id, shared_folder_path=str(self.owner.state.shared_folder.path), custom_system_prompt=system_prompt)
        self.owner.state.sub_agents[agent_id] = {'agent_id': agent_id, 'agent_name': agent_name, 'runtime': sub_runtime, 'skills': skills, 'current_task': task_description, 'status': 'idle', 'progress': 0.0, 'has_custom_prompt': bool(system_prompt)}
        policy.logger.info('子 Agent 已创建: %s (%s)', agent_id, agent_name)
        return {'agent_id': agent_id, 'agent_name': agent_name}

    def cancel_subagent(self, agent_id: str) -> dict:
        """Request cooperative cancellation for a sub-agent runtime."""
        info = self.owner.state.sub_agents.get(agent_id)
        if not info:
            return {'ok': False, 'message': f'子 Agent {agent_id} 不存在'}
        runtime = info.get('runtime')
        cancel_runtime = getattr(runtime, 'cancel', None)
        if callable(cancel_runtime):
            cancel_runtime()
        info['status'] = 'cancelled'
        policy.logger.info('子 Agent 已请求取消: %s', agent_id)
        return {'ok': True, 'agent_id': agent_id}

    def get_subagent_status(self) -> dict:
        """返回本 Worker 上所有子 Agent 的状态。"""
        result = []
        for agent_id, info in list(self.owner.state.sub_agents.items()):
            result.append({'agent_id': agent_id, 'agent_name': info.get('agent_name', ''), 'current_task': info.get('current_task', ''), 'status': info.get('status', 'idle'), 'progress': info.get('progress', 0.0)})
        return {'sub_agents': result, 'total': len(result)}

    def update_subagent_prompt(self, agent_id: str, new_prompt: str) -> dict:
        """动态更新子 Agent 的 system prompt (优化2)。

        PM 可在任务执行中途调用此方法, 更新子 Agent 的定制 prompt,
        用于纠偏、补充上下文、调整策略。

        Args:
            agent_id: 子 Agent ID
            new_prompt: 新的 system prompt

        Returns:
            {ok: True} 或 {ok: False, message}
        """
        info = self.owner.state.sub_agents.get(agent_id)
        if not info:
            return {'ok': False, 'message': f'子 Agent {agent_id} 不存在'}
        runtime = info.get('runtime')
        if not runtime:
            return {'ok': False, 'message': '子 Agent runtime 不可用'}
        runtime.set_custom_prompt(new_prompt)
        info['has_custom_prompt'] = bool(new_prompt)
        policy.logger.info('子 Agent %s prompt 已更新 (%d 字符)', agent_id, len(new_prompt))
        return {'ok': True, 'agent_id': agent_id}
