"""Domain SQL repository; Database remains the sole connection and schema owner."""

from __future__ import annotations
import time
from typing import Any, Optional
from .protocol import HostRecord
from .logger import get_logger
from .repository_registry import Repository
logger = get_logger("database")

class HostsRepository(Repository):
    """HostsRepository over the Database owner; SQL and connection policy are preserved."""

    def upsert_host(self, record: HostRecord) -> Any:
        """插入或更新主机记录。"""
        conn = self.database._get_conn()
        conn.execute("""
            INSERT INTO hosts (
                device_id, device_name, role, hostname, platform,
                ip, api_port, cpu_count, memory_total_mb, disk_total_gb,
                cpu_percent, memory_percent, disk_percent,
                shared_folder, shared_file_count, online,
                registered_at, last_seen, latency_ms,
                rating_tier, rating_score, rating_summary,
                code_version, version_ts, source, federation
            ) VALUES (
                ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?,
                ?, ?, ?,
                ?, ?, ?,
                ?, ?, ?,
                ?, ?, ?,
                ?, ?, ?, ?
            )
            ON CONFLICT(device_id) DO UPDATE SET
                device_name=excluded.device_name,
                role=excluded.role,
                hostname=excluded.hostname,
                platform=excluded.platform,
                ip=excluded.ip,
                api_port=excluded.api_port,
                cpu_count=excluded.cpu_count,
                memory_total_mb=excluded.memory_total_mb,
                disk_total_gb=excluded.disk_total_gb,
                cpu_percent=excluded.cpu_percent,
                memory_percent=excluded.memory_percent,
                disk_percent=excluded.disk_percent,
                shared_folder=excluded.shared_folder,
                shared_file_count=excluded.shared_file_count,
                online=excluded.online,
                last_seen=excluded.last_seen,
                latency_ms=excluded.latency_ms,
                rating_tier=excluded.rating_tier,
                rating_score=excluded.rating_score,
                rating_summary=excluded.rating_summary,
                code_version=excluded.code_version,
                version_ts=excluded.version_ts,
                source=excluded.source,
                federation=excluded.federation
        """, (
            record.device_id, record.device_name, record.role,
            record.hostname, record.platform,
            record.ip, record.api_port,
            record.cpu_count, record.memory_total_mb, record.disk_total_gb,
            record.cpu_percent, record.memory_percent, record.disk_percent,
            record.shared_folder, record.shared_file_count,
            1 if record.online else 0,
            record.registered_at, record.last_seen, record.latency_ms,
            record.rating_tier, record.rating_score, record.rating_summary,
            record.code_version, record.version_ts,
            record.source, record.federation,
        ))
        conn.commit()


    def log_heartbeat(self, device_id: str, cpu: float, mem: float, disk: float) -> Any:
        """记录心跳历史。"""
        conn = self.database._get_conn()
        conn.execute("""
            INSERT INTO heartbeat_log (device_id, timestamp, cpu_percent, memory_percent, disk_percent)
            VALUES (?, ?, ?, ?, ?)
        """, (device_id, time.time(), cpu, mem, disk))
        conn.commit()


    def get_host(self, device_id: str) -> Optional[HostRecord]:
        """查询单台主机记录。"""
        conn = self.database._get_conn()
        row = conn.execute(
            "SELECT * FROM hosts WHERE device_id = ?", (device_id,)
        ).fetchone()
        if row:
            return self.database._row_to_host(row)
        return None


    def list_hosts(self, source: Optional[str] = None) -> list[HostRecord]:
        """列出所有主机记录 (source 可选: 'lan' 本网段 / 'fed' 联邦远端)。"""
        conn = self.database._get_conn()
        if source:
            rows = conn.execute(
                "SELECT * FROM hosts WHERE source = ? "
                "ORDER BY online DESC, device_name ASC", (source,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM hosts ORDER BY online DESC, device_name ASC"
            ).fetchall()
        return [self.database._row_to_host(r) for r in rows]


    def set_offline(self, device_id: str) -> Any:
        """标记主机为离线。"""
        conn = self.database._get_conn()
        conn.execute(
            "UPDATE hosts SET online = 0 WHERE device_id = ?", (device_id,)
        )
        conn.commit()


    def prune_offline(self, ttl: float) -> list[str]:
        """将超过 TTL 未活跃的主机标记为离线。返回新标记离线的主机 ID 列表。"""
        conn = self.database._get_conn()
        cutoff = time.time() - ttl
        # 先查出即将离线的设备
        rows = conn.execute(
            "SELECT device_id FROM hosts WHERE last_seen < ? AND online = 1",
            (cutoff,),
        ).fetchall()
        gone_ids = [r["device_id"] for r in rows]
        conn.execute(
            "UPDATE hosts SET online = 0 WHERE last_seen < ? AND online = 1",
            (cutoff,),
        )
        conn.commit()
        return gone_ids


    def cleanup_old_heartbeats(self, max_age_hours: int = 24) -> Any:
        """清理超过指定时长的心跳历史。"""
        conn = self.database._get_conn()
        cutoff = time.time() - max_age_hours * 3600
        conn.execute(
            "DELETE FROM heartbeat_log WHERE timestamp < ?", (cutoff,)
        )
        conn.commit()


    def log_host_event(self, device_id: str, event_type: str, detail: str = "") -> Any:
        """记录主机事件 (join/leave/register/rating_change)。"""
        conn = self.database._get_conn()
        conn.execute(
            "INSERT INTO host_events (device_id, event_type, timestamp, detail) VALUES (?, ?, ?, ?)",
            (device_id, event_type, time.time(), detail),
        )
        conn.commit()


    def get_host_events(self, device_id: str = None, limit: int = 50) -> list[dict]:
        """查询主机事件历史。device_id=None 时返回全站事件。"""
        conn = self.database._get_conn()
        if device_id:
            rows = conn.execute(
                "SELECT * FROM host_events WHERE device_id = ? ORDER BY timestamp DESC LIMIT ?",
                (device_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM host_events ORDER BY timestamp DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]


    def update_host_rating(self, device_id: str, tier: str, score: int, summary: str) -> Any:
        """更新主机评级。"""
        conn = self.database._get_conn()
        old = conn.execute(
            "SELECT rating_tier FROM hosts WHERE device_id = ?", (device_id,)
        ).fetchone()
        conn.execute(
            "UPDATE hosts SET rating_tier = ?, rating_score = ?, rating_summary = ? WHERE device_id = ?",
            (tier, score, summary, device_id),
        )
        conn.commit()
        # 记录评级变更事件
        old_tier = old["rating_tier"] if old else ""
        if old_tier and old_tier != tier:
            self.database.log_host_event(device_id, "rating_change", f"{old_tier}->{tier}")


    def get_host_stats(self) -> dict:
        """返回主机舰队统计摘要。"""
        conn = self.database._get_conn()
        total = conn.execute("SELECT COUNT(*) as c FROM hosts").fetchone()["c"]
        online = conn.execute("SELECT COUNT(*) as c FROM hosts WHERE online = 1").fetchone()["c"]
        offline = total - online
        tiers = {}
        for t in ("S", "A", "B", "C", "D"):
            count = conn.execute(
                "SELECT COUNT(*) as c FROM hosts WHERE rating_tier = ? AND online = 1", (t,)
            ).fetchone()["c"]
            tiers[t] = count
        return {
            "total": total,
            "online": online,
            "offline": offline,
            "tiers": tiers,
        }

