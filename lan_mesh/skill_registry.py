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


class SkillRegistry:
    """中央技能注册表 — 管理技能注册、权限分配与内容分发。

    部署在 Station Director 端，是技能库的唯一权威来源。
    Worker 通过 HTTP API 拉取已授权技能到本地缓存。
    """

    def __init__(self, db: Database, skills_dir: str):
        self.db = db
        self.skills_dir = Path(skills_dir)
        self._content_cache: dict[str, dict] = {}  # skill_id → {content, reference}

    # ── 技能注册 ──────────────────────────────────────────────────

    def scan_and_register(self) -> list[dict]:
        """扫描 skills/ 目录，解析每个子目录的 SKILL.md front matter，
        自动注册新技能或更新已有技能元数据。

        Returns:
            新增/更新的技能列表 [{skill_id, action: "created"|"updated"}]
        """
        results = []
        if not self.skills_dir.is_dir():
            self.skills_dir.mkdir(parents=True, exist_ok=True)
            logger.info("skills 目录已创建: %s", self.skills_dir)
            return results

        for entry in sorted(self.skills_dir.iterdir()):
            if not entry.is_dir():
                continue
            skill_md = entry / "SKILL.md"
            if not skill_md.is_file():
                continue

            meta = self._parse_front_matter(skill_md)
            if not meta:
                continue

            skill_id = meta.get("name", entry.name).strip()
            existing = self.db.get_skill(skill_id)
            action = "updated" if existing else "created"

            # iter-61: front matter 未声明 default_access 时保持 DB 现值,
            # 防止重扫内置目录把 market 安装的安全默认 ["station"] 覆盖回 all
            if "default_access" in meta:
                default_access = meta["default_access"]
            else:
                default_access = (existing or {}).get("default_access", ["all"])

            self.db.upsert_skill(
                skill_id=skill_id,
                name=meta.get("name", skill_id),
                description=meta.get("description", ""),
                category=meta.get("category", "general"),
                tags=meta.get("tags", []),
                default_access=default_access,
                content_path=entry.name,
                version=meta.get("version", "1.0"),
            )
            results.append({"skill_id": skill_id, "action": action})
            logger.info("%s: %s (%s)", action, skill_id, entry.name)

        # 清除内容缓存（文件可能已变更）
        self._content_cache.clear()
        return results

    def _parse_front_matter(self, filepath: Path) -> dict:
        """解析 Markdown 文件的 YAML front matter。

        Returns:
            解析后的字典，无 front matter 时返回空字典。
        """
        try:
            content = filepath.read_text(encoding="utf-8")
        except Exception as e:
            logger.error("读取文件失败 %s: %s", filepath, e)
            return {}

        if not content.startswith("---"):
            return {}

        parts = content.split("---", 2)
        if len(parts) < 3:
            return {}

        try:
            meta = yaml.safe_load(parts[1]) or {}
            return meta
        except yaml.YAMLError as e:
            logger.error("YAML 解析失败 %s: %s", filepath, e)
            return {}

    # ── 技能查询 ──────────────────────────────────────────────────

    def list_skills(self, category: str = None) -> list[SkillRecord]:
        """列出所有已注册技能，可按分类过滤。"""
        rows = self.db.list_skills(category)
        return [SkillRecord(**row) for row in rows]

    def get_skill(self, skill_id: str) -> Optional[SkillRecord]:
        """获取单个技能元数据。"""
        row = self.db.get_skill(skill_id)
        return SkillRecord(**row) if row else None

    def get_skill_content(self, skill_id: str) -> dict:
        """获取技能完整内容（Markdown 正文）。

        Returns:
            {skill_id, name, content: "markdown正文", reference: "markdown"|"", version}
        """
        # 检查缓存
        if skill_id in self._content_cache:
            return self._content_cache[skill_id]

        skill = self.get_skill(skill_id)
        if not skill:
            return {"skill_id": skill_id, "content": "", "reference": "", "version": ""}

        skill_dir = self.skills_dir / skill.content_path
        result = {
            "skill_id": skill_id,
            "name": skill.name,
            "content": "",
            "reference": "",
            "version": skill.version,
        }

        # 读取主文件 (去掉 front matter)
        skill_md = skill_dir / "SKILL.md"
        if skill_md.is_file():
            raw = skill_md.read_text(encoding="utf-8")
            if raw.startswith("---"):
                parts = raw.split("---", 2)
                result["content"] = parts[2].strip() if len(parts) >= 3 else raw
            else:
                result["content"] = raw.strip()

        # 读取扩展参考
        ref_md = skill_dir / "reference.md"
        if ref_md.is_file():
            result["reference"] = ref_md.read_text(encoding="utf-8").strip()

        self._content_cache[skill_id] = result
        return result

    # ── 权限管理 ──────────────────────────────────────────────────

    def assign_skill(self, skill_id: str, assignee_type: str, assignee_id: str):
        """分配技能给角色/Agent/主机。

        Args:
            skill_id: 技能 ID
            assignee_type: 'role' | 'agent' | 'host'
            assignee_id: 角色名 / agent_id / device_id
        """
        self.db.assign_skill(skill_id, assignee_type, assignee_id)
        logger.info("技能 %s 已分配给 %s:%s", skill_id, assignee_type, assignee_id)

    def revoke_skill(self, skill_id: str, assignee_type: str, assignee_id: str):
        """撤销技能分配。"""
        self.db.revoke_skill(skill_id, assignee_type, assignee_id)
        logger.info("技能 %s 已从 %s:%s 撤销", skill_id, assignee_type, assignee_id)

    def get_skill_assignments(self, skill_id: str) -> list[dict]:
        """查询技能的所有分配记录。"""
        return self.db.get_skill_assignments(skill_id)

    def get_skills_for_role(self, role: str) -> list[SkillRecord]:
        """获取角色可用的技能列表。

        逻辑:
        1. default_access 含 role 或 'all' 的技能
        2. 直接分配给该角色 (assignee_type='role') 的技能
        """
        all_skills = self.db.list_skills()
        result = []
        seen_ids = set()

        for row in all_skills:
            default_access = row.get("default_access", ["all"])
            if "all" in default_access or role in default_access:
                result.append(SkillRecord(**row))
                seen_ids.add(row["skill_id"])

        # 添加直接分配给该角色的技能
        assigned_ids = self.db.get_skills_for_assignee("role", role)
        for sid in assigned_ids:
            if sid not in seen_ids:
                skill_row = self.db.get_skill(sid)
                if skill_row:
                    result.append(SkillRecord(**skill_row))
                    seen_ids.add(sid)

        return result

    def get_skills_for_agent(self, agent_id: str, role: str = "worker") -> list[SkillRecord]:
        """获取 Agent 可用的技能（角色默认 + 直接分配）。

        Args:
            agent_id: Agent ID
            role: Agent 的角色（用于查角色默认权限）
        """
        role_skills = self.get_skills_for_role(role)
        seen_ids = {s.skill_id for s in role_skills}

        # 添加直接分配给该 Agent 的技能
        assigned_ids = self.db.get_skills_for_assignee("agent", agent_id)
        for sid in assigned_ids:
            if sid not in seen_ids:
                skill_row = self.db.get_skill(sid)
                if skill_row:
                    role_skills.append(SkillRecord(**skill_row))
                    seen_ids.add(sid)

        return role_skills

    def check_access(self, skill_id: str, role: str, agent_id: str = None) -> bool:
        """检查角色/Agent 是否有权访问指定技能。"""
        skill = self.get_skill(skill_id)
        if not skill:
            return False

        # station 角色拥有全部权限
        if role == "station":
            return True

        # 检查默认权限
        if "all" in skill.default_access or role in skill.default_access:
            return True

        # 检查角色级分配
        assigned_role_ids = self.db.get_skills_for_assignee("role", role)
        if skill_id in assigned_role_ids:
            return True

        # 检查 Agent 级分配
        if agent_id:
            assigned_agent_ids = self.db.get_skills_for_assignee("agent", agent_id)
            if skill_id in assigned_agent_ids:
                return True

        return False

    # ── Worker 分发 ───────────────────────────────────────────────

    def build_skill_package(self, role: str, agent_id: str = None) -> list[dict]:
        """构建 Worker 拉取的技能包。

        Returns:
            [{skill_id, name, category, description, tags, content, reference, version}, ...]
        """
        if agent_id:
            skills = self.get_skills_for_agent(agent_id, role)
        else:
            skills = self.get_skills_for_role(role)

        package = []
        for skill in skills:
            content = self.get_skill_content(skill.skill_id)
            package.append({
                "skill_id": skill.skill_id,
                "name": skill.name,
                "category": skill.category,
                "description": skill.description,
                "tags": skill.tags,
                "content": content.get("content", ""),
                "reference": content.get("reference", ""),
                "version": skill.version,
            })
        return package

    def build_system_prompt(self, role: str, task_context: str = "",
                            agent_id: str = None) -> str:
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
            skills = self.get_skills_for_agent(agent_id, role)
        else:
            skills = self.get_skills_for_role(role)

        if not skills:
            return ""

        # 如果有任务上下文，尝试按 tags 匹配过滤
        if task_context:
            context_lower = task_context.lower()
            matched = []
            for skill in skills:
                # 检查 tags 是否与任务上下文相关
                tags_match = any(
                    tag.lower() in context_lower
                    for tag in skill.tags
                )
                # 检查技能名/描述是否与任务上下文相关
                name_match = skill.name.lower() in context_lower
                desc_match = any(
                    word.lower() in context_lower
                    for word in skill.description.split()
                    if len(word) > 2
                )
                if tags_match or name_match or desc_match:
                    matched.append(skill)

            # 如果有匹配的技能就用匹配的，否则用全部
            skills = matched if matched else skills

        # 拼接技能内容
        prompts = []
        for skill in skills:
            content = self.get_skill_content(skill.skill_id)
            text = content.get("content", "")
            if text:
                prompts.append(f"# 技能: {skill.name}\n\n{text}")

        if not prompts:
            return ""

        return "以下是你的能力参考知识库，请在执行任务时参考:\n\n" + "\n\n---\n\n".join(prompts)

    # ── 统计 ──────────────────────────────────────────────────────

    def stats(self) -> dict:
        """返回技能库统计信息。"""
        all_skills = self.db.list_skills()
        categories = {}
        for row in all_skills:
            cat = row.get("category", "general")
            categories[cat] = categories.get(cat, 0) + 1

        # 统计分配数
        conn = self.db._get_conn()
        total_assignments = conn.execute(
            "SELECT COUNT(*) as c FROM skill_assignments"
        ).fetchone()["c"]

        return {
            "total_skills": len(all_skills),
            "categories": categories,
            "total_assignments": total_assignments,
        }
