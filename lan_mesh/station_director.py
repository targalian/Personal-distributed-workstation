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


from typing import Any

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
        return call_component(self, HostRegistryService, 'on_host_registered', info=info)

    # ── 心跳处理 ─────────────────────────────────────────────────

    def on_heartbeat(self, device_id: str, metrics: dict) -> Optional[HostRecord]:
        return call_component(self, HostRegistryService, 'on_heartbeat', device_id=device_id, metrics=metrics)

    # ── 离线检测 ─────────────────────────────────────────────────

    def prune_offline(self, ttl: float) -> list[str]:
        return call_component(self, HostRegistryService, 'prune_offline', ttl=ttl)

    # ── 舰队查询 ─────────────────────────────────────────────────

    def get_fleet_summary(self) -> dict:
        return call_component(self, HostRegistryService, 'get_fleet_summary')

    def get_all_hosts(self) -> list[dict]:
        return call_component(self, HostRegistryService, 'get_all_hosts')

    def get_hosts_by_tier(self, min_tier: str='D', online_only: bool=True) -> list[dict]:
        return call_component(self, HostRegistryService, 'get_hosts_by_tier', min_tier=min_tier, online_only=online_only)

    def get_host_events(self, device_id: str=None, limit: int=50) -> list[dict]:
        return call_component(self, HostRegistryService, 'get_host_events', device_id=device_id, limit=limit)

    def get_host_detail(self, device_id: str) -> Optional[dict]:
        return call_component(self, HostRegistryService, 'get_host_detail', device_id=device_id)

    # ── 评级管理 ─────────────────────────────────────────────────

    def recompute_ratings(self) -> Any:
        return call_component(self, HostRegistryService, 'recompute_ratings')

    def bind_discovery(self, discovery: DiscoveryService) -> Any:
        return call_component(self, HostRegistryService, 'bind_discovery', discovery=discovery)


from typing import Any
from .component_service import call_component
from .host_registry_service import HostRegistryService
