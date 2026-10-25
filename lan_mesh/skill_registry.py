"""
技能库注册表 — 中央技能管理与分发系统

架构角色:
  Station Director (中央技能库)
    ↓ 扫描 skills/ 目录
    ↓ 注册到 SQLite (skills 表)
    ↓ 权限分配 (skill_assignments 表)
  Worker (HTTP 拉取)
    ↓ GET /api/station/skills/download?role=worker
    ↓ 缓存到 ~/.lan_mesh/skills_cache/
  AgentRuntime (LLM 注入)
    ↓ 读取本地缓存
    ↓ 构建 system prompt

技能文件结构:
  skills/
    └── {skill_id}/
        ├── SKILL.md        ← 主内容 (含 YAML front matter)
        └── reference.md    ← 扩展参考 (可选)

front matter 格式:
  ---
  name: multi-agent-architect
  description: 多智能体协作架构决策框架
  category: architecture
  tags: [multi-agent, architecture, decision]
  default_access: ["station", "secretary"]
  version: "1.0"
  ---
"""
import json
import time
from pathlib import Path
from typing import Optional

import yaml

from .database import Database
from .protocol import SkillRecord
from .logger import get_logger

logger = get_logger("skill_registry")


from typing import Any

class SkillRegistry:
    """中央技能注册表 — 管理技能注册、权限分配与内容分发。

    部署在 Station Director 端，是技能库的唯一权威来源。
    Worker 通过 HTTP API 拉取已授权技能到本地缓存。
    """

    def __init__(self, db: Database, skills_dir: str):
        self.db = db
        self.skills_dir = Path(skills_dir)
        self._content_cache: dict[str, dict] = {}  # skill_id → {content, reference}
        self._skill_directories: dict[str, Path] = {}

    # ── 技能注册 ──────────────────────────────────────────────────

    def scan_and_register(self) -> list[dict]:
        return call_component(self, SkillCatalogService, 'scan_and_register')

    def _parse_front_matter(self, filepath: Path) -> dict:
        return call_component(self, SkillCatalogService, '_parse_front_matter', filepath=filepath)

    # ── 技能查询 ──────────────────────────────────────────────────

    def list_skills(self, category: str=None) -> list[SkillRecord]:
        return call_component(self, SkillCatalogService, 'list_skills', category=category)

    def get_skill(self, skill_id: str) -> Optional[SkillRecord]:
        return call_component(self, SkillCatalogService, 'get_skill', skill_id=skill_id)

    def get_skill_content(self, skill_id: str) -> dict:
        return call_component(self, SkillCatalogService, 'get_skill_content', skill_id=skill_id)

    # ── 权限管理 ──────────────────────────────────────────────────

    def assign_skill(self, skill_id: str, assignee_type: str, assignee_id: str) -> Any:
        return call_component(self, SkillAccessService, 'assign_skill', skill_id=skill_id, assignee_type=assignee_type, assignee_id=assignee_id)

    def revoke_skill(self, skill_id: str, assignee_type: str, assignee_id: str) -> Any:
        return call_component(self, SkillAccessService, 'revoke_skill', skill_id=skill_id, assignee_type=assignee_type, assignee_id=assignee_id)

    def get_skill_assignments(self, skill_id: str) -> list[dict]:
        return call_component(self, SkillAccessService, 'get_skill_assignments', skill_id=skill_id)

    def get_skills_for_role(self, role: str) -> list[SkillRecord]:
        return call_component(self, SkillAccessService, 'get_skills_for_role', role=role)

    def get_skills_for_agent(self, agent_id: str, role: str='worker') -> list[SkillRecord]:
        return call_component(self, SkillAccessService, 'get_skills_for_agent', agent_id=agent_id, role=role)

    def check_access(self, skill_id: str, role: str, agent_id: str=None) -> bool:
        return call_component(self, SkillAccessService, 'check_access', skill_id=skill_id, role=role, agent_id=agent_id)

    # ── Worker 分发 ───────────────────────────────────────────────

    def build_skill_package(self, role: str, agent_id: str=None) -> list[dict]:
        return call_component(self, SkillDistributionService, 'build_skill_package', role=role, agent_id=agent_id)

    def build_system_prompt(self, role: str, task_context: str='', agent_id: str=None) -> str:
        return call_component(self, SkillDistributionService, 'build_system_prompt', role=role, task_context=task_context, agent_id=agent_id)

    # ── 统计 ──────────────────────────────────────────────────────

    def stats(self) -> dict:
        return call_component(self, SkillCatalogService, 'stats')


from typing import Any
from .component_service import call_component
from .skill_catalog_service import SkillCatalogService
from .skill_access_service import SkillAccessService
from .skill_distribution_service import SkillDistributionService
