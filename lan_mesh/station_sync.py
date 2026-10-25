"""
集群同步域 — 密钥/资源/配置/版本四类同步与对齐。

拆分自 station_controller.py (iter-74 Phase 2)。覆盖 S1 API Key 加密分发
(push/pull)、S3 启动时一次性同步、R4 配置多数派对齐、F1 版本领导者比对与
自动升级。所有跨节点调用均包裹超时与异常隔离, 失败不阻断主流程。
"""
import sys
import threading
import time
from pathlib import Path

from .config import load_model_pool
from .http_retry import http_post
from .logger import get_logger

logger = get_logger("station")


from typing import Any

class StationSyncMixin:
    """密钥/配置/版本同步与对齐。

    依赖 StationController 提供的属性: cfg / state / db / director /
    _running / _config_hash 等。
    """

    def _startup_sync_once(self) -> Any:
        return call_component(self, StationSyncService, '_startup_sync_once')

    def _startup_key_sync(self, peers: list) -> Any:
        return call_component(self, StationSyncService, '_startup_key_sync', peers=peers)

    def _align_config_with_peers(self, peers: list=None) -> dict:
        return call_component(self, StationSyncService, '_align_config_with_peers', peers=peers)

    def _align_loop(self) -> Any:
        return call_component(self, StationSyncService, '_align_loop')

    def pull_resource_secrets(self, ip: str, port: int) -> dict:
        return call_component(self, StationSyncService, 'pull_resource_secrets', ip=ip, port=port)

    def _sync_with_new_peer(self, device_id: str, ip: str, port: int, role: str, code_version: str, version_ts: float) -> Any:
        return call_component(self, StationSyncService, '_sync_with_new_peer', device_id=device_id, ip=ip, port=port, role=role, code_version=code_version, version_ts=version_ts)

    def _check_version_leadership(self, self_ver: dict, peers: list) -> Any:
        return call_component(self, StationSyncService, '_check_version_leadership', self_ver=self_ver, peers=peers)

    def _auto_upgrade(self, leader_commit: str, leader_name: str='') -> Any:
        return call_component(self, StationSyncService, '_auto_upgrade', leader_commit=leader_commit, leader_name=leader_name)

    def push_resource_secrets(self, only_device_id: str='', fallback_ip: str='', fallback_port: int=0) -> list:
        return call_component(self, StationSyncService, 'push_resource_secrets', only_device_id=only_device_id, fallback_ip=fallback_ip, fallback_port=fallback_port)


from typing import Any
from .component_service import call_component
from .station_sync_service import StationSyncService
