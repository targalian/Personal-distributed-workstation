"""DiscoveryRegistryService: domain behavior separated from the public compatibility facade."""

from __future__ import annotations
from . import discovery as policy
from .component_service import ComponentService

class DiscoveryRegistryService(ComponentService):
    """DiscoveryRegistryService business operations with explicit state ownership."""
    OPERATIONS = ('list_devices', 'find_device', 'network_status')

    def list_devices(self) -> list[dict]:
        """返回当前已知设备的快照列表。"""
        with self.owner._lock:
            now = policy.time.time()
            devices = []
            for entry in self.owner._devices.values():
                pkt = entry['packet']
                age = now - entry['last_seen']
                devices.append({**pkt.to_dict(), 'ip': entry['ip'], 'online': age < self.owner.device_ttl, 'last_seen_ago': round(age, 1)})
            devices.sort(key=lambda d: (not d['online'], d.get('device_name', '')))
            return devices

    def find_device(self, device_id: str) -> policy.Optional[dict]:
        """根据 device_id 查找设备。"""
        with self.owner._lock:
            entry = self.owner._devices.get(device_id)
            if entry:
                pkt = entry['packet']
                return {**pkt.to_dict(), 'ip': entry['ip'], 'online': policy.time.time() - entry['last_seen'] < self.owner.device_ttl}
            return None

    def network_status(self) -> policy.NetworkStatus:
        """返回本机网络状态。"""
        return policy.NetworkStatus(udp_port=self.owner.discovery_port, api_port=self.owner.api_port, local_ips=policy.get_local_ipv4_addresses(), broadcast_targets=policy.get_broadcast_targets())
