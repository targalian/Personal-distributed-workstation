"""SkillCatalogService: domain behavior separated from the public compatibility facade."""

from __future__ import annotations
from typing import Optional
from pathlib import Path
from .protocol import SkillRecord
from . import skill_registry as policy
from .component_service import ComponentService
from .skill_knowledge import builtin_directories, RESOURCE_NAMES

class SkillCatalogService(ComponentService):
    """SkillCatalogService business operations with explicit state ownership."""
    OPERATIONS = ('scan_and_register', '_parse_front_matter', 'list_skills', 'get_skill', 'get_skill_content', 'stats')

    def scan_and_register(self) -> list[dict]:
        """扫描 skills/ 目录，解析每个子目录的 SKILL.md front matter，
        自动注册新技能或更新已有技能元数据。

        Returns:
            新增/更新的技能列表 [{skill_id, action: "created"|"updated"}]
        """
        results = []
        directories = {entry.name: entry for entry in builtin_directories()}
        if self.owner.skills_dir.is_dir():
            directories.update({entry.name: entry for entry in self.owner.skills_dir.iterdir()
                                if entry.is_dir()})
        self.owner._skill_directories.clear()
        for entry in sorted(directories.values()):
            if not entry.is_dir():
                continue
            skill_md = entry / 'SKILL.md'
            if not skill_md.is_file():
                continue
            meta = self.owner._parse_front_matter(skill_md)
            if not meta:
                continue
            skill_id = meta.get('name', entry.name).strip()
            existing = self.owner.db.get_skill(skill_id)
            action = 'updated' if existing else 'created'
            if 'default_access' in meta:
                default_access = meta['default_access']
            else:
                default_access = (existing or {}).get('default_access', ['all'])
            self.owner.db.upsert_skill(skill_id=skill_id, name=meta.get('name', skill_id), description=meta.get('description', ''), category=meta.get('category', 'general'), tags=meta.get('tags', []), default_access=default_access, content_path=entry.name, version=meta.get('version', '1.0'))
            self.owner._skill_directories[skill_id] = entry
            results.append({'skill_id': skill_id, 'action': action})
            policy.logger.info('%s: %s (%s)', action, skill_id, entry.name)
        self.owner._content_cache.clear()
        return results

    def _parse_front_matter(self, filepath: Path) -> dict:
        """解析 Markdown 文件的 YAML front matter。

        Returns:
            解析后的字典，无 front matter 时返回空字典。
        """
        try:
            content = filepath.read_text(encoding='utf-8')
        except Exception as e:
            policy.logger.error('读取文件失败 %s: %s', filepath, e)
            return {}
        if not content.startswith('---'):
            return {}
        parts = content.split('---', 2)
        if len(parts) < 3:
            return {}
        try:
            meta = policy.yaml.safe_load(parts[1]) or {}
            if isinstance(meta, dict) and isinstance(meta.get('metadata'), dict):
                meta = {**meta['metadata'], **meta}
            return meta
        except policy.yaml.YAMLError as e:
            policy.logger.error('YAML 解析失败 %s: %s', filepath, e)
            return {}

    def list_skills(self, category: str=None) -> list[SkillRecord]:
        """列出所有已注册技能，可按分类过滤。"""
        rows = self.owner.db.list_skills(category)
        return [SkillRecord(**row) for row in rows]

    def get_skill(self, skill_id: str) -> Optional[SkillRecord]:
        """获取单个技能元数据。"""
        row = self.owner.db.get_skill(skill_id)
        return policy.SkillRecord(**row) if row else None

    def get_skill_content(self, skill_id: str) -> dict:
        """获取技能完整内容（Markdown 正文）。

        Returns:
            {skill_id, name, content: "markdown正文", reference: "markdown"|"", version}
        """
        if skill_id in self.owner._content_cache:
            return self.owner._content_cache[skill_id]
        skill = self.owner.get_skill(skill_id)
        if not skill:
            return {'skill_id': skill_id, 'content': '', 'reference': '', 'version': ''}
        skill_dir = self.owner._skill_directories.get(skill_id,
            self.owner.skills_dir / skill.content_path)
        result = {'skill_id': skill_id, 'name': skill.name, 'content': '', 'reference': '', 'version': skill.version}
        skill_md = skill_dir / 'SKILL.md'
        if skill_md.is_file():
            raw = skill_md.read_text(encoding='utf-8')
            if raw.startswith('---'):
                parts = raw.split('---', 2)
                result['content'] = parts[2].strip() if len(parts) >= 3 else raw
            else:
                result['content'] = raw.strip()
        ref_md = skill_dir / 'reference.md'
        if ref_md.is_file():
            result['reference'] = ref_md.read_text(encoding='utf-8').strip()
        result['resources'] = {name: (skill_dir / name).read_text(encoding='utf-8')
                               for name in RESOURCE_NAMES if (skill_dir / name).is_file()}
        self.owner._content_cache[skill_id] = result
        return result

    def stats(self) -> dict:
        """返回技能库统计信息。"""
        all_skills = self.owner.db.list_skills()
        categories = {}
        for row in all_skills:
            cat = row.get('category', 'general')
            categories[cat] = categories.get(cat, 0) + 1
        conn = self.owner.db._get_conn()
        total_assignments = conn.execute('SELECT COUNT(*) as c FROM skill_assignments').fetchone()['c']
        return {'total_skills': len(all_skills), 'categories': categories, 'total_assignments': total_assignments}
