"""Domain SQL repository; Database remains the sole connection and schema owner."""

from __future__ import annotations
import json
import time
from typing import Any, Optional
from .logger import get_logger
from .repository_registry import Repository
logger = get_logger("database")

class SkillsRepository(Repository):
    """SkillsRepository over the Database owner; SQL and connection policy are preserved."""

    def upsert_skill(self, skill_id: str, name: str, description: str,
                      category: str, tags: list, default_access: list,
                      content_path: str, version: str = "1.0",
                      origin: str = "builtin") -> Any:
        """插入或更新技能记录。

        origin 仅首次插入时写入 (iter-61): 更新不覆盖来源标记,
        防止重扫内置目录把市场安装技能误标回 builtin。
        """
        conn = self.database._get_conn()
        now = time.time()
        conn.execute("""
            INSERT INTO skills (
                skill_id, name, description, category, tags,
                default_access, content_path, origin, version, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(skill_id) DO UPDATE SET
                name=excluded.name,
                description=excluded.description,
                category=excluded.category,
                tags=excluded.tags,
                default_access=excluded.default_access,
                content_path=excluded.content_path,
                version=excluded.version,
                updated_at=excluded.updated_at
        """, (
            skill_id, name, description, category,
            json.dumps(tags, ensure_ascii=False),
            json.dumps(default_access, ensure_ascii=False),
            content_path, origin, version, now, now,
        ))
        conn.commit()


    def get_skill(self, skill_id: str) -> Optional[dict]:
        """查询单个技能记录。"""
        conn = self.database._get_conn()
        row = conn.execute(
            "SELECT * FROM skills WHERE skill_id = ?", (skill_id,)
        ).fetchone()
        if not row:
            return None
        return self.database._row_to_skill(row)


    def list_skills(self, category: str = None) -> list[dict]:
        """列出所有技能,可按分类过滤。"""
        conn = self.database._get_conn()
        if category:
            rows = conn.execute(
                "SELECT * FROM skills WHERE category = ? ORDER BY updated_at DESC",
                (category,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM skills ORDER BY updated_at DESC"
            ).fetchall()
        return [self.database._row_to_skill(r) for r in rows]


    def delete_skill(self, skill_id: str) -> Any:
        """删除技能及其所有分配记录。"""
        conn = self.database._get_conn()
        conn.execute("DELETE FROM skills WHERE skill_id = ?", (skill_id,))
        conn.execute("DELETE FROM skill_assignments WHERE skill_id = ?", (skill_id,))
        conn.commit()


    def assign_skill(self, skill_id: str, assignee_type: str, assignee_id: str) -> Any:
        """分配技能给角色/Agent/主机。"""
        conn = self.database._get_conn()
        conn.execute(
            """INSERT OR IGNORE INTO skill_assignments
               (skill_id, assignee_type, assignee_id, assigned_at)
               VALUES (?, ?, ?, ?)""",
            (skill_id, assignee_type, assignee_id, time.time()),
        )
        conn.commit()


    def revoke_skill(self, skill_id: str, assignee_type: str, assignee_id: str) -> Any:
        """撤销技能分配。"""
        conn = self.database._get_conn()
        conn.execute(
            """DELETE FROM skill_assignments
               WHERE skill_id = ? AND assignee_type = ? AND assignee_id = ?""",
            (skill_id, assignee_type, assignee_id),
        )
        conn.commit()


    def get_skill_assignments(self, skill_id: str) -> list[dict]:
        """查询技能的所有分配记录。"""
        conn = self.database._get_conn()
        rows = conn.execute(
            "SELECT * FROM skill_assignments WHERE skill_id = ? ORDER BY assigned_at DESC",
            (skill_id,),
        ).fetchall()
        return [dict(r) for r in rows]


    def get_skills_for_assignee(self, assignee_type: str, assignee_id: str) -> list[str]:
        """查询指定角色/Agent/主机被分配的技能 ID 列表。"""
        conn = self.database._get_conn()
        rows = conn.execute(
            """SELECT skill_id FROM skill_assignments
               WHERE assignee_type = ? AND assignee_id = ?""",
            (assignee_type, assignee_id),
        ).fetchall()
        return [r["skill_id"] for r in rows]

