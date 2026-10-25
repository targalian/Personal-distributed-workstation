"""SkillAccessService: domain behavior separated from the public compatibility facade."""

from __future__ import annotations
from typing import Any
from .protocol import SkillRecord
from . import skill_registry as policy
from .component_service import ComponentService

class SkillAccessService(ComponentService):
    """SkillAccessService business operations with explicit state ownership."""
    OPERATIONS = ('assign_skill', 'revoke_skill', 'get_skill_assignments', 'get_skills_for_role', 'get_skills_for_agent', 'check_access')

    def assign_skill(self, skill_id: str, assignee_type: str, assignee_id: str) -> Any:
        """分配技能给角色/Agent/主机。

        Args:
            skill_id: 技能 ID
            assignee_type: 'role' | 'agent' | 'host'
            assignee_id: 角色名 / agent_id / device_id
        """
        self.owner.db.assign_skill(skill_id, assignee_type, assignee_id)
        policy.logger.info('技能 %s 已分配给 %s:%s', skill_id, assignee_type, assignee_id)

    def revoke_skill(self, skill_id: str, assignee_type: str, assignee_id: str) -> Any:
        """撤销技能分配。"""
        self.owner.db.revoke_skill(skill_id, assignee_type, assignee_id)
        policy.logger.info('技能 %s 已从 %s:%s 撤销', skill_id, assignee_type, assignee_id)

    def get_skill_assignments(self, skill_id: str) -> list[dict]:
        """查询技能的所有分配记录。"""
        return self.owner.db.get_skill_assignments(skill_id)

    def get_skills_for_role(self, role: str) -> list[SkillRecord]:
        """获取角色可用的技能列表。

        逻辑:
        1. default_access 含 role 或 'all' 的技能
        2. 直接分配给该角色 (assignee_type='role') 的技能
        """
        all_skills = self.owner.db.list_skills()
        result = []
        seen_ids = set()
        for row in all_skills:
            default_access = row.get('default_access', ['all'])
            if 'all' in default_access or role in default_access:
                result.append(policy.SkillRecord(**row))
                seen_ids.add(row['skill_id'])
        assigned_ids = self.owner.db.get_skills_for_assignee('role', role)
        for sid in assigned_ids:
            if sid not in seen_ids:
                skill_row = self.owner.db.get_skill(sid)
                if skill_row:
                    result.append(policy.SkillRecord(**skill_row))
                    seen_ids.add(sid)
        return result

    def get_skills_for_agent(self, agent_id: str, role: str='worker') -> list[SkillRecord]:
        """获取 Agent 可用的技能（角色默认 + 直接分配）。

        Args:
            agent_id: Agent ID
            role: Agent 的角色（用于查角色默认权限）
        """
        role_skills = self.owner.get_skills_for_role(role)
        seen_ids = {s.skill_id for s in role_skills}
        assigned_ids = self.owner.db.get_skills_for_assignee('agent', agent_id)
        for sid in assigned_ids:
            if sid not in seen_ids:
                skill_row = self.owner.db.get_skill(sid)
                if skill_row:
                    role_skills.append(policy.SkillRecord(**skill_row))
                    seen_ids.add(sid)
        return role_skills

    def check_access(self, skill_id: str, role: str, agent_id: str=None) -> bool:
        """检查角色/Agent 是否有权访问指定技能。"""
        skill = self.owner.get_skill(skill_id)
        if not skill:
            return False
        if role == 'station':
            return True
        if 'all' in skill.default_access or role in skill.default_access:
            return True
        assigned_role_ids = self.owner.db.get_skills_for_assignee('role', role)
        if skill_id in assigned_role_ids:
            return True
        if agent_id:
            assigned_agent_ids = self.owner.db.get_skills_for_assignee('agent', agent_id)
            if skill_id in assigned_agent_ids:
                return True
        return False
