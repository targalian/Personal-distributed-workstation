"""
主机与发现域 — 发现回调、主机信息采集、配置刷新下发、日志修剪、Bot 通道。

拆分自 station_controller.py (iter-74 Phase 2)。Station Director「只管机器」
的那一半: UDP 发现包构造与设备上线回调 (含离线清理触发 PM 孤儿迁移)、
配置脚本下发与周期刷新、日志修剪守护、Bot 通道配置与命令分派。
"""
import shutil
import threading
import time
from pathlib import Path

from .bot_gateway import BotChannel
from .host_info import collect_host_info, make_discovery_packet
from .logger import get_logger
from .protocol import (
    DiscoveryPacket,
    HEARTBEAT_INTERVAL_SECS,
    HostInfo,
    PRUNE_INTERVAL_SECS,
)

logger = get_logger("station")


from typing import Any

class StationHostsMixin:
    """发现/主机/配置刷新/日志修剪/Bot 通道。

    依赖 StationController 提供的属性: cfg / state / db / director /
    bot_gateway / discovery / _running 等。
    """

    def _load_bot_config(self) -> Any:
        return call_component(self, StationHostService, '_load_bot_config')

    def _on_bot_command(self, command: str, args: str, chat_id: str) -> str:
        return call_component(self, StationHostService, '_on_bot_command', command=command, args=args, chat_id=chat_id)

    def _collect_info(self) -> HostInfo:
        return call_component(self, StationHostService, '_collect_info')

    def _make_packet(self) -> DiscoveryPacket:
        return call_component(self, StationHostService, '_make_packet')

    def _on_device_seen(self, packet: DiscoveryPacket, ip: str) -> Any:
        return call_component(self, StationHostService, '_on_device_seen', packet=packet, ip=ip)

    def _deploy_config_script(self) -> Any:
        return call_component(self, StationHostService, '_deploy_config_script')

    def _refresh_host_config(self) -> Any:
        return call_component(self, StationHostService, '_refresh_host_config')

    def _config_refresh_loop(self) -> Any:
        return call_component(self, StationHostService, '_config_refresh_loop')

    def _prune_logs_if_due(self) -> Any:
        return call_component(self, StationHostService, '_prune_logs_if_due')

    def _prune_loop(self) -> Any:
        return call_component(self, StationHostService, '_prune_loop')


from typing import Any
from .component_service import call_component
from .station_host_service import StationHostService
