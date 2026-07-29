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

    def start(self):
        """启动发现服务的三个后台线程。"""
        if self._running:
            return
        self._running = True

        for target, name in [
            (self._presence_loop, "discovery-presence"),
            (self._listen_loop, "discovery-listen"),
            (self._prune_loop, "discovery-prune"),
        ]:
            t = threading.Thread(target=target, name=name, daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self):
        """停止发现服务。"""
        self._running = False

    def broadcast_now(self):
        """立即发送一次广播包。"""
        packet = self._make_packet()
        self._broadcast_packet(packet)

    # ── 设备查询 ─────────────────────────────────────────────────

    def list_devices(self) -> list[dict]:
        """返回当前已知设备的快照列表。"""
        with self._lock:
            now = time.time()
            devices = []
            for entry in self._devices.values():
                pkt = entry["packet"]
                age = now - entry["last_seen"]
                devices.append({
                    **pkt.to_dict(),
                    "ip": entry["ip"],
                    "online": age < self.device_ttl,
                    "last_seen_ago": round(age, 1),
                })
            # 在线优先,然后按名称排序
            devices.sort(key=lambda d: (not d["online"], d.get("device_name", "")))
            return devices

    def find_device(self, device_id: str) -> Optional[dict]:
        """根据 device_id 查找设备。"""
        with self._lock:
            entry = self._devices.get(device_id)
            if entry:
                pkt = entry["packet"]
                return {
                    **pkt.to_dict(),
                    "ip": entry["ip"],
                    "online": (time.time() - entry["last_seen"]) < self.device_ttl,
                }
            return None

    def network_status(self) -> NetworkStatus:
        """返回本机网络状态。"""
        return NetworkStatus(
            udp_port=self.discovery_port,
            api_port=self.api_port,
            local_ips=get_local_ipv4_addresses(),
            broadcast_targets=get_broadcast_targets(),
        )

    # ── 后台循环 ─────────────────────────────────────────────────

    def _presence_loop(self):
        """定期 UDP 广播自身存在 (参考 QuickLAN start_presence_loop)。"""
        # 首次立即广播
        self.broadcast_now()
        while self._running:
            time.sleep(self.presence_interval)
            self.broadcast_now()

    def _listen_loop(self):
        """监听 UDP 广播 (参考 QuickLAN start_listen_loop)。

        收到其他设备的包时:
        1. 回送一个 presence 包让对方也知道我们
        2. 更新设备列表
        3. 触发 on_device_seen 回调
        """
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)

        try:
            sock.bind(("", self.discovery_port))
        except OSError as e:
            logger.error("UDP 绑定端口 %d 失败: %s", self.discovery_port, e)
            # 尝试 SO_REUSEPORT (Linux/macOS 支持, Windows 不存在该选项)
            reuse_port = getattr(socket, "SO_REUSEPORT", None)
            if reuse_port is not None:
                try:
                    sock.setsockopt(socket.SOL_SOCKET, reuse_port, 1)
                    sock.bind(("", self.discovery_port))
                except OSError:
                    logger.warning("端口 %d 仍被占用, 发现服务降级运行", self.discovery_port)
                    return
            else:
                logger.warning("端口 %d 仍被占用, 发现服务降级运行", self.discovery_port)
                return

        sock.settimeout(2.0)

        while self._running:
            try:
                data, addr = sock.recvfrom(8192)
                raw = json.loads(data.decode("utf-8"))
                packet = DiscoveryPacket.from_dict(raw)

                if not packet.is_lanmesh():
                    continue
                if packet.device_id == self.device_id:
                    continue

                # 回送 presence 让对方也知道我们 (参考 QuickLAN 的单播回复)
                self._send_packet(self._make_packet(), addr)

                # 更新设备列表
                with self._lock:
                    self._devices[packet.device_id] = {
                        "packet": packet,
                        "ip": addr[0],
                        "last_seen": time.time(),
                    }

                # 触发回调
                if self._on_device_seen:
                    try:
                        self._on_device_seen(packet, addr[0])
                    except Exception as e:
                        logger.error("on_device_seen 回调异常: %s", e)

            except socket.timeout:
                continue
            except json.JSONDecodeError:
                continue
            except OSError:
                continue

        sock.close()

    def _prune_loop(self):
        """定期清理超时设备 (参考 QuickLAN start_prune_loop)。"""
        while self._running:
            time.sleep(5)
            now = time.time()
            with self._lock:
                expired = [
                    did for did, entry in self._devices.items()
                    if now - entry["last_seen"] > self.device_ttl * 3
                ]
                for did in expired:
                    del self._devices[did]

    # ── UDP 发送 ────────────────────────────────────────────────

    def _broadcast_packet(self, packet: DiscoveryPacket):
        """向所有子网广播地址发送 UDP 包 (参考 QuickLAN broadcast_packet)。"""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        payload = json.dumps(packet.to_dict()).encode("utf-8")

        for target in get_broadcast_targets():
            try:
                sock.sendto(payload, (target, self.discovery_port))
            except OSError:
                continue
        sock.close()

    def _send_packet(self, packet: DiscoveryPacket, addr: tuple):
        """单播发送 UDP 包到指定地址 (参考 QuickLAN send_discovery_packet)。"""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        payload = json.dumps(packet.to_dict()).encode("utf-8")
        try:
            sock.sendto(payload, addr)
        except OSError:
            pass
        sock.close()

    def probe_ip(self, ip: str):
        """主动探测指定 IP 的设备 (参考 QuickLAN probe_ip)。"""
        packet = self._make_packet()
        self._send_packet(packet, (ip, self.discovery_port))
