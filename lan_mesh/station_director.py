"""
Station Director (工作站主管) — 基础设施资源管理器

职责:
1. 主机注册入站: 评级 + 记录事件 + 持久化
2. 心跳处理: 更新实时指标, 资源变化时重新评级
3. 离线检测: 记录离线事件
4. 舰队管理: 主机列表/统计/事件历史
5. 资源池查询: 按评级筛选在线主机 (供 Secretary/Planner 调用)

设计原则:
- Station Director 只管"机器", 不管"项目"
- Secretary 通过 Station Director 获取资源池信息
- 未来 Planner 根据任务难度查询 Station Director 获取匹配主机
"""
import time
import threading
from typing import Optional

from .database import Database
from .discovery import DiscoveryService
from .host_info import collect_host_info
from .host_rating import rate_host, HostRating, TIER_ORDER
from .protocol import HostInfo, HostRecord
from .shared_folder import SharedFolderManager
from .logger import get_logger

logger = get_logger("station_director")


class StationDirector:
    """工作站主管 — 管理主机出入站、评级、资源池。"""

    def __init__(self, db: Database, discovery: Optional[DiscoveryService] = None,
                 shared_folder: Optional[SharedFolderManager] = None):
        self.db = db
        self.discovery = discovery
        self.shared_folder = shared_folder
        self._lock = threading.Lock()

    # ── 主机注册 ─────────────────────────────────────────────────

    def on_host_registered(self, info: HostInfo) -> HostRecord:
        """Worker 注册入站: 评级 + 记录事件 + 持久化。

        Args:
            info: Worker 发送的完整 HostInfo

        Returns:
            创建/更新的 HostRecord
        """
        with self._lock:
            # 检查是否首次入站
            existing = self.db.get_host(info.device_id)
            is_new = existing is None

            # 计算评级
            rating = rate_host(info.cpu_count, info.memory_total_mb, info.disk_total_gb)

            # 构建记录
            record = HostRecord(
                device_id=info.device_id,
                device_name=info.device_name,
                role=info.role,
                hostname=info.hostname,
                platform=info.platform,
                ip="",
                api_port=info.api_port,
                cpu_count=info.cpu_count,
                memory_total_mb=info.memory_total_mb,
                disk_total_gb=info.disk_total_gb,
                cpu_percent=info.cpu_percent,
                memory_percent=info.memory_percent,
                disk_percent=info.disk_percent,
                shared_folder=info.shared_folder,
                shared_file_count=info.shared_file_count,
                online=True,
                registered_at=existing.registered_at if existing else time.time(),
                last_seen=time.time(),
                rating_tier=rating.tier,
                rating_score=rating.score,
                rating_summary=rating.summary,
                code_version=getattr(info, "code_version", ""),
                version_ts=getattr(info, "version_ts", 0.0),
            )

            # 获取真实 IP: 优先 UDP 发现列表, 其次 HostInfo.ip_addresses
            ip = ""
            if self.discovery:
                dev = self.discovery.find_device(info.device_id)
                if dev:
                    ip = dev.get("ip", "")
            if not ip and getattr(info, "ip_addresses", None):
                ip = info.ip_addresses[0] if isinstance(info.ip_addresses, list) else str(info.ip_addresses)
            record.ip = ip

            # 持久化
            self.db.upsert_host(record)

            # 记录事件
            if is_new:
                self.db.log_host_event(info.device_id, "join",
                                       f"{info.hostname} ({info.platform}) {rating.tier}级")
                logger.info("新主机入站: %s [%s级] %d核/%dGB",
                            info.device_name, rating.tier,
                            info.cpu_count, info.memory_total_mb // 1024)
            else:
                self.db.log_host_event(info.device_id, "register", "重新注册")
                # 检查评级变化
                if existing and existing.rating_tier != rating.tier:
                    logger.info("主机评级变更: %s %s->%s",
                                info.device_name, existing.rating_tier, rating.tier)

            return record

    # ── 心跳处理 ─────────────────────────────────────────────────

    def on_heartbeat(self, device_id: str, metrics: dict) -> Optional[HostRecord]:
        """心跳处理: 更新实时指标。

        Args:
            device_id: 设备 ID
            metrics: 实时指标 {cpu_percent, memory_percent, disk_percent, shared_file_count}

        Returns:
            更新后的 HostRecord, 设备不存在返回 None
        """
        with self._lock:
            record = self.db.get_host(device_id)
            if not record:
                return None

            record.cpu_percent = metrics.get("cpu_percent", record.cpu_percent)
            record.memory_percent = metrics.get("memory_percent", record.memory_percent)
            record.disk_percent = metrics.get("disk_percent", record.disk_percent)
            record.shared_file_count = metrics.get("shared_file_count", record.shared_file_count)
            record.online = True
            record.last_seen = time.time()

            # S2/S3: 心跳携带代码版本时同步落库 (跨主机版本统计)
            if metrics.get("code_version"):
                record.code_version = metrics["code_version"]
            if metrics.get("version_ts"):
                record.version_ts = metrics["version_ts"]

            # 更新 IP: 优先 UDP 发现, 其次 metrics 传入的 ip
            if self.discovery:
                dev = self.discovery.find_device(device_id)
                if dev:
                    new_ip = dev.get("ip", "")
                    if new_ip:
                        record.ip = new_ip
            if not record.ip and metrics.get("ip"):
                record.ip = metrics["ip"]

            self.db.upsert_host(record)
            self.db.log_heartbeat(device_id, record.cpu_percent,
                                  record.memory_percent, record.disk_percent)
            return record

    # ── 离线检测 ─────────────────────────────────────────────────

    def prune_offline(self, ttl: float) -> list[str]:
        """清理超时离线主机并记录离线事件。返回被清理的 device_id 列表。"""
        gone_ids = self.db.prune_offline(ttl)
        for device_id in gone_ids:
            self.db.log_host_event(device_id, "leave", "超时未心跳, 标记离线")
            record = self.db.get_host(device_id)
            name = record.device_name if record else device_id[:8]
            logger.info("主机离线: %s", name)
        return gone_ids

    # ── 舰队查询 ─────────────────────────────────────────────────

    def get_fleet_summary(self) -> dict:
        """返回主机舰队概览: 在线/离线/各评级分布。"""
        stats = self.db.get_host_stats()
        return {
            "total": stats["total"],
            "online": stats["online"],
            "offline": stats["offline"],
            "tiers": stats["tiers"],
        }

    def get_all_hosts(self) -> list[dict]:
        """返回所有主机列表 (含评级+状态)。"""
        hosts = self.db.list_hosts()
        return [h.to_dict() for h in hosts]

    def get_hosts_by_tier(self, min_tier: str = "D", online_only: bool = True) -> list[dict]:
        """按评级筛选主机 (供 Secretary/Planner 调用)。

        Args:
            min_tier: 最低评级要求 (S/A/B/C/D)
            online_only: 是否仅返回在线主机

        Returns:
            满足条件的主机列表
        """
        hosts = self.db.list_hosts()
        min_order = TIER_ORDER.get(min_tier, 99)
        result = []
        for h in hosts:
            if online_only and not h.online:
                continue
            host_order = TIER_ORDER.get(h.rating_tier, 99)
            if host_order <= min_order:
                result.append(h.to_dict())
        return result

    def get_host_events(self, device_id: str = None, limit: int = 50) -> list[dict]:
        """查询出入站事件历史。"""
        return self.db.get_host_events(device_id, limit)

    def get_host_detail(self, device_id: str) -> Optional[dict]:
        """查询单台主机详情 (含事件历史)。"""
        record = self.db.get_host(device_id)
        if not record:
            return None
        events = self.db.get_host_events(device_id, limit=20)
        detail = record.to_dict()
        detail["events"] = events
        return detail

    # ── 评级管理 ─────────────────────────────────────────────────

    def recompute_ratings(self):
        """重算所有在线主机的评级 (配置变更或手动触发)。"""
        with self._lock:
            hosts = self.db.list_hosts()
            updated = 0
            for h in hosts:
                rating = rate_host(h.cpu_count, h.memory_total_mb, h.disk_total_gb)
                if rating.tier != h.rating_tier or rating.score != h.rating_score:
                    self.db.update_host_rating(
                        h.device_id, rating.tier, rating.score, rating.summary
                    )
                    updated += 1
            logger.info("评级重算完成: %d/%d 台主机评级变更", updated, len(hosts))
            return updated

    def bind_discovery(self, discovery: DiscoveryService):
        """绑定发现服务 (延迟绑定, 因为 discovery 创建时可能还未初始化)。"""
        self.discovery = discovery
