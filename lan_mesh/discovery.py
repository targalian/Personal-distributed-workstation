"""
UDP 广播局域网设备发现 - 参考 QuickLAN 的 DiscoveryService

核心机制:
1. 定期 UDP 广播 presence 包 (携带设备身份+配置摘要)
2. 监听 UDP 广播,收到其他设备包时更新设备列表
3. TTL 超时清理: 超过 DEVICE_TTL_SECS 未收到的设备标记为离线

设计要点 (与 QuickLAN 的异同):
- 相同: UDP 广播发现、JSON 序列化、TTL 过期清理、子网广播地址计算
- 不同: 采用 Secretary/Worker 架构而非纯 P2P;发现包携带硬件配置摘要
"""
import json
import socket
import struct
import threading
import time
from dataclasses import asdict
from typing import Callable, Optional

from .host_info import get_local_ipv4_addresses, get_broadcast_targets
from .logger import get_logger
from .protocol import (
    APP_NAME,
    PROTOCOL_VERSION,
    DISCOVERY_PORT,
    PRESENCE_INTERVAL_SECS,
    DEVICE_TTL_SECS,
    DiscoveryPacket,
    NetworkStatus,
)

logger = get_logger("discovery")


from typing import Any

class DiscoveryService:
    """UDP 广播发现服务。

    在后台线程中运行三个循环:
    - presence_loop: 定期广播自身存在
    - listen_loop: 监听其他设备的 UDP 包
    - prune_loop: 定期清理超时离线设备
    """

    def __init__(
        self,
        device_id: str,
        device_name: str,
        role: str,
        api_port: int,
        make_packet: Callable[[], DiscoveryPacket],
        on_device_seen: Optional[Callable[[DiscoveryPacket, str], None]] = None,
        discovery_port: int = DISCOVERY_PORT,
        presence_interval: int = PRESENCE_INTERVAL_SECS,
        device_ttl: int = DEVICE_TTL_SECS,
    ):
        self.device_id = device_id
        self.device_name = device_name
        self.role = role
        self.api_port = api_port
        self.discovery_port = discovery_port
        self.presence_interval = presence_interval
        self.device_ttl = device_ttl
        self._make_packet = make_packet
        self._on_device_seen = on_device_seen

        self._devices: dict[str, dict] = {}   # device_id -> {packet, ip, last_seen}
        self._lock = threading.RLock()
        self._running = False
        self._threads: list[threading.Thread] = []

    # ── 生命周期 ─────────────────────────────────────────────────

    def start(self) -> None:
        """启动发现服务的三个后台线程。"""
        if self._running:
            return
        if any(thread.is_alive() for thread in self._threads):
            raise RuntimeError('Discovery threads are still stopping')
        from .application_lifecycle import stop_signal
        stop_signal(self).clear()
        self._threads.clear()
        self._running = True

        for target, name in [
            (self._presence_loop, "discovery-presence"),
            (self._listen_loop, "discovery-listen"),
            (self._prune_loop, "discovery-prune"),
        ]:
            t = threading.Thread(target=target, name=name, daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self) -> None:
        """停止发现服务。"""
        self._running = False
        from .application_lifecycle import stop_signal, join_threads
        stop_signal(self).set()
        sock = vars(self).get('_socket')
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        join_threads(self._threads)

    def broadcast_now(self) -> Any:
        return call_component(self, DiscoveryTransportService, 'broadcast_now')

    # ── 设备查询 ─────────────────────────────────────────────────

    def list_devices(self) -> list[dict]:
        return call_component(self, DiscoveryRegistryService, 'list_devices')

    def find_device(self, device_id: str) -> Optional[dict]:
        return call_component(self, DiscoveryRegistryService, 'find_device', device_id=device_id)

    def network_status(self) -> NetworkStatus:
        return call_component(self, DiscoveryRegistryService, 'network_status')

    # ── 后台循环 ─────────────────────────────────────────────────

    def _presence_loop(self) -> Any:
        return call_component(self, DiscoveryTransportService, '_presence_loop')

    def _listen_loop(self) -> Any:
        return call_component(self, DiscoveryTransportService, '_listen_loop')

    def _prune_loop(self) -> Any:
        return call_component(self, DiscoveryTransportService, '_prune_loop')

    # ── UDP 发送 ────────────────────────────────────────────────

    def _broadcast_packet(self, packet: DiscoveryPacket) -> Any:
        return call_component(self, DiscoveryTransportService, '_broadcast_packet', packet=packet)

    def _send_packet(self, packet: DiscoveryPacket, addr: tuple) -> Any:
        return call_component(self, DiscoveryTransportService, '_send_packet', packet=packet, addr=addr)

    def probe_ip(self, ip: str) -> Any:
        return call_component(self, DiscoveryTransportService, 'probe_ip', ip=ip)


from typing import Any
from .component_service import call_component
from .discovery_registry_service import DiscoveryRegistryService
from .discovery_transport_service import DiscoveryTransportService
