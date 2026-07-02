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
import threading
import time
from pathlib import Path
from typing import Optional


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

    def _ensure_client(self):
        """延迟初始化 boto3 S3 客户端。"""
        if self._client is not None:
            return

        import boto3
        from botocore.config import Config as BotoConfig

        scheme = "https" if self.secure else "http"
        endpoint_url = f"{scheme}://{self.endpoint}"

        config = BotoConfig(
            signature_version="s3v4",
            retries={"max_attempts": 3, "mode": "standard"},
        )

        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=self.access_key,
            aws_secret_access_key=self.secret_key,
            region_name=self.region,
            config=config,
        )

    def test_connection(self) -> dict:
        """测试云存储连接是否正常。"""
        try:
            self._ensure_client()
            self._client.head_bucket(Bucket=self.bucket)
            return {"ok": True, "bucket": self.bucket, "endpoint": self.endpoint}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ── 同步核心逻辑 ────────────────────────────────────────────

    def sync(self) -> dict:
        """执行一次双向同步。

        Returns:
            {uploaded: int, downloaded: int, errors: list, duration: float}
        """
        with self._lock:
            start = time.time()
            uploaded = 0
            downloaded = 0
            errors = []

            try:
                self._ensure_client()
            except Exception as e:
                return {"uploaded": 0, "downloaded": 0, "errors": [f"客户端初始化失败: {e}"], "duration": 0}

            # 1. 上传本地文件到云端
            u, u_err = self._upload_all()
            uploaded += u
            errors.extend(u_err)

            # 2. 从云端下载文件到本地
            d, d_err = self._download_all()
            downloaded += d
            errors.extend(d_err)

            duration = time.time() - start
            self._last_sync_time = time.time()
            self._last_sync_result = {
                "uploaded": uploaded,
                "downloaded": downloaded,
                "errors": errors,
                "duration": round(duration, 2),
                "timestamp": self._last_sync_time,
            }

            if uploaded or downloaded:
                print(f"[CloudSync] 同步完成: 上传 {uploaded}, 下载 {downloaded}, 耗时 {duration:.1f}s")
            elif errors:
                print(f"[CloudSync] 同步完成 (有错误): {len(errors)} 个错误")

            return self._last_sync_result

    def _cloud_key(self, relative_path: str) -> str:
        """生成云端对象 key: prefix/device_id/relative_path"""
        return f"{self.prefix}{self.device_id}/{relative_path}".replace("\\", "/")

    def _list_cloud_objects(self) -> dict:
        """列举云端本主机前缀下的所有对象。

        Returns:
            {relative_path: {"key": str, "size": int, "mtime": float}}
        """
        objects = {}
        prefix = f"{self.prefix}{self.device_id}/"

        paginator = self._client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                rel = key[len(prefix):]
                if not rel:
                    continue
                objects[rel] = {
                    "key": key,
                    "size": obj["Size"],
                    "mtime": obj["LastModified"].timestamp(),
                }
        return objects

    def _upload_all(self) -> tuple:
        """上传本地新增/修改文件到云端。

        Returns:
            (uploaded_count, errors_list)
        """
        uploaded = 0
        errors = []

        if not self.local_path.is_dir():
            return 0, []

        # 获取云端已有对象
        try:
            cloud_objects = self._list_cloud_objects()
        except Exception as e:
            return 0, [f"列举云端对象失败: {e}"]

        # 遍历本地文件
        for local_file in self.local_path.rglob("*"):
            if not local_file.is_file():
                continue
            if local_file.name.startswith("."):
                continue

            rel = str(local_file.relative_to(self.local_path))
            cloud_key = self._cloud_key(rel)

            try:
                local_mtime = local_file.stat().st_mtime
                cloud_info = cloud_objects.get(rel)

                # 跳过: 云端存在且本地未修改
                if cloud_info and local_mtime <= cloud_info["mtime"]:
                    continue

                # 上传
                self._client.upload_file(
                    str(local_file),
                    self.bucket,
                    cloud_key,
                )
                uploaded += 1

            except Exception as e:
                errors.append(f"上传 {rel} 失败: {e}")

        return uploaded, errors

    def _download_all(self) -> tuple:
        """从云端下载新增/修改文件到本地。

        Returns:
            (downloaded_count, errors_list)
        """
        downloaded = 0
        errors = []

        try:
            cloud_objects = self._list_cloud_objects()
        except Exception as e:
            return 0, [f"列举云端对象失败: {e}"]

        for rel, info in cloud_objects.items():
            local_file = self.local_path / rel

            try:
                # 跳过: 本地存在且本地更新
                if local_file.is_file():
                    local_mtime = local_file.stat().st_mtime
                    if local_mtime >= info["mtime"]:
                        continue

                # 确保目录存在
                local_file.parent.mkdir(parents=True, exist_ok=True)

                # 下载
                self._client.download_file(
                    self.bucket,
                    info["key"],
                    str(local_file),
                )
                downloaded += 1

            except Exception as e:
                errors.append(f"下载 {rel} 失败: {e}")

        return downloaded, errors

    # ── 自动同步 ────────────────────────────────────────────────

    def start_auto_sync(self):
        """启动定时自动同步后台线程。"""
        if self._running:
            return
        if not self.auto_sync:
            return

        self._running = True

        # 启动时立即同步一次
        try:
            result = self.sync()
            print(f"[CloudSync] 首次同步: 上传 {result['uploaded']}, 下载 {result['downloaded']}")
        except Exception as e:
            print(f"[CloudSync] 首次同步失败: {e}")

        # 定时同步 (sync_interval > 0)
        if self.sync_interval > 0:
            self._sync_thread = threading.Thread(
                target=self._sync_loop, name="cloud-sync", daemon=True
            )
            self._sync_thread.start()
            print(f"[CloudSync] 定时同步已启动: 间隔 {self.sync_interval}s")

    def _sync_loop(self):
        """定时同步循环。"""
        while self._running:
            time.sleep(self.sync_interval)
            try:
                self.sync()
            except Exception as e:
                print(f"[CloudSync] 定时同步异常: {e}")

    def stop(self):
        """停止自动同步。"""
        self._running = False

    # ── 状态查询 ────────────────────────────────────────────────

    def get_status(self) -> dict:
        """返回同步状态。"""
        return {
            "enabled": True,
            "running": self._running,
            "auto_sync": self.auto_sync,
            "sync_interval": self.sync_interval,
            "endpoint": self.endpoint,
            "bucket": self.bucket,
            "prefix": f"{self.prefix}{self.device_id}/",
            "local_path": str(self.local_path),
            "last_sync": self._last_sync_result,
        }
