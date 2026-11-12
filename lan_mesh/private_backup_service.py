"""Owned private backup operations with explicit peer selection and manual failover."""

import base64
import hashlib
import ipaddress
import json
import sqlite3
import threading
import zipfile
from types import SimpleNamespace
from pathlib import Path
from typing import Any

import requests

from .application_lifecycle import stop_signal
from .component_service import ComponentService
from .http_retry import auth_headers
from .memory_backup import export_memory
from .memory_restore import restore_memory
from .private_bundle import CHUNK_BYTES, PrivateBundleStore
from .service_runtime import ServiceError


class PrivateBackupService(ComponentService):
    OPERATIONS = ('export_memory', 'restore_memory', 'inventory', 'peers', 'status', 'receive', 'push', 'delete')

    def __init__(self, server: Any, owner: object):
        super().__init__(server, owner)
        self._lock = threading.RLock()

    def _store(self) -> PrivateBundleStore:
        folder = getattr(getattr(self.owner, 'state', None), 'shared_folder', None)
        if folder is None:
            raise ServiceError(503, '共享工作目录未初始化')
        return PrivateBundleStore(Path(getattr(folder, 'path', folder)))

    def _chat(self) -> object:
        if getattr(self.owner, 'secretary_active', False) is not True:
            raise ServiceError(503, '请先在目标实例激活Secretary，再操作记忆快照')
        chat = getattr(self.owner, 'chat_handler', None)
        if chat is None:
            raise ServiceError(503, '对话服务尚未初始化')
        return chat

    def _invoke(self, function: Any, *args: Any) -> dict:
        try:
            return function(*args)
        except ServiceError:
            raise
        except KeyError as exc:
            raise ServiceError(404, str(exc)) from exc
        except (ValueError, TypeError, sqlite3.IntegrityError, zipfile.BadZipFile) as exc:
            raise ServiceError(409, str(exc)) from exc
        except (OSError, requests.RequestException) as exc:
            print(f'[Station] 私有备份操作暂未完成: {type(exc).__name__}')
            raise ServiceError(503, '备份操作未完成，可按原备份标识重试；已有历史未被覆盖') from exc

    def export_memory(self, conv_id: str) -> dict:
        with self._lock:
            return self._invoke(export_memory, self._chat(), self._store(), conv_id)

    def restore_memory(self, bundle_id: str, payload: dict) -> dict:
        if payload.get('confirmed') is not True:
            raise ServiceError(422, '恢复快照需要明确确认')
        with self._lock:
            return self._invoke(restore_memory, self._chat(), self._store(), bundle_id)

    def inventory(self) -> dict:
        store = self._store()
        items = []
        for path in store.root.glob('*.zip'):
            if path.is_symlink():
                continue
            try:
                with zipfile.ZipFile(path) as archive:
                    if archive.getinfo('manifest.json').file_size > 4 * 1024 * 1024:
                        continue
                    manifest = json.loads(archive.read('manifest.json'))
                items.append(dict(bundle_id=path.stem, bytes=path.stat().st_size,
                                  kind=manifest['kind'], scope=manifest['scope'], created_at=manifest['created_at']))
            except (ValueError, KeyError, OSError, zipfile.BadZipFile):
                continue
        return {'items': sorted(items, key=lambda row: row['created_at'], reverse=True)[:200]}

    def peers(self) -> dict:
        myself = getattr(self.owner.state, 'device_id', '')
        return {'items': [dict(device_id=h.device_id, name=h.device_name, ip=h.ip, api_port=h.api_port)
                          for h in self._peers() if h.device_id != myself and h.ip and h.api_port]}

    def _peers(self) -> list:
        db = vars(self.owner).get('db')
        if db:
            return db.list_hosts()
        state = self.owner.state
        ip, port = getattr(state, 'secretary_ip', None), getattr(state, 'secretary_port', None)
        return [SimpleNamespace(device_id='secretary', device_name='已登记Secretary', ip=ip, api_port=port)] if ip and port else []

    def status(self, bundle_id: str) -> dict:
        with self._lock:
            return self._invoke(self._store().status, bundle_id)

    def receive(self, bundle_id: str, payload: dict) -> dict:
        with self._lock:
            return self._invoke(self._store().receive, bundle_id, payload)

    def delete(self, bundle_id: str, payload: dict) -> dict:
        """Explicit cleanup never removes active restored histories or prepared restores."""
        if payload.get('confirmed') is not True:
            raise ServiceError(422, '删除私有快照需要明确确认')
        with self._lock:
            db = vars(self.owner).get('db') or vars(self.owner).get('_delivery_db')
            job = db and db._get_conn().execute(
                "SELECT 1 FROM private_restore_jobs WHERE bundle_id=? AND state='prepared'", (bundle_id,)).fetchone()
            if job:
                raise ServiceError(409, '该快照正在恢复，请先完成恢复')
            return self._invoke(self._delete, bundle_id)

    def _delete(self, bundle_id: str) -> dict:
        store = self._store()
        for suffix in ('.zip', '.part'):
            store.path(bundle_id, suffix).unlink(missing_ok=True)
        print(f'[Station] 私有快照已删除: {bundle_id[:12]}')
        return dict(bundle_id=bundle_id, deleted=True)

    def push(self, bundle_id: str, payload: dict) -> dict:
        peer = next((h for h in self._peers() if h.device_id == str(payload.get('peer_id', ''))), None)
        if not peer or peer.device_id == getattr(self.owner.state, 'device_id', ''):
            raise ServiceError(404, '请选择已登记的其他工作站')
        try:
            address = ipaddress.ip_address(peer.ip)
            port = int(peer.api_port)
            if not 1 <= port <= 65535:
                raise ValueError('port')
        except (ValueError, TypeError) as exc:
            raise ServiceError(409, '工作站地址无效') from exc
        host = f'[{address}]' if address.version == 6 else str(address)
        url = f'http://{host}:{port}/api/station/private-bundles/{bundle_id}'
        return self._invoke(self._push, self._store(), bundle_id, url)

    def _push(self, store: PrivateBundleStore, bundle_id: str, url: str) -> dict:
        store.validate(bundle_id)
        path = store.path(bundle_id)
        total = path.stat().st_size
        status = self._request('GET', url)
        if status.get('bundle_id') != bundle_id or type(status.get('offset')) is not int or not 0 <= status['offset'] <= total:
            raise ValueError('对端返回无效的备份接收位置')
        offset = status['offset']
        with path.open('rb') as handle:
            handle.seek(offset)
            while offset < total:
                if stop_signal(self.owner).is_set():
                    raise OSError('station stopping')
                data = handle.read(CHUNK_BYTES)
                status = self._request('POST', url + '/chunks', dict(offset=offset, total=total,
                    data=base64.b64encode(data).decode(), sha256=hashlib.sha256(data).hexdigest()))
                offset += len(data)
                if status.get('bundle_id') != bundle_id or status.get('offset') != offset:
                    raise ValueError('对端未确认完整分片')
        if not status.get('complete') or status.get('offset') != total:
            raise ValueError('对端未确认完整备份')
        print(f'[Station] 私有备份复制已确认: {bundle_id[:12]} bytes={total}')
        return dict(bundle_id=bundle_id, state='replicated', bytes=total)

    @staticmethod
    def _request(method: str, url: str, payload: dict | None = None) -> dict:
        response = requests.request(method, url, json=payload, headers=auth_headers(), timeout=10, allow_redirects=False)
        if response.status_code != 200:
            raise ServiceError(503, f'对端备份接口返回HTTP {response.status_code}，请核对认证和版本')
        return response.json()
