"""Domain SQL repository; Database remains the sole connection and schema owner."""

from __future__ import annotations
import time
from typing import Any, Optional
from .logger import get_logger
from .repository_registry import Repository
logger = get_logger("database")

class CheckpointsRepository(Repository):
    """CheckpointsRepository over the Database owner; SQL and connection policy are preserved."""

    def save_checkpoint(self, checkpoint_id: str, task_id: str, phase: str,
                        dag_json: str, context_json: str, history_json: str) -> Any:
        """保存图执行检查点。"""
        conn = self.database._get_conn()
        conn.execute("""
            INSERT OR REPLACE INTO graph_checkpoints
                (checkpoint_id, task_id, phase, dag_json, context_json, history_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (checkpoint_id, task_id, phase, dag_json, context_json, history_json, time.time()))
        conn.commit()


    def get_latest_checkpoint(self, task_id: str) -> Optional[dict]:
        """获取指定任务的最新检查点。"""
        conn = self.database._get_conn()
        row = conn.execute("""
            SELECT * FROM graph_checkpoints
            WHERE task_id = ?
            ORDER BY created_at DESC LIMIT 1
        """, (task_id,)).fetchone()
        if not row:
            return None
        return dict(row)


    def list_checkpoints(self, task_id: str) -> list:
        """列出指定任务的所有检查点 (按时间倒序)。"""
        conn = self.database._get_conn()
        rows = conn.execute("""
            SELECT checkpoint_id, task_id, phase, created_at
            FROM graph_checkpoints
            WHERE task_id = ?
            ORDER BY created_at DESC
        """, (task_id,)).fetchall()
        return [dict(r) for r in rows]


    def delete_checkpoints(self, task_id: str) -> Any:
        """删除指定任务的所有检查点。"""
        conn = self.database._get_conn()
        conn.execute("DELETE FROM graph_checkpoints WHERE task_id = ?", (task_id,))
        conn.commit()


    def save_pm_snapshot(self, pm_id: str, task_id: str, phase: str,
                         state_json: str) -> None:
        """iter-53: 保存 PM 执行态快照 (UPSERT, 一个 PM 只保留最新快照)。"""
        conn = self.database._get_conn()
        conn.execute("""
            INSERT INTO pm_snapshots (pm_id, task_id, phase, state_json, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(pm_id) DO UPDATE SET
                task_id = excluded.task_id,
                phase = excluded.phase,
                state_json = excluded.state_json,
                updated_at = excluded.updated_at
        """, (pm_id, task_id, phase, state_json, time.time()))
        conn.commit()


    def get_pm_snapshot(self, pm_id: str) -> Optional[dict]:
        """iter-53: 获取指定 PM 的最新执行态快照。"""
        conn = self.database._get_conn()
        row = conn.execute(
            "SELECT * FROM pm_snapshots WHERE pm_id = ?", (pm_id,)
        ).fetchone()
        return dict(row) if row else None


    def get_pm_snapshot_by_task(self, task_id: str) -> Optional[dict]:
        """iter-53: 按任务 ID 查找最新快照 (resume 端点入口)。"""
        conn = self.database._get_conn()
        row = conn.execute("""
            SELECT * FROM pm_snapshots WHERE task_id = ?
            ORDER BY updated_at DESC LIMIT 1
        """, (task_id,)).fetchone()
        return dict(row) if row else None


    def delete_pm_snapshot(self, pm_id: str) -> None:
        """iter-53: 删除指定 PM 的执行态快照 (任务终结时清理)。"""
        conn = self.database._get_conn()
        conn.execute("DELETE FROM pm_snapshots WHERE pm_id = ?", (pm_id,))
        conn.commit()

