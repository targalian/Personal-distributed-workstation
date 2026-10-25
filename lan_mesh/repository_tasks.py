"""Domain SQL repository; Database remains the sole connection and schema owner."""

from __future__ import annotations
from typing import Any, Optional
from .protocol import Task
from .logger import get_logger
from .repository_registry import Repository
logger = get_logger("database")

class TasksRepository(Repository):
    """TasksRepository over the Database owner; SQL and connection policy are preserved."""

    def save_task(self, task: Task) -> Any:
        """保存任务 (含子任务)。"""
        import json
        conn = self.database._get_conn()
        conn.execute("""
            INSERT INTO tasks (task_id, name, description, input_data, output_data,
                              status, subtasks, created_at, completed_at, created_by, project_id, pm_agent_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(task_id) DO UPDATE SET
                name=excluded.name, description=excluded.description,
                input_data=excluded.input_data, output_data=excluded.output_data,
                status=excluded.status, subtasks=excluded.subtasks,
                completed_at=excluded.completed_at, project_id=excluded.project_id,
                pm_agent_id=excluded.pm_agent_id
        """, (
            task.task_id, task.name, task.description,
            json.dumps(task.input_data), json.dumps(task.output_data),
            task.status, json.dumps(task.subtasks),
            task.created_at, task.completed_at, task.created_by,
            task.project_id, task.pm_agent_id,
        ))
        conn.commit()


    def get_task(self, task_id: str) -> Optional[Task]:
        """查询单个任务。"""
        import json
        conn = self.database._get_conn()
        row = conn.execute(
            "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        if not row:
            return None
        return Task(
            task_id=row["task_id"], name=row["name"], description=row["description"],
            input_data=json.loads(row["input_data"]),
            output_data=json.loads(row["output_data"]),
            status=row["status"],
            subtasks=json.loads(row["subtasks"]),
            created_at=row["created_at"], completed_at=row["completed_at"],
            created_by=row["created_by"],
            project_id=row["project_id"] if "project_id" in row.keys() else "",
            pm_agent_id=row["pm_agent_id"] if "pm_agent_id" in row.keys() else "",
        )


    def update_task_status(self, task_id: str, status: str) -> Any:
        """更新任务状态 (用于取消/暂停)。"""
        conn = self.database._get_conn()
        conn.execute(
            "UPDATE tasks SET status = ? WHERE task_id = ?",
            (status, task_id),
        )
        conn.commit()


    def delete_task(self, task_id: str) -> bool:
        """彻底删除任务及关联的 PM Agent 和 Checkpoint 记录。"""
        conn = self.database._get_conn()
        conn.execute("DELETE FROM pm_agents WHERE task_id = ?", (task_id,))
        conn.execute("DELETE FROM graph_checkpoints WHERE task_id = ?", (task_id,))
        conn.execute("DELETE FROM pm_snapshots WHERE task_id = ?", (task_id,))
        cursor = conn.execute("DELETE FROM tasks WHERE task_id = ?", (task_id,))
        conn.commit()
        return cursor.rowcount > 0


    def update_task_subtasks(self, task_id: str, subtasks: list) -> Any:
        """更新任务的子任务列表 (PM 同步进度用)。"""
        import json
        conn = self.database._get_conn()
        conn.execute(
            "UPDATE tasks SET subtasks = ? WHERE task_id = ?",
            (json.dumps(subtasks, ensure_ascii=False), task_id),
        )
        conn.commit()


    def list_tasks(self, status: str = None, limit: int = 50) -> list[Task]:
        """列出任务,可按状态过滤。"""
        import json
        conn = self.database._get_conn()
        if status:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE status = ? ORDER BY created_at DESC LIMIT ?",
                (status, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [
            Task(
                task_id=r["task_id"], name=r["name"], description=r["description"],
                input_data=json.loads(r["input_data"]),
                output_data=json.loads(r["output_data"]),
                status=r["status"],
                subtasks=json.loads(r["subtasks"]),
                created_at=r["created_at"], completed_at=r["completed_at"],
                created_by=r["created_by"],
                project_id=r["project_id"] if "project_id" in r.keys() else "",
                pm_agent_id=r["pm_agent_id"] if "pm_agent_id" in r.keys() else "",
            )
            for r in rows
        ]

