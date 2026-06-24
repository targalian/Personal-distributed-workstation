"""
主机配置自动采集 - 使用 psutil 收集 CPU/内存/磁盘/OS/网络信息

参考 QuickLAN 的 device identity 设计，扩展了完整的硬件画像采集。
"""
import os
import platform
import socket
import time
import uuid
from pathlib import Path
from typing import List

import psutil

from .protocol import HostInfo, DiscoveryPacket


# ── 设备 ID 持久化 ──────────────────────────────────────────────

def load_or_create_device_id(data_dir: Path, role: str = "worker") -> str:
    """加载或生成持久化的设备 UUID。

    参考QuickLAN的 load_or_create_device_id,在数据目录下
    持久化一个 UUID 用于跨重启保持设备身份。
    角色参数确保 Master/Worker 在同一台机器上有独立身份。
    """
    device_file = data_dir / f"device_id_{role}"
    if device_file.is_file():
        value = device_file.read_text(encoding="utf-8").strip()
        if value:
            return value

    data_dir.mkdir(parents=True, exist_ok=True)
    new_id = str(uuid.uuid4())
    device_file.write_text(new_id, encoding="utf-8")
    return new_id


# ── 网络信息采集 ────────────────────────────────────────────────

def get_local_ipv4_addresses() -> List[str]:
    """获取所有非回环 IPv4 地址。

    参考QuickLAN的 local_ipv4_addresses。
    """
    ips = []
    try:
        for name, addrs in psutil.net_if_addrs().items():
            for addr in addrs:
                if addr.family == socket.AF_INET:
                    ip = addr.address
                    if not ip.startswith("127."):
                        ips.append(ip)
    except Exception:
        pass
    return sorted(set(ips))


def get_mac_address() -> str:
    """获取首个非虚拟网卡的 MAC 地址。

    使用 psutil.AF_LINK 跨平台获取 MAC (Linux: AF_PACKET, Windows: AF_LINK)。
    """
    try:
        for name, addrs in psutil.net_if_addrs().items():
            if name.startswith(("lo", "docker", "virbr", "veth", "br-", "Loopback")):
                continue
            for addr in addrs:
                if addr.family == psutil.AF_LINK and addr.address:
                    return addr.address.replace("-", ":")
    except Exception:
        pass
    return ""


def get_broadcast_targets() -> List[str]:
    """计算所有子网广播地址。

    参考QuickLAN的 broadcast_targets 与 subnet_broadcast 算法。
    """
    import struct

    targets = ["255.255.255.255"]
    try:
        for name, addrs in psutil.net_if_addrs().items():
            if name.startswith(("lo", "docker", "virbr", "veth", "br-")):
                continue
            ip_addr = None
            netmask = None
            for addr in addrs:
                if addr.family == socket.AF_INET and not addr.address.startswith("127."):
                    ip_addr = addr.address
                    netmask = addr.netmask
            if ip_addr and netmask:
                ip_int = struct.unpack("!I", socket.inet_aton(ip_addr))[0]
                mask_int = struct.unpack("!I", socket.inet_aton(netmask))[0]
                bcast_int = ip_int | (~mask_int & 0xFFFFFFFF)
                bcast = socket.inet_ntoa(struct.pack("!I", bcast_int))
                targets.append(bcast)
    except Exception:
        pass
    return sorted(set(targets))


# ── 磁盘信息 ────────────────────────────────────────────────────

def get_disk_info(path: str = None) -> dict:
    """获取指定挂载点的磁盘使用情况。

    默认使用系统根分区: Linux/macOS 为 '/', Windows 为系统盘 (如 'C:\\')。
    """
    if path is None:
        path = os.path.abspath(os.sep)  # Linux: '/', Windows: 'C:\\'
    try:
        usage = psutil.disk_usage(path)
        return {
            "total_gb": int(usage.total / (1024 ** 3)),
            "used_gb": int(usage.used / (1024 ** 3)),
            "free_gb": int(usage.free / (1024 ** 3)),
            "percent": usage.percent,
        }
    except Exception:
        return {"total_gb": 0, "used_gb": 0, "free_gb": 0, "percent": 0.0}


# ── 完整主机信息采集 ────────────────────────────────────────────

def collect_host_info(
    device_id: str,
    device_name: str,
    role: str,
    api_port: int,
    shared_folder: str,
    shared_file_count: int,
    start_time: float,
) -> HostInfo:
    """采集完整主机配置信息。

    使用 psutil 收集 CPU、内存、磁盘、网络等运行时数据。
    参考QuickLAN的设备信息采集，扩展了更完整的硬件画像。
    """
    # CPU
    cpu_count = psutil.cpu_count(logical=True) or 0
    cpu_percent = psutil.cpu_percent(interval=0.5)
    cpu_freq = psutil.cpu_freq()
    cpu_freq_mhz = cpu_freq.current if cpu_freq else 0.0

    # 内存
    mem = psutil.virtual_memory()

    # 磁盘 (根分区)
    disk = get_disk_info("/")

    # 网络
    ips = get_local_ipv4_addresses()
    mac = get_mac_address()

    return HostInfo(
        device_id=device_id,
        device_name=device_name,
        role=role,
        hostname=socket.gethostname(),
        platform=platform.system(),
        platform_release=platform.release(),
        architecture=platform.machine(),
        python_version=platform.python_version(),
        # CPU
        cpu_count=cpu_count,
        cpu_percent=round(cpu_percent, 1),
        cpu_freq_mhz=round(cpu_freq_mhz, 0),
        # 内存
        memory_total_mb=int(mem.total / (1024 ** 2)),
        memory_available_mb=int(mem.available / (1024 ** 2)),
        memory_percent=round(mem.percent, 1),
        # 磁盘
        disk_total_gb=disk["total_gb"],
        disk_used_gb=disk["used_gb"],
        disk_free_gb=disk["free_gb"],
        disk_percent=round(disk["percent"], 1),
        # 网络
        ip_addresses=ips,
        mac_address=mac,
        # 共享
        shared_folder=shared_folder,
        shared_file_count=shared_file_count,
        # 运行时
        api_port=api_port,
        uptime_seconds=round(time.time() - start_time, 1),
        timestamp=time.time(),
    )


def make_discovery_packet(host: HostInfo) -> DiscoveryPacket:
    """根据完整主机信息生成 UDP 发现包摘要。"""
    return DiscoveryPacket(
        device_id=host.device_id,
        device_name=host.device_name,
        role=host.role,
        api_port=host.api_port,
        hostname=host.hostname,
        platform=host.platform,
        cpu_count=host.cpu_count,
        cpu_percent=host.cpu_percent,
        memory_total_mb=host.memory_total_mb,
        memory_percent=host.memory_percent,
        disk_total_gb=host.disk_total_gb,
        disk_percent=host.disk_percent,
        shared_folder=host.shared_folder,
        ip_addresses=host.ip_addresses,
    )
