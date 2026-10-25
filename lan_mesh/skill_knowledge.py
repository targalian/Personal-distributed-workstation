"""Packaged Yomk skill assets, task matching and bounded prompt enrichment."""

from pathlib import Path
from typing import Any

import yaml


BUILTIN_SKILLS_DIR = Path(__file__).parent / 'builtin_skills'
RESOURCE_NAMES = ('reference.md', 'examples.md', 'upstream-skill.md', 'UPSTREAM.md', 'LICENSE.txt')


def read_skill_document(directory: Path) -> tuple[dict[str, Any], str]:
    """Read metadata and body without requiring a writable installation directory."""
    try:
        raw = (directory / 'SKILL.md').read_text(encoding='utf-8')
        if raw.startswith('---'):
            parts = raw.split('---', 2)
            if len(parts) == 3:
                meta = yaml.safe_load(parts[1]) or {}
                if not isinstance(meta, dict):
                    return {}, parts[2].strip()
                extra = meta.get('metadata', {})
                if isinstance(extra, dict):
                    meta = {**extra, **meta}
                return meta, parts[2].strip()
        return {}, raw.strip()
    except (OSError, yaml.YAMLError) as exc:
        print(f'[Skill] 技能读取失败: {directory.name}: {exc}')
        return {}, ''


def builtin_directories() -> list[Path]:
    """Return packaged skills in stable order, including in installed wheels."""
    if not BUILTIN_SKILLS_DIR.is_dir():
        return []
    return sorted(p for p in BUILTIN_SKILLS_DIR.iterdir() if (p / 'SKILL.md').is_file())


def is_builtin_skill(skill_id: str) -> bool:
    """Identify only packaged skills, never arbitrary third-party instructions."""
    return any(p.name == skill_id for p in builtin_directories())


def matches_activation(metadata: dict, task_context: str) -> bool:
    """Evaluate opt-in any/all/unless keywords; empty context activates no opt-in skill."""
    rule = metadata.get('activation')
    if not isinstance(rule, dict):
        return True
    context = task_context.casefold()
    if not context.strip():
        return False
    contains = lambda word: str(word).casefold() in context
    return (all(contains(w) for w in rule.get('all', []))
            and (not rule.get('any') or any(contains(w) for w in rule['any']))
            and not any(contains(w) for w in rule.get('unless', [])))


def append_builtin_guidance(prompt: str, task_context: str) -> str:
    """Keep role instructions and add only relevant built-in skill entrypoints once."""
    sections = []
    for directory in builtin_directories():
        metadata, body = read_skill_document(directory)
        marker = f'## 工作站内置技能: {directory.name}'
        if body and matches_activation(metadata, task_context) and marker not in prompt:
            sections.append(f'{marker}\n资料目录: {directory}\n\n{body}')
    return '\n\n'.join(part for part in (prompt, *sections) if part)
