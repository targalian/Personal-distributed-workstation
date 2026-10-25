"""Domain SQL repository; Database remains the sole connection and schema owner."""

from __future__ import annotations
import time
from .logger import get_logger
from .repository_registry import Repository
logger = get_logger("database")

class ResourcesRepository(Repository):
    """ResourcesRepository over the Database owner; SQL and connection policy are preserved."""

    def insert_resource_usage(self, resource_id: str, model_id: str,
                              plan_type: str, input_tokens: int,
                              output_tokens: int, cost: float,
                              usage_id: str = "", task_id: str = "",
                              project_id: str = "") -> bool:
        """写入一条 LLM 调用用量记录。

        Args:
            usage_id: 幂等键 (R3 跨主机上报去重); 非空且已存在 → 跳过。
            task_id/project_id: 成本归因 (R6); 留空表示无归因上下文。

        Returns:
            是否实际写入 (重复上报返回 False)
        """
        conn = self.database._get_conn()
        if usage_id:
            dup = conn.execute(
                "SELECT 1 FROM resource_usage_log WHERE usage_id = ? LIMIT 1",
                (usage_id,)).fetchone()
            if dup:
                return False
        conn.execute("""
            INSERT INTO resource_usage_log
                (resource_id, model_id, plan_type, input_tokens, output_tokens,
                 cost, created_at, usage_id, task_id, project_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (resource_id, model_id, plan_type, int(input_tokens),
              int(output_tokens), cost, time.time(), usage_id,
              task_id, project_id))
        conn.commit()
        return True


    def query_unreported_usage(self, limit: int = 200) -> list:
        """查询未上报 Secretary 的用量记录 (R3 跨主机上报)。"""
        conn = self.database._get_conn()
        rows = conn.execute("""
            SELECT id, usage_id, model_id, input_tokens, output_tokens,
                   task_id, project_id
            FROM resource_usage_log
            WHERE reported = 0
            ORDER BY id ASC LIMIT ?
        """, (int(limit),)).fetchall()
        return [dict(r) for r in rows]


    def mark_usage_reported(self, ids: list) -> None:
        """标记用量记录已上报 (R3)。"""
        if not ids:
            return
        conn = self.database._get_conn()
        placeholders = ",".join("?" for _ in ids)
        conn.execute(f"""
            UPDATE resource_usage_log SET reported = 1
            WHERE id IN ({placeholders})
        """, [int(i) for i in ids])
        conn.commit()


    def sum_resource_usage(self, resource_id: str, since_ts: float) -> dict:
        """聚合指定资源池在窗口内的用量。

        Returns:
            {"tokens": 输入+输出 token 总数, "cost": 折算金额总数}
        """
        conn = self.database._get_conn()
        row = conn.execute("""
            SELECT COALESCE(SUM(input_tokens + output_tokens), 0) AS tokens,
                   COALESCE(SUM(cost), 0) AS cost
            FROM resource_usage_log
            WHERE resource_id = ? AND created_at >= ?
        """, (resource_id, since_ts)).fetchone()
        return {"tokens": row["tokens"], "cost": row["cost"]}


    def query_cost_by_task(self, limit: int = 100) -> list:
        """R6: 按 task_id 聚合成本分摊 (含未归因汇总行)。

        Returns:
            [{"task_id", "project_id", "calls", "tokens", "cost",
              "last_at"}] 按 cost 降序; 无归因记录 task_id 为空串。
        """
        conn = self.database._get_conn()
        rows = conn.execute("""
            SELECT task_id,
                   MAX(project_id) AS project_id,
                   COUNT(*) AS calls,
                   COALESCE(SUM(input_tokens + output_tokens), 0) AS tokens,
                   COALESCE(SUM(cost), 0) AS cost,
                   MAX(created_at) AS last_at
            FROM resource_usage_log
            GROUP BY task_id
            ORDER BY cost DESC, tokens DESC
            LIMIT ?
        """, (int(limit),)).fetchall()
        return [dict(r) for r in rows]


    def avg_tokens_per_task(self, days: int = 30) -> dict:
        """F4.4: 近 N 天有任务归因的资源用量, 每任务 token 均值 (供预算预估)。

        Returns:
            {"avg": float, "samples": int}; 无归因记录返回全零。
        """
        conn = self.database._get_conn()
        cutoff = time.time() - int(days) * 86400
        rows = conn.execute("""
            SELECT task_id, SUM(input_tokens + output_tokens) AS tokens
            FROM resource_usage_log
            WHERE created_at >= ? AND task_id != ''
            GROUP BY task_id
        """, (cutoff,)).fetchall()
        if not rows:
            return {"avg": 0.0, "samples": 0}
        total = sum(r["tokens"] for r in rows)
        return {"avg": round(total / len(rows), 1), "samples": len(rows)}

