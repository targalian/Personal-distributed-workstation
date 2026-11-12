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
                              project_id: str = "", usage_metadata: dict | None = None) -> bool:
        """写入一条 LLM 调用用量记录。

        Args:
            usage_id: 幂等键 (R3 跨主机上报去重); 非空且已存在 → 跳过。
            task_id/project_id: 成本归因 (R6); 留空表示无归因上下文。

        Returns:
            是否实际写入 (重复上报返回 False)
        """
        conn = self.database._get_conn()
        with conn:
            inserted = conn.execute("""
                INSERT OR IGNORE INTO resource_usage_log
                    (resource_id, model_id, plan_type, input_tokens, output_tokens,
                     cost, created_at, usage_id, task_id, project_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (resource_id, model_id, plan_type, int(input_tokens), int(output_tokens),
                  cost, time.time(), usage_id, task_id, project_id)).rowcount
            if not inserted and usage_id:
                previous = conn.execute('SELECT model_id,input_tokens,output_tokens,task_id,project_id '
                                        'FROM resource_usage_log WHERE usage_id=?', (usage_id,)).fetchone()
                if tuple(previous) != (model_id, int(input_tokens), int(output_tokens), task_id, project_id):
                    raise ValueError('usage_id_conflict')
            if usage_id and usage_metadata:
                conn.execute("INSERT OR IGNORE INTO usage_call_details VALUES (?, ?, ?, ?, ?)", (
                    usage_id, str(usage_metadata.get('attempt_id', '')),
                    str(usage_metadata.get('purpose', '')),
                    str(usage_metadata.get('outcome', 'unknown')),
                    int(bool(usage_metadata.get('usage_known', False)))))
        return bool(inserted)


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
        result = []
        for row in rows:
            item = dict(row)
            details = conn.execute('SELECT * FROM usage_call_details WHERE usage_id=?',
                                   (item['usage_id'],)).fetchone()
            if details:
                item['usage_metadata'] = dict(details)
            result.append(item)
        return result


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


    def query_attempt_usage(self, task_id: str, project_id: str, limit: int = 100) -> list[dict]:
        """Include failed/cancelled calls and explicitly count unknown provider usage."""
        rows = self.database._get_conn().execute("""
            SELECT d.attempt_id, d.purpose, d.outcome, COUNT(*) AS calls,
                   SUM(l.input_tokens + l.output_tokens) AS reported_tokens,
                   SUM(l.cost) AS reported_cost, SUM(1-d.usage_known) AS unknown_usage_calls
            FROM resource_usage_log l JOIN usage_call_details d ON l.usage_id=d.usage_id
            WHERE l.task_id=? AND l.project_id=?
            GROUP BY d.attempt_id,d.purpose,d.outcome ORDER BY MAX(l.created_at) DESC LIMIT ?
        """, (task_id, project_id, max(1, min(500, limit)))).fetchall()
        return [dict(row) for row in rows]

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

