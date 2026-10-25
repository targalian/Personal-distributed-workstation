"""
云存储同步管理器 — S3 兼容服务 (阿里云 OSS / MinIO / AWS S3)

功能:
1. 启动时自动将本地共享文件夹同步到云存储
2. 从云存储下载其他主机上传的文件
3. 可选定时自动同步 (sync_interval)
4. 密钥优先从环境变量读取

设计:
- 使用 boto3 作为 S3 客户端
- 每台主机以 device_id 为前缀存储, 避免冲突
- 同步策略: 上传本地新增/修改文件, 下载云端新增/修改文件 (双向增量同步)
"""
import os
from .application_lifecycle import wait_for_stop
import threading
import time
from pathlib import Path
from typing import Optional

from .logger import get_logger

logger = get_logger("cloud_sync")


from typing import Any

class CloudSyncManager:
    """云存储同步管理器。

    将本地共享文件夹与 S3 兼容云存储双向同步,
    使不同主机之间通过云盘共享文件。

    存储结构:
      s3://bucket/prefix/<device_id>/file1.txt
      s3://bucket/prefix/<device_id>/subdir/file2.txt

    同步逻辑:
      - 上传: 本地文件 mtime > 云端 mtime (或云端不存在) → 上传
      - 下载: 云端文件 mtime > 本地 mtime (或本地不存在) → 下载
    """

    def __init__(
        self,
        local_path: str,
        device_id: str,
        endpoint: str,
        bucket: str,
        prefix: str = "shared/",
        access_key: str = "",
        secret_key: str = "",
        region: str = "",
        secure: bool = True,
        auto_sync: bool = True,
        sync_interval: int = 300,
    ):
        self.local_path = Path(local_path).expanduser().resolve()
        self.device_id = device_id
        self.endpoint = endpoint
        self.bucket = bucket
        self.prefix = prefix.strip("/") + "/"
        self.region = region or None
        self.secure = secure
        self.auto_sync = auto_sync
        self.sync_interval = sync_interval

        # 密钥: 环境变量优先
        self.access_key = (
            os.environ.get("CLOUD_STORAGE_ACCESS_KEY", "") or access_key
        )
        self.secret_key = (
            os.environ.get("CLOUD_STORAGE_SECRET_KEY", "") or secret_key
        )

        self._client = None
        self._running = False
        self._sync_thread: Optional[threading.Thread] = None
        self._last_sync_time: float = 0.0
        self._last_sync_result: dict = {}
        self._lock = threading.Lock()

    # ── 客户端初始化 ────────────────────────────────────────────

    def _ensure_client(self) -> Any:
        return call_component(self, CloudTransferService, '_ensure_client')

    def test_connection(self) -> dict:
        return call_component(self, CloudTransferService, 'test_connection')

    # ── 同步核心逻辑 ────────────────────────────────────────────

    def sync(self) -> dict:
        return call_component(self, CloudTransferService, 'sync')

    def _cloud_key(self, relative_path: str) -> str:
        return call_component(self, CloudTransferService, '_cloud_key', relative_path=relative_path)

    def _list_cloud_objects(self) -> dict:
        return call_component(self, CloudTransferService, '_list_cloud_objects')

    def _upload_all(self) -> tuple:
        return call_component(self, CloudTransferService, '_upload_all')

    def _download_all(self) -> tuple:
        return call_component(self, CloudTransferService, '_download_all')

    # ── 自动同步 ────────────────────────────────────────────────

    def start_auto_sync(self):
        """启动定时自动同步后台线程。"""
        if self._running:
            return
        if not self.auto_sync:
            return

        self._running = True
        from .application_lifecycle import stop_signal
        stop_signal(self).clear()

        # 启动时立即同步一次
        try:
            result = self.sync()
            logger.info("首次同步: 上传 %d, 下载 %d", result['uploaded'], result['downloaded'])
        except Exception as e:
            logger.error("首次同步失败: %s", e)

        # 定时同步 (sync_interval > 0)
        if self.sync_interval > 0:
            self._sync_thread = threading.Thread(
                target=self._sync_loop, name="cloud-sync", daemon=True
            )
            self._sync_thread.start()
            logger.info("定时同步已启动: 间隔 %ds", self.sync_interval)

    def _sync_loop(self):
        """定时同步循环。"""
        while self._running:
            if wait_for_stop(self, self.sync_interval):
                return
            try:
                self.sync()
            except Exception as e:
                logger.error("定时同步异常: %s", e)

    def stop(self) -> None:
        """停止自动同步。"""
        self._running = False
        from .application_lifecycle import stop_signal, join_threads
        stop_signal(self).set()
        join_threads([getattr(self, '_sync_thread', None)])

    # ── 状态查询 ────────────────────────────────────────────────

    def get_status(self) -> dict:
        return call_component(self, CloudTransferService, 'get_status')


from typing import Any
from .component_service import call_component
from .cloud_transfer_service import CloudTransferService
