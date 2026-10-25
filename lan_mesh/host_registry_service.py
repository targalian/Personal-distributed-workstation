"""HostRegistryService: domain behavior separated from the public compatibility facade."""

from __future__ import annotations
from typing import Any
from . import station_director as policy
from .component_service import ComponentService

class HostRegistryService(ComponentService):
    """HostRegistryService business operations with explicit state ownership."""
    OPERATIONS = ('on_host_registered', 'on_heartbeat', 'prune_offline', 'get_fleet_summary', 'get_all_hosts', 'get_hosts_by_tier', 'get_host_events', 'get_host_detail', 'recompute_ratings', 'bind_discovery')

    def on_host_registered(self, info: policy.HostInfo) -> policy.HostRecord:
        """Worker 注册入站: 评级 + 记录事件 + 持久化。

        Args:
            info: Worker 发送的完整 HostInfo

        Returns:
            创建/更新的 HostRecord
        """
        with self.owner._lock:
            existing = self.owner.db.get_host(info.device_id)
            is_new = existing is None
            rating = policy.rate_host(info.cpu_count, info.memory_total_mb, info.disk_total_gb)
            record = policy.HostRecord(device_id=info.device_id, device_name=info.device_name, role=info.role, hostname=info.hostname, platform=info.platform, ip='', api_port=info.api_port, cpu_count=info.cpu_count, memory_total_mb=info.memory_total_mb, disk_total_gb=info.disk_total_gb, cpu_percent=info.cpu_percent, memory_percent=info.memory_percent, disk_percent=info.disk_percent, shared_folder=info.shared_folder, shared_file_count=info.shared_file_count, online=True, registered_at=existing.registered_at if existing else policy.time.time(), last_seen=policy.time.time(), rating_tier=rating.tier, rating_score=rating.score, rating_summary=rating.summary, code_version=getattr(info, 'code_version', ''), version_ts=getattr(info, 'version_ts', 0.0))
            ip = ''
            if self.owner.discovery:
                dev = self.owner.discovery.find_device(info.device_id)
                if dev:
                    ip = dev.get('ip', '')
            if not ip and getattr(info, 'ip_addresses', None):
                ip = info.ip_addresses[0] if isinstance(info.ip_addresses, list) else str(info.ip_addresses)
            record.ip = ip
            self.owner.db.upsert_host(record)
            if is_new:
                self.owner.db.log_host_event(info.device_id, 'join', f'{info.hostname} ({info.platform}) {rating.tier}级')
                policy.logger.info('新主机入站: %s [%s级] %d核/%dGB', info.device_name, rating.tier, info.cpu_count, info.memory_total_mb // 1024)
            else:
                self.owner.db.log_host_event(info.device_id, 'register', '重新注册')
                if existing and existing.rating_tier != rating.tier:
                    policy.logger.info('主机评级变更: %s %s->%s', info.device_name, existing.rating_tier, rating.tier)
            return record

    def on_heartbeat(self, device_id: str, metrics: dict) -> policy.Optional[policy.HostRecord]:
        """心跳处理: 更新实时指标。

        Args:
            device_id: 设备 ID
            metrics: 实时指标 {cpu_percent, memory_percent, disk_percent, shared_file_count}

        Returns:
            更新后的 HostRecord, 设备不存在返回 None
        """
        with self.owner._lock:
            record = self.owner.db.get_host(device_id)
            if not record:
                return None
            record.cpu_percent = metrics.get('cpu_percent', record.cpu_percent)
            record.memory_percent = metrics.get('memory_percent', record.memory_percent)
            record.disk_percent = metrics.get('disk_percent', record.disk_percent)
            record.shared_file_count = metrics.get('shared_file_count', record.shared_file_count)
            record.online = True
            record.last_seen = policy.time.time()
            if metrics.get('code_version'):
                record.code_version = metrics['code_version']
            if metrics.get('version_ts'):
                record.version_ts = metrics['version_ts']
            if metrics.get('role'):
                record.role = metrics['role']
            if self.owner.discovery:
                dev = self.owner.discovery.find_device(device_id)
                if dev:
                    new_ip = dev.get('ip', '')
                    if new_ip:
                        record.ip = new_ip
            if not record.ip and metrics.get('ip'):
                record.ip = metrics['ip']
            self.owner.db.upsert_host(record)
            self.owner.db.log_heartbeat(device_id, record.cpu_percent, record.memory_percent, record.disk_percent)
            return record

    def prune_offline(self, ttl: float) -> list[str]:
        """清理超时离线主机并记录离线事件。返回被清理的 device_id 列表。"""
        gone_ids = self.owner.db.prune_offline(ttl)
        for device_id in gone_ids:
            self.owner.db.log_host_event(device_id, 'leave', '超时未心跳, 标记离线')
            record = self.owner.db.get_host(device_id)
            name = record.device_name if record else device_id[:8]
            policy.logger.info('主机离线: %s', name)
        return gone_ids

    def get_fleet_summary(self) -> dict:
        """返回主机舰队概览: 在线/离线/各评级分布。"""
        stats = self.owner.db.get_host_stats()
        return {'total': stats['total'], 'online': stats['online'], 'offline': stats['offline'], 'tiers': stats['tiers']}

    def get_all_hosts(self) -> list[dict]:
        """返回所有主机列表 (含评级+状态)。"""
        hosts = self.owner.db.list_hosts()
        return [h.to_dict() for h in hosts]

    def get_hosts_by_tier(self, min_tier: str='D', online_only: bool=True) -> list[dict]:
        """按评级筛选主机 (供 Secretary/Planner 调用)。

        Args:
            min_tier: 最低评级要求 (S/A/B/C/D)
            online_only: 是否仅返回在线主机

        Returns:
            满足条件的主机列表
        """
        hosts = self.owner.db.list_hosts()
        min_order = policy.TIER_ORDER.get(min_tier, 99)
        result = []
        for h in hosts:
            if online_only and (not h.online):
                continue
            host_order = policy.TIER_ORDER.get(h.rating_tier, 99)
            if host_order <= min_order:
                result.append(h.to_dict())
        return result

    def get_host_events(self, device_id: str=None, limit: int=50) -> list[dict]:
        """查询出入站事件历史。"""
        return self.owner.db.get_host_events(device_id, limit)

    def get_host_detail(self, device_id: str) -> policy.Optional[dict]:
        """查询单台主机详情 (含事件历史)。"""
        record = self.owner.db.get_host(device_id)
        if not record:
            return None
        events = self.owner.db.get_host_events(device_id, limit=20)
        detail = record.to_dict()
        detail['events'] = events
        return detail

    def recompute_ratings(self) -> Any:
        """重算所有在线主机的评级 (配置变更或手动触发)。"""
        with self.owner._lock:
            hosts = self.owner.db.list_hosts()
            updated = 0
            for h in hosts:
                rating = policy.rate_host(h.cpu_count, h.memory_total_mb, h.disk_total_gb)
                if rating.tier != h.rating_tier or rating.score != h.rating_score:
                    self.owner.db.update_host_rating(h.device_id, rating.tier, rating.score, rating.summary)
                    updated += 1
            policy.logger.info('评级重算完成: %d/%d 台主机评级变更', updated, len(hosts))
            return updated

    def bind_discovery(self, discovery: policy.DiscoveryService) -> Any:
        """绑定发现服务 (延迟绑定, 因为 discovery 创建时可能还未初始化)。"""
        self.owner.discovery = discovery
