"""Domain SQL repository; Database remains the sole connection and schema owner."""

from __future__ import annotations
import json
import time
from typing import Any
from .logger import get_logger
from .repository_registry import Repository
logger = get_logger("database")

class ConversationsRepository(Repository):
    """ConversationsRepository over the Database owner; SQL and connection policy are preserved."""

    def save_chat_message(self, role: str, content: str, action_taken: str = "", timestamp: float = 0) -> Any:
        """保存一条聊天记录到持久化存储。"""
        conn = self.database._get_conn()
        ts = timestamp or time.time()
        conn.execute(
            "INSERT INTO chat_history (role, content, action_taken, timestamp) VALUES (?, ?, ?, ?)",
            (role, content, action_taken, ts),
        )
        conn.commit()


    def get_chat_history(self, limit: int = 100) -> list[dict]:
        """查询最近的聊天记录。"""
        conn = self.database._get_conn()
        rows = conn.execute(
            "SELECT role, content, action_taken, timestamp FROM chat_history ORDER BY timestamp ASC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


    def clear_chat_history(self) -> Any:
        """清空全部聊天记录。"""
        conn = self.database._get_conn()
        conn.execute("DELETE FROM chat_history")
        conn.commit()


    def save_task_memory(self, task_name: str, task_keywords: list,
                         task_type: str, collaboration_mode: str,
                         team_size: int, duration_secs: float,
                         success: bool, error_pattern: str = "",
                         boss_feedback: str = "", device_id: str = "") -> Any:
        """保存一条任务记忆 (PM 完成后调用)。

        记录任务模式, 用于未来同类任务的决策参考:
        - 什么类型的任务适合什么协作模式
        - 历史上哪些任务失败过, 失败原因是什么
        - Boss 对交付物的反馈偏好
        """
        conn = self.database._get_conn()
        conn.execute("""
            INSERT INTO task_memory (
                task_name, task_keywords, task_type, collaboration_mode,
                team_size, duration_secs, success, error_pattern,
                boss_feedback, device_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            task_name, json.dumps(task_keywords, ensure_ascii=False),
            task_type, collaboration_mode, team_size, duration_secs,
            1 if success else 0, error_pattern, boss_feedback,
            device_id, time.time(),
        ))
        conn.commit()


    def query_task_memory(self, task_type: str = "", keyword: str = "",
                          limit: int = 10) -> list[dict]:
        """查询任务记忆 (按类型或关键词)。

        用于新任务提交时参考历史经验:
        - 同类型任务的历史成功/失败率
        - 推荐的协作模式和团队规模
        - 常见错误模式预警
        """
        conn = self.database._get_conn()
        if task_type:
            rows = conn.execute(
                "SELECT * FROM task_memory WHERE task_type = ? ORDER BY created_at DESC LIMIT ?",
                (task_type, limit),
            ).fetchall()
        elif keyword:
            rows = conn.execute(
                "SELECT * FROM task_memory WHERE task_keywords LIKE ? ORDER BY created_at DESC LIMIT ?",
                (f"%{keyword}%", limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM task_memory ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        results = []
        for r in rows:
            d = dict(r)
            d["task_keywords"] = json.loads(d["task_keywords"]) if d["task_keywords"] else []
            d["success"] = bool(d["success"])
            results.append(d)
        return results


    def get_task_memory_stats(self, task_type: str = "") -> dict:
        """统计任务记忆 (成功率、平均耗时、推荐模式)。"""
        conn = self.database._get_conn()
        if task_type:
            rows = conn.execute(
                "SELECT * FROM task_memory WHERE task_type = ?", (task_type,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM task_memory").fetchall()

        if not rows:
            return {"total": 0, "success_rate": 0, "avg_duration": 0, "recommended_mode": ""}

        total = len(rows)
        success_count = sum(1 for r in rows if r["success"])
        avg_duration = sum(r["duration_secs"] for r in rows) / total if total else 0

        # 统计最常用的协作模式
        mode_counts: dict[str, int] = {}
        for r in rows:
            mode = r["collaboration_mode"]
            if mode:
                mode_counts[mode] = mode_counts.get(mode, 0) + 1
        recommended_mode = max(mode_counts, key=mode_counts.get) if mode_counts else ""

        # 常见错误模式
        error_patterns: dict[str, int] = {}
        for r in rows:
            ep = r["error_pattern"]
            if ep:
                error_patterns[ep] = error_patterns.get(ep, 0) + 1

        return {
            "total": total,
            "success_rate": success_count / total if total else 0,
            "avg_duration": avg_duration,
            "recommended_mode": recommended_mode,
            "common_errors": sorted(error_patterns.items(), key=lambda x: -x[1])[:3],
        }

