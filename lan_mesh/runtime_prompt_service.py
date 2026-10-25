"""RuntimePromptService: domain behavior separated from the public compatibility facade."""

from __future__ import annotations
from typing import Any
from .component_service import ComponentService

class RuntimePromptService(ComponentService):
    """RuntimePromptService business operations with explicit state ownership."""
    OPERATIONS = ('set_custom_prompt', '_build_system_prompt')

    def set_custom_prompt(self, prompt: str) -> Any:
        """设置/更新自定义 system prompt (PM 注入)。

        设置后, _build_system_prompt 将优先使用此 prompt,
        不再从技能缓存拼装。传入空字符串则恢复默认行为。
        """
        self.owner._custom_system_prompt = prompt

    def _build_system_prompt(self, task_context: str='') -> str:
        """构建 system prompt。

        优先使用 PM 注入的 custom_system_prompt (含角色、团队、依赖等定制信息)。
        若未设置, 则回退到从本地技能缓存拼装知识库 prompt。
        """
        if self.owner._custom_system_prompt:
            return self.owner._custom_system_prompt
        if not self.owner._skills_cache_dir.is_dir():
            return ''
        current_skill = getattr(self.owner._local, 'current_skill', '')
        target_skill_dir = None
        if current_skill:
            target_skill_dir = self.owner._skills_cache_dir / current_skill
        prompts = []
        if target_skill_dir and target_skill_dir.is_dir():
            skill_md = target_skill_dir / 'SKILL.md'
            if skill_md.is_file():
                try:
                    content = skill_md.read_text(encoding='utf-8')
                    if content.startswith('---'):
                        parts = content.split('---', 2)
                        if len(parts) >= 3:
                            content = parts[2].strip()
                    if content:
                        prompts.append(content)
                except Exception:
                    pass
        else:
            for skill_dir in sorted(self.owner._skills_cache_dir.iterdir()):
                if not skill_dir.is_dir():
                    continue
                skill_md = skill_dir / 'SKILL.md'
                if not skill_md.is_file():
                    continue
                try:
                    content = skill_md.read_text(encoding='utf-8')
                    if content.startswith('---'):
                        parts = content.split('---', 2)
                        if len(parts) >= 3:
                            content = parts[2].strip()
                    if content:
                        prompts.append(content)
                except Exception:
                    continue
        if not prompts:
            return ''
        return '以下是你的能力参考知识库，请在执行任务时参考:\n\n' + '\n\n---\n\n'.join(prompts)
