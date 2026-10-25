"""CloudTransferService: domain behavior separated from the public compatibility facade."""

from __future__ import annotations
from typing import Any
from . import cloud_sync as policy
from .component_service import ComponentService

class CloudTransferService(ComponentService):
    """CloudTransferService business operations with explicit state ownership."""
    OPERATIONS = ('_ensure_client', 'test_connection', 'sync', '_cloud_key', '_list_cloud_objects', '_upload_all', '_download_all', 'get_status')

    def _ensure_client(self) -> Any:
        """延迟初始化 boto3 S3 客户端。"""
        if self.owner._client is not None:
            return
        import boto3
        from botocore.config import Config as BotoConfig
        scheme = 'https' if self.owner.secure else 'http'
        endpoint_url = f'{scheme}://{self.owner.endpoint}'
        config = BotoConfig(signature_version='s3v4', retries={'max_attempts': 3, 'mode': 'standard'})
        self.owner._client = boto3.client('s3', endpoint_url=endpoint_url, aws_access_key_id=self.owner.access_key, aws_secret_access_key=self.owner.secret_key, region_name=self.owner.region, config=config)

    def test_connection(self) -> dict:
        """测试云存储连接是否正常。"""
        try:
            self.owner._ensure_client()
            self.owner._client.head_bucket(Bucket=self.owner.bucket)
            return {'ok': True, 'bucket': self.owner.bucket, 'endpoint': self.owner.endpoint}
        except Exception as e:
            return {'ok': False, 'error': str(e)}

    def sync(self) -> dict:
        """执行一次双向同步。

        Returns:
            {uploaded: int, downloaded: int, errors: list, duration: float}
        """
        with self.owner._lock:
            start = policy.time.time()
            uploaded = 0
            downloaded = 0
            errors = []
            try:
                self.owner._ensure_client()
            except Exception as e:
                return {'uploaded': 0, 'downloaded': 0, 'errors': [f'客户端初始化失败: {e}'], 'duration': 0}
            u, u_err = self.owner._upload_all()
            uploaded += u
            errors.extend(u_err)
            d, d_err = self.owner._download_all()
            downloaded += d
            errors.extend(d_err)
            duration = policy.time.time() - start
            self.owner._last_sync_time = policy.time.time()
            self.owner._last_sync_result = {'uploaded': uploaded, 'downloaded': downloaded, 'errors': errors, 'duration': round(duration, 2), 'timestamp': self.owner._last_sync_time}
            if uploaded or downloaded:
                policy.logger.info('同步完成: 上传 %d, 下载 %d, 耗时 %.1fs', uploaded, downloaded, duration)
            elif errors:
                policy.logger.warning('同步完成 (有错误): %d 个错误', len(errors))
            return self.owner._last_sync_result

    def _cloud_key(self, relative_path: str) -> str:
        """生成云端对象 key: prefix/device_id/relative_path"""
        return f'{self.owner.prefix}{self.owner.device_id}/{relative_path}'.replace('\\', '/')

    def _list_cloud_objects(self) -> dict:
        """列举云端本主机前缀下的所有对象。

        Returns:
            {relative_path: {"key": str, "size": int, "mtime": float}}
        """
        objects = {}
        prefix = f'{self.owner.prefix}{self.owner.device_id}/'
        paginator = self.owner._client.get_paginator('list_objects_v2')
        for page in paginator.paginate(Bucket=self.owner.bucket, Prefix=prefix):
            for obj in page.get('Contents', []):
                key = obj['Key']
                rel = key[len(prefix):]
                if not rel:
                    continue
                objects[rel] = {'key': key, 'size': obj['Size'], 'mtime': obj['LastModified'].timestamp()}
        return objects

    def _upload_all(self) -> tuple:
        """上传本地新增/修改文件到云端。

        Returns:
            (uploaded_count, errors_list)
        """
        uploaded = 0
        errors = []
        if not self.owner.local_path.is_dir():
            return (0, [])
        try:
            cloud_objects = self.owner._list_cloud_objects()
        except Exception as e:
            return (0, [f'列举云端对象失败: {e}'])
        for local_file in self.owner.local_path.rglob('*'):
            if not local_file.is_file():
                continue
            if local_file.name.startswith('.'):
                continue
            rel = str(local_file.relative_to(self.owner.local_path))
            cloud_key = self.owner._cloud_key(rel)
            try:
                local_mtime = local_file.stat().st_mtime
                cloud_info = cloud_objects.get(rel)
                if cloud_info and local_mtime <= cloud_info['mtime']:
                    continue
                self.owner._client.upload_file(str(local_file), self.owner.bucket, cloud_key)
                uploaded += 1
            except Exception as e:
                errors.append(f'上传 {rel} 失败: {e}')
        return (uploaded, errors)

    def _download_all(self) -> tuple:
        """从云端下载新增/修改文件到本地。

        Returns:
            (downloaded_count, errors_list)
        """
        downloaded = 0
        errors = []
        try:
            cloud_objects = self.owner._list_cloud_objects()
        except Exception as e:
            return (0, [f'列举云端对象失败: {e}'])
        for rel, info in cloud_objects.items():
            local_file = self.owner.local_path / rel
            try:
                if local_file.is_file():
                    local_mtime = local_file.stat().st_mtime
                    if local_mtime >= info['mtime']:
                        continue
                local_file.parent.mkdir(parents=True, exist_ok=True)
                self.owner._client.download_file(self.owner.bucket, info['key'], str(local_file))
                downloaded += 1
            except Exception as e:
                errors.append(f'下载 {rel} 失败: {e}')
        return (downloaded, errors)

    def get_status(self) -> dict:
        """返回同步状态。"""
        return {'enabled': True, 'running': self.owner._running, 'auto_sync': self.owner.auto_sync, 'sync_interval': self.owner.sync_interval, 'endpoint': self.owner.endpoint, 'bucket': self.owner.bucket, 'prefix': f'{self.owner.prefix}{self.owner.device_id}/', 'local_path': str(self.owner.local_path), 'last_sync': self.owner._last_sync_result}
