"""Domain SQL repository; Database remains the sole connection and schema owner."""

from __future__ import annotations
import time
from .logger import get_logger
from .repository_registry import Repository
logger = get_logger("database")

class MetricsRepository(Repository):
    """MetricsRepository over the Database owner; SQL and connection policy are preserved."""

    def insert_llm_call(self, call_type: str, model: str,
                        input_tokens: int, output_tokens: int,
                        ttft_ms: float, total_ms: float,
                        status: str = "ok", task_id: str = "",
                        error: str = "") -> None:
        """写入一条 LLM 调用审计记录 (runtime_trace 双写入口)。"""
        conn = self.database._get_conn()
        conn.execute("""
            INSERT INTO llm_call_log
                (call_type, model, input_tokens, output_tokens,
                 ttft_ms, total_ms, status, task_id, error, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (call_type, model, int(input_tokens), int(output_tokens),
              float(ttft_ms), float(total_ms), status, task_id,
              error[:500], time.time()))
        conn.commit()


    def query_llm_metrics(self, hours: float = 1.0) -> dict:
        """聚合时间窗口内的调用总量、延迟分位、模型/状态分组和最近错误。"""
        conn = self.database._get_conn()
        cutoff = time.time() - hours * 3600

        # 总量与平均延迟
        row = conn.execute("""
            SELECT COUNT(*) AS total_calls,
                   COALESCE(AVG(total_ms), 0) AS avg_latency_ms,
                   COALESCE(AVG(ttft_ms), 0) AS avg_ttft_ms,
                   COALESCE(SUM(input_tokens), 0) AS total_input_tokens,
                   COALESCE(SUM(output_tokens), 0) AS total_output_tokens
            FROM llm_call_log WHERE created_at >= ?
        """, (cutoff,)).fetchone()
        total_calls = row["total_calls"]
        avg_latency = row["avg_latency_ms"]
        avg_ttft = row["avg_ttft_ms"]
        total_in = row["total_input_tokens"]
        total_out = row["total_output_tokens"]

        p99_latency = self._query_p99(cutoff)

        # 按模型聚合
        model_rows = conn.execute("""
            SELECT model, COUNT(*) AS calls,
                   COALESCE(SUM(input_tokens + output_tokens), 0) AS tokens,
                   COALESCE(AVG(total_ms), 0) AS avg_ms
            FROM llm_call_log WHERE created_at >= ?
            GROUP BY model ORDER BY calls DESC
        """, (cutoff,)).fetchall()
        by_model = {
            r["model"]: {"calls": r["calls"], "tokens": r["tokens"],
                         "avg_ms": round(r["avg_ms"], 1)}
            for r in model_rows
        }

        # 按状态聚合
        status_rows = conn.execute("""
            SELECT status, COUNT(*) AS cnt
            FROM llm_call_log WHERE created_at >= ?
            GROUP BY status
        """, (cutoff,)).fetchall()
        by_status = {r["status"]: r["cnt"] for r in status_rows}

        # 最近错误 Top5
        err_rows = conn.execute("""
            SELECT model, error, COUNT(*) AS cnt
            FROM llm_call_log
            WHERE created_at >= ? AND status != 'ok' AND error != ''
            GROUP BY model, error ORDER BY cnt DESC LIMIT 5
        """, (cutoff,)).fetchall()
        recent_errors = [{"model": r["model"], "error": r["error"][:200],
                          "count": r["cnt"]} for r in err_rows]

        return {
            "window_hours": hours,
            "total_calls": total_calls,
            "avg_latency_ms": round(avg_latency, 1),
            "p99_latency_ms": round(p99_latency, 1),
            "avg_ttft_ms": round(avg_ttft, 1),
            "total_input_tokens": int(total_in),
            "total_output_tokens": int(total_out),
            "by_model": by_model,
            "by_status": by_status,
            "recent_errors": recent_errors,
        }


    def _query_p99(self, cutoff: float) -> float:
        'Fetch latency percentile within the same thread-local connection.'
        conn = self.database._get_conn()
        # P99 延迟
        p99_row = conn.execute("""
            SELECT total_ms FROM llm_call_log
            WHERE created_at >= ?
            ORDER BY total_ms ASC
        """, (cutoff,)).fetchall()
        if p99_row:
            idx = min(int(len(p99_row) * 0.99), len(p99_row) - 1)
            p99_latency = p99_row[idx]["total_ms"]
        else:
            p99_latency = 0

        return p99_latency

    def query_llm_recent(self, limit: int = 50) -> list[dict]:
        """查询最近 N 条 LLM 调用明细 (供调试/排查)。"""
        conn = self.database._get_conn()
        rows = conn.execute("""
            SELECT id, call_type, model, input_tokens, output_tokens,
                   ttft_ms, total_ms, status, task_id, error, created_at
            FROM llm_call_log
            ORDER BY id DESC LIMIT ?
        """, (int(limit),)).fetchall()
        return [dict(r) for r in rows]

