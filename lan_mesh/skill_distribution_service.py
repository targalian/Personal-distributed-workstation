"""SkillDistributionService: domain behavior separated from the public compatibility facade."""

from __future__ import annotations
from .component_service import ComponentService
from .skill_knowledge import builtin_directories, matches_activation, read_skill_document

class SkillDistributionService(ComponentService):
    """SkillDistributionService business operations with explicit state ownership."""
    OPERATIONS = ('build_skill_package', 'build_system_prompt')

    def build_skill_package(self, role: str, agent_id: str=None) -> list[dict]:
        """构建 Worker 拉取的技能包。

        Returns:
            [{skill_id, name, category, description, tags, content, reference, version}, ...]
        """
        if agent_id:
            skills = self.owner.get_skills_for_agent(agent_id, role)
        else:
            skills = self.owner.get_skills_for_role(role)
        package = []
        for skill in skills:
            content = self.owner.get_skill_content(skill.skill_id)
            package.append({'skill_id': skill.skill_id, 'name': skill.name, 'category': skill.category, 'description': skill.description, 'tags': skill.tags, 'content': content.get('content', ''), 'reference': content.get('reference', ''), 'version': skill.version, 'resources': content.get('resources', {})})
        return package

    def build_system_prompt(self, role: str, task_context: str='', agent_id: str=None) -> str:
        """根据角色和任务上下文构建 system prompt。

        1. 获取角色可用技能
        2. 按 task_context 中的关键词匹配 tags（可选过滤）
        3. 拼接技能内容为 system prompt

        Args:
            role: 角色名
            task_context: 任务描述（用于按 tag 过滤相关技能）
            agent_id: Agent ID（用于查直接分配的技能）

        Returns:
            拼接好的 system prompt 字符串
        """
        if agent_id:
            skills = self.owner.get_skills_for_agent(agent_id, role)
        else:
            skills = self.owner.get_skills_for_role(role)
        builtin = {directory.name: read_skill_document(directory)[0]
                   for directory in builtin_directories()}
        skills = [skill for skill in skills if skill.skill_id not in builtin
                  or matches_activation(builtin[skill.skill_id], task_context)]
        if not skills:
            return ''
        if task_context:
            context_lower = task_context.lower()
            matched = []
            for skill in skills:
                tags_match = any((tag.lower() in context_lower for tag in skill.tags))
                name_match = skill.name.lower() in context_lower
                desc_match = any((word.lower() in context_lower for word in skill.description.split() if len(word) > 2))
                if skill.skill_id in builtin or tags_match or name_match or desc_match:
                    matched.append(skill)
            skills = matched if matched else skills
        prompts = []
        for skill in skills:
            content = self.owner.get_skill_content(skill.skill_id)
            text = content.get('content', '')
            if text:
                prompts.append(f'# 技能: {skill.name}\n\n{text}')
        if not prompts:
            return ''
        return '以下是你的能力参考知识库，请在执行任务时参考:\n\n' + '\n\n---\n\n'.join(prompts)
