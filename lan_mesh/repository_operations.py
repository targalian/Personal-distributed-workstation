"""Domain SQL repository; Database remains the sole connection and schema owner."""

from __future__ import annotations
import json
import time
from typing import Optional
from .logger import get_logger
from .repository_registry import Repository
logger = get_logger("database")

class OperationsRepository(Repository):
    """OperationsRepository over the Database owner; SQL and connection policy are preserved."""

    def save_optimization_item(self, item: dict) -> None:
        """Save one persistent workstation optimization item."""
        item_id = str(item.get("id", item.get("item_id", "")))
        if not item_id:
            raise ValueError("optimization item id is required")
        now = time.time()
        conn = self.database._get_conn()
        conn.execute("""
            INSERT INTO workstation_optimization_items (
                item_id, source, title, description, priority, status,
                decision_reply, shadow_run_id, result_json,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(item_id) DO UPDATE SET
                source=excluded.source, title=excluded.title,
                description=excluded.description, priority=excluded.priority,
                status=excluded.status, decision_reply=excluded.decision_reply,
                shadow_run_id=excluded.shadow_run_id,
                result_json=excluded.result_json, updated_at=excluded.updated_at
        """, (
            item_id, str(item.get("source", "agent")), str(item.get("title", "")),
            str(item.get("description", "")), str(item.get("priority", "normal")),
            str(item.get("status", "waiting_boss")), str(item.get("decision_reply", "")),
            str(item.get("shadow_run_id", "")),
            json.dumps(item.get("result", {}), ensure_ascii=False),
            float(item.get("created_at", now)), float(item.get("updated_at", now)),
        ))
        conn.commit()


    def get_optimization_item(self, item_id: str) -> Optional[dict]:
        """Return one workstation optimization item."""
        row = self.database._get_conn().execute(
            "SELECT * FROM workstation_optimization_items WHERE item_id = ?",
            (item_id,)).fetchone()
        return self.database._optimization_row_to_dict(row) if row else None


    def list_optimization_items(self, status: str = "",
                                limit: int = 100) -> list[dict]:
        """List workstation optimization items."""
        conn = self.database._get_conn()
        if status:
            rows = conn.execute("""
                SELECT * FROM workstation_optimization_items
                WHERE status = ? ORDER BY updated_at DESC LIMIT ?
            """, (status, limit)).fetchall()
        else:
            rows = conn.execute("""
                SELECT * FROM workstation_optimization_items
                ORDER BY updated_at DESC LIMIT ?
            """, (limit,)).fetchall()
        return [self.database._optimization_row_to_dict(row) for row in rows]


    def prune_logs(self, retention_days: float = 30.0) -> dict:
        """iter-54: 按保留期修剪日志表, 防止 DB 无限增长 (补强#2)。

        每表删除超过保留期的历史行; resource_usage_log 仅删已上报
        (reported=1) 的行, 未上报的保留给 R3 上报线程补报。
        返回各表删除行数统计。
        """
        cutoff = time.time() - float(retention_days) * 86400
        conn = self.database._get_conn()
        stats: dict = {}

        # 每次 LLM 调用一行, 增长最快; 审计查询最大窗口 7 天 (metrics 端点夹取)
        stats["llm_call_log"] = conn.execute(
            "DELETE FROM llm_call_log WHERE created_at < ?", (cutoff,)
        ).rowcount
        # 聊天记录 (role/content/timestamp)
        stats["chat_history"] = conn.execute(
            "DELETE FROM chat_history WHERE timestamp < ?", (cutoff,)
        ).rowcount
        # 用量日志: 只删已上报行, 未上报 (reported=0) 保留等离线补报
        stats["resource_usage_log"] = conn.execute(
            "DELETE FROM resource_usage_log WHERE created_at < ? AND reported = 1",
            (cutoff,),
        ).rowcount
        # PM 进度报告 (查询仅取最近 50 条)
        stats["progress_reports"] = conn.execute(
            "DELETE FROM progress_reports WHERE timestamp < ?", (cutoff,)
        ).rowcount
        # 心跳历史固定 24h 保留 (与 cleanup_old_heartbeats 同一语义)
        hb_cutoff = time.time() - 24 * 3600
        stats["heartbeat_log"] = conn.execute(
            "DELETE FROM heartbeat_log WHERE timestamp < ?", (hb_cutoff,)
        ).rowcount
        conn.commit()
        return stats


    def vacuum(self) -> None:
        """iter-54: VACUUM 回收删除后未释放的磁盘空间 (修剪周期内调用)。"""
        conn = self.database._get_conn()
        conn.commit()  # 提交本线程挂起事务, 避免 VACUUM 死锁
        conn.execute("VACUUM")


    def save_error_record(self, record: dict) -> None:
        """iter-47: 持久化一条错误记录到 error_log (error_tracker 回调调用)。

        容量上限 2000 行: 超出删除最旧行, 防止 DB 无限增长。
        """
        try:
            context_json = json.dumps(record.get("context") or {},
                                      ensure_ascii=False)
        except Exception:
            context_json = "{}"
        conn = self.database._get_conn()
        conn.execute("""
            INSERT INTO error_log
                (timestamp, module, error_type, message, context_json, traceback)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (float(record.get("timestamp") or time.time()),
              str(record.get("module", "")),
              str(record.get("error_type", "")),
              str(record.get("message", ""))[:500],
              context_json,
              str(record.get("traceback", ""))[:2000]))
        # 容量修剪: 只保留最近 2000 行 (与 INSERT 同事务提交)
        conn.execute("""
            DELETE FROM error_log WHERE id NOT IN (
                SELECT id FROM error_log ORDER BY id DESC LIMIT 2000
            )
        """)
        conn.commit()


    def query_error_history(self, limit: int = 100, module: str = "") -> list[dict]:
        """iter-47: 查询持久化错误记录 (按写入序倒序, 供 /api/errors/history)。"""
        limit = max(1, min(int(limit), 500))
        conn = self.database._get_conn()
        if module:
            rows = conn.execute("""
                SELECT id, timestamp, module, error_type, message, context_json
                FROM error_log WHERE module = ?
                ORDER BY id DESC LIMIT ?
            """, (module, limit)).fetchall()
        else:
            rows = conn.execute("""
                SELECT id, timestamp, module, error_type, message, context_json
                FROM error_log ORDER BY id DESC LIMIT ?
            """, (limit,)).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            raw = d.pop("context_json", "{}")
            try:
                d["context"] = json.loads(raw or "{}")
            except Exception:
                d["context"] = {}
            result.append(d)
        return result


    def save_heal_record(self, record: dict) -> None:
        """iter-49: 持久化一条自愈动作执行记录到 heal_log。

        result 取值: ok / failed / manual_required; detail 为执行摘要。
        容量上限 500 行: 超出删除最旧行。
        """
        conn = self.database._get_conn()
        conn.execute("""
            INSERT INTO heal_log
                (timestamp, category, action, result, detail)
            VALUES (?, ?, ?, ?, ?)
        """, (float(record.get("timestamp") or time.time()),
              str(record.get("category", "")),
              str(record.get("action", "")),
              str(record.get("result", "")),
              str(record.get("detail", ""))[:500]))
        # 容量修剪: 只保留最近 500 行 (与 INSERT 同事务提交)
        conn.execute("""
            DELETE FROM heal_log WHERE id NOT IN (
                SELECT id FROM heal_log ORDER BY id DESC LIMIT 500
            )
        """)
        conn.commit()


    def query_heal_history(self, limit: int = 50) -> list[dict]:
        """iter-49: 查询自愈执行历史 (按写入序倒序, 供 /api/errors/heal/history)。"""
        limit = max(1, min(int(limit), 200))
        conn = self.database._get_conn()
        rows = conn.execute("""
            SELECT id, timestamp, category, action, result, detail
            FROM heal_log ORDER BY id DESC LIMIT ?
        """, (limit,)).fetchall()
        return [dict(r) for r in rows]

