"""DiscoveryTransportService: domain behavior separated from the public compatibility facade."""

from __future__ import annotations
from .application_lifecycle import wait_for_stop
from typing import Any
from . import discovery as policy
from .component_service import ComponentService

class DiscoveryTransportService(ComponentService):
    """DiscoveryTransportService business operations with explicit state ownership."""
    OPERATIONS = ('broadcast_now', '_presence_loop', '_listen_loop', '_prune_loop', '_broadcast_packet', '_send_packet', 'probe_ip')

    def broadcast_now(self) -> Any:
        """立即发送一次广播包。"""
        packet = self.owner._make_packet()
        self.owner._broadcast_packet(packet)

    def _presence_loop(self) -> Any:
        """定期 UDP 广播自身存在 (参考 QuickLAN start_presence_loop)。"""
        self.owner.broadcast_now()
        while self.owner._running:
            if wait_for_stop(self.owner, self.owner.presence_interval):
                return
            self.owner.broadcast_now()

    def _listen_loop(self) -> Any:
        """监听 UDP 广播 (参考 QuickLAN start_listen_loop)。

        收到其他设备的包时:
        1. 回送一个 presence 包让对方也知道我们
        2. 更新设备列表
        3. 触发 on_device_seen 回调
        """
        sock = policy.socket.socket(policy.socket.AF_INET, policy.socket.SOCK_DGRAM)
        self.owner._socket = sock
        sock.setsockopt(policy.socket.SOL_SOCKET, policy.socket.SO_REUSEADDR, 1)
        sock.setsockopt(policy.socket.SOL_SOCKET, policy.socket.SO_BROADCAST, 1)
        try:
            sock.bind(('', self.owner.discovery_port))
        except OSError as e:
            policy.logger.error('UDP 绑定端口 %d 失败: %s', self.owner.discovery_port, e)
            reuse_port = getattr(policy.socket, 'SO_REUSEPORT', None)
            if reuse_port is not None:
                try:
                    sock.setsockopt(policy.socket.SOL_SOCKET, reuse_port, 1)
                    sock.bind(('', self.owner.discovery_port))
                except OSError:
                    policy.logger.warning('端口 %d 仍被占用, 发现服务降级运行', self.owner.discovery_port)
                    return
            else:
                policy.logger.warning('端口 %d 仍被占用, 发现服务降级运行', self.owner.discovery_port)
                return
        sock.settimeout(2.0)
        while self.owner._running:
            try:
                data, addr = sock.recvfrom(8192)
                raw = policy.json.loads(data.decode('utf-8'))
                packet = policy.DiscoveryPacket.from_dict(raw)
                if not packet.is_lanmesh():
                    continue
                if packet.device_id == self.owner.device_id:
                    continue
                self.owner._send_packet(self.owner._make_packet(), addr)
                with self.owner._lock:
                    self.owner._devices[packet.device_id] = {'packet': packet, 'ip': addr[0], 'last_seen': policy.time.time()}
                if self.owner._on_device_seen:
                    try:
                        self.owner._on_device_seen(packet, addr[0])
                    except Exception as e:
                        policy.logger.error('on_device_seen 回调异常: %s', e)
            except policy.socket.timeout:
                continue
            except policy.json.JSONDecodeError:
                continue
            except OSError:
                continue
        sock.close()

    def _prune_loop(self) -> Any:
        """定期清理超时设备 (参考 QuickLAN start_prune_loop)。"""
        while self.owner._running:
            if wait_for_stop(self.owner, 5):
                return
            now = policy.time.time()
            with self.owner._lock:
                expired = [did for did, entry in self.owner._devices.items() if now - entry['last_seen'] > self.owner.device_ttl * 3]
                for did in expired:
                    del self.owner._devices[did]

    def _broadcast_packet(self, packet: policy.DiscoveryPacket) -> Any:
        """向所有子网广播地址发送 UDP 包 (参考 QuickLAN broadcast_packet)。"""
        sock = policy.socket.socket(policy.socket.AF_INET, policy.socket.SOCK_DGRAM)
        sock.setsockopt(policy.socket.SOL_SOCKET, policy.socket.SO_BROADCAST, 1)
        payload = policy.json.dumps(packet.to_dict()).encode('utf-8')
        for target in policy.get_broadcast_targets():
            try:
                sock.sendto(payload, (target, self.owner.discovery_port))
            except OSError:
                continue
        sock.close()

    def _send_packet(self, packet: policy.DiscoveryPacket, addr: tuple) -> Any:
        """单播发送 UDP 包到指定地址 (参考 QuickLAN send_discovery_packet)。"""
        sock = policy.socket.socket(policy.socket.AF_INET, policy.socket.SOCK_DGRAM)
        sock.setsockopt(policy.socket.SOL_SOCKET, policy.socket.SO_BROADCAST, 1)
        payload = policy.json.dumps(packet.to_dict()).encode('utf-8')
        try:
            sock.sendto(payload, addr)
        except OSError:
            pass
        sock.close()

    def probe_ip(self, ip: str) -> Any:
        """主动探测指定 IP 的设备 (参考 QuickLAN probe_ip)。"""
        packet = self.owner._make_packet()
        self.owner._send_packet(packet, (ip, self.owner.discovery_port))
