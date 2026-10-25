"""Domain SQL repository; Database remains the sole connection and schema owner."""

from __future__ import annotations
import json
import time
from typing import Any
from .logger import get_logger
from .repository_registry import Repository
logger = get_logger("database")

class ProjectsRepository(Repository):
    """ProjectsRepository over the Database owner; SQL and connection policy are preserved."""

    def upsert_project(self, project) -> Any:
        """插入或更新项目记录。"""
        conn = self.database._get_conn()
        conn.execute("""
            INSERT INTO projects (
                project_id, name, description, workspace_path,
                budget_limit_usd, budget_used_usd, allowed_models,
                routing_strategy, status, charter, roadmap, decisions,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(project_id) DO UPDATE SET
                name=excluded.name, description=excluded.description,
                workspace_path=excluded.workspace_path,
                budget_limit_usd=excluded.budget_limit_usd,
                budget_used_usd=excluded.budget_used_usd,
                allowed_models=excluded.allowed_models,
                routing_strategy=excluded.routing_strategy,
                status=excluded.status, charter=excluded.charter,
                roadmap=excluded.roadmap, decisions=excluded.decisions,
                updated_at=excluded.updated_at
        """, (
            project.project_id, project.name, project.description,
            project.workspace_path,
            project.budget_limit_usd, project.budget_used_usd,
            json.dumps(project.allowed_models),
            project.routing_strategy, project.status,
            json.dumps(project.charter), json.dumps(project.roadmap),
            json.dumps(project.decisions),
            project.created_at, project.updated_at,
        ))
        conn.commit()


    def get_project(self, project_id: str) -> Any:
        """查询单个项目。"""
        conn = self.database._get_conn()
        row = conn.execute(
            "SELECT * FROM projects WHERE project_id = ?", (project_id,)
        ).fetchone()
        if not row:
            return None
        return self.database._row_to_project(row)


    def list_projects(self, status: str = None) -> list:
        """列出所有项目,可按状态过滤。"""
        conn = self.database._get_conn()
        if status:
            rows = conn.execute(
                "SELECT * FROM projects WHERE status = ? ORDER BY updated_at DESC",
                (status,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM projects ORDER BY updated_at DESC"
            ).fetchall()
        return [self.database._row_to_project(r) for r in rows]


    def delete_project(self, project_id: str) -> Any:
        """归档项目 (软删除,设置 status=archived)。"""
        conn = self.database._get_conn()
        conn.execute(
            "UPDATE projects SET status = 'archived', updated_at = ? WHERE project_id = ?",
            (time.time(), project_id),
        )
        conn.commit()


    def update_project_budget(self, project_id: str, used_usd: float) -> Any:
        """更新项目已消费金额。"""
        conn = self.database._get_conn()
        conn.execute(
            "UPDATE projects SET budget_used_usd = ?, updated_at = ? WHERE project_id = ?",
            (used_usd, time.time(), project_id),
        )
        conn.commit()


    def update_project_status(self, project_id: str, status: str) -> Any:
        """更新项目状态。"""
        conn = self.database._get_conn()
        conn.execute(
            "UPDATE projects SET status = ?, updated_at = ? WHERE project_id = ?",
            (status, time.time(), project_id),
        )
        conn.commit()


    def record_usage(self, project_id: str, task_id: str, subtask_id: str,
                     model: str, input_tokens: int, output_tokens: int,
                     cost_usd: float) -> Any:
        """记录一次模型调用的消费。"""
        conn = self.database._get_conn()
        conn.execute("""
            INSERT INTO usage_log
                (project_id, task_id, subtask_id, model, input_tokens, output_tokens, cost_usd, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (project_id, task_id, subtask_id, model,
              input_tokens, output_tokens, cost_usd, time.time()))
        conn.commit()


    def get_usage_log(self, project_id: str, limit: int = 100) -> list[dict]:
        """查询项目的消费记录。"""
        conn = self.database._get_conn()
        rows = conn.execute(
            """SELECT * FROM usage_log WHERE project_id = ?
               ORDER BY timestamp DESC LIMIT ?""",
            (project_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]

