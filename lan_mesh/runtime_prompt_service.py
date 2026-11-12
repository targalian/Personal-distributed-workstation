"""RuntimePromptService: task-scoped skill instructions and a bounded skill catalog."""

from __future__ import annotations

from typing import Any

from .component_service import ComponentService
from .skill_knowledge import (
    append_builtin_guidance, is_builtin_skill, matches_activation, read_skill_document,
)


class RuntimePromptService(ComponentService):
    """Compose prompts without injecting every cached skill into every task."""

    OPERATIONS = ('set_custom_prompt', '_build_system_prompt')

    def set_custom_prompt(self, prompt: str) -> Any:
        """Set a PM prompt; an empty value restores task-scoped skill selection."""
        self.owner._custom_system_prompt = prompt

    def _build_system_prompt(self, task_context: str = '') -> str:
        """Keep PM instructions and load explicit or opted-in skill bodies only."""
        if self.owner._custom_system_prompt:
            return append_builtin_guidance(self.owner._custom_system_prompt, task_context)
        root = self.owner._skills_cache_dir
        if not root.is_dir():
            return append_builtin_guidance('', task_context)
        current = getattr(self.owner._local, 'current_skill', '')
        sections, catalog = [], []
        remaining = 16000
        # Only known child directories can become an explicit skill selection.
        directories = sorted(root.iterdir(), key=lambda p: (p.name != current, p.name))
        for directory in directories:
            if not directory.is_dir() or is_builtin_skill(directory.name):
                continue
            if not (directory / 'SKILL.md').is_file():
                continue
            metadata, body = read_skill_document(directory)
            if not body:
                continue
            selected = directory.name == current or (
                isinstance(metadata.get('activation'), dict)
                and matches_activation(metadata, task_context))
            if selected and len(body) <= remaining:
                sections.append(f'## 技能: {directory.name}\n{body}')
                remaining -= len(body)
            elif len(catalog) < 24:
                description = str(metadata.get('description') or metadata.get('name') or directory.name)
                description = ' '.join(description.split())[:180]
                catalog.append(f'- {directory.name}: {description}\n  入口: {directory / "SKILL.md"}')
        if catalog:
            sections.append('以下技能仅供发现；需要时读取入口再使用，不代表已加载或获得工具权限:\n'
                            + '\n'.join(catalog))
        return append_builtin_guidance('\n\n'.join(sections), task_context)
