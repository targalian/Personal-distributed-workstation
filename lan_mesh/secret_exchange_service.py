"""SecretExchangeService: transport-neutral interaction operations behind the original API."""

from fastapi import Request
from .event_bus import publish_event
from .station_routes_common import _heal_mesh_token_from, get_mesh_auth_token

from .logger import get_logger
from .service_context import StationService, StationServiceContext
from .service_runtime import ServiceError

logger = get_logger("station_api")

class SecretExchangeService(StationService):
    """Resolve current components per call and preserve legacy response contracts."""

    def __init__(self, server, context: StationServiceContext):
        super().__init__(server, context, "/SecretExchangeService")

    def fetch_secrets(self) -> dict:
        """S3: 提供本机资源配置加密密文, 供新节点启动时拉取 (API Key 自动同步)。

        与 bootstrap-token 同一信任假设 (局域网成员); 密文受
        AES-256-GCM 保护, 无 mesh_token 无法解密。本机无可用
        密钥配置时返回空 blob (调用方幂等跳过)。
        """
        controller = self.context.controller
        state = controller.state
        db = getattr(controller, 'db', None)
        from pathlib import Path
        from .auth import get_mesh_token
        from .model_resources import read_config_data
        from .secret_sync import config_hash, encrypt_config
        target = Path(__file__).parent / 'resources.yaml'
        if not target.is_file():
            return {'blob': '', 'detail': '本机无 resources.yaml'}
        cfg = read_config_data(target)
        data = cfg.get('data') or {}
        pools = data.get('resources') or []
        key_count = sum((1 for p in pools if (p.get('api_key') or '').strip()))
        if not key_count:
            return {'blob': '', 'detail': '本机无 api_key 直填配置'}
        token = get_mesh_auth_token() or get_mesh_token()
        if not token:
            return {'blob': '', 'detail': '无 mesh_token, 无法加密'}
        payload = encrypt_config(data, token)
        payload['config_hash'] = config_hash(data)
        payload['pools'] = len(pools)
        try:
            payload['config_ts'] = float(data.get('config_ts') or 0)
        except (TypeError, ValueError):
            payload['config_ts'] = 0.0
        return payload

    def receive_secrets(self, payload: dict, request: Request) -> dict:
        """S1: 接收 Secretary 加密推送的资源配置 (含 API Key 直填)。

        解密用本机 mesh_token (与推送方同信任根, 与认证开关解耦);
        校验通过后落盘 resources.yaml 并热重载 (直填 key 由 load()
        注入环境变量); 配置指纹一致时幂等跳过。
        """
        controller = self.context.controller
        state = controller.state
        db = getattr(controller, 'db', None)
        from pathlib import Path
        from .auth import get_mesh_token
        from .config import load_model_pool
        from .model_resources import init_resource_manager, read_config_data, save_config, set_bot_notify_global, validate_config
        from .secret_sync import config_hash, decrypt_config
        local_token = get_mesh_auth_token() or get_mesh_token()
        try:
            data = decrypt_config(payload, local_token)
        except (ValueError, RuntimeError) as e:
            healed = ''
            if 'mesh_token 不匹配' in str(e):
                src_ip = request.client.host if request.client else ''
                try:
                    src_port = int(payload.get('src_port') or 0)
                except (TypeError, ValueError):
                    src_port = 0
                if src_ip and src_port:
                    try:
                        healed = _heal_mesh_token_from(controller, src_ip, src_port)
                    except Exception as heal_err:
                        logger.warning('[S1] 密钥接收自愈收敛失败: %s', heal_err)
            if not healed or healed == local_token:
                raise ServiceError(status_code=400, detail=str(e))
            try:
                data = decrypt_config(payload, healed)
            except (ValueError, RuntimeError) as retry_err:
                raise ServiceError(status_code=400, detail=str(retry_err))
        expected = (payload.get('config_hash') or '').strip()
        if expected and config_hash(data) != expected:
            raise ServiceError(status_code=400, detail='配置指纹不匹配, 拒绝落盘')
        if not isinstance(data, dict):
            raise ServiceError(status_code=400, detail='解密内容非配置对象')
        target = Path(__file__).parent / 'resources.yaml'
        current = read_config_data(target) if target.is_file() else {}
        cur_data = current.get('data') or {}
        if cur_data and config_hash(cur_data) == config_hash(data):
            return {'ok': True, 'applied': False, 'detail': '配置一致', 'pools': len(cur_data.get('resources') or [])}
        errors = validate_config(data)
        if errors:
            raise ServiceError(status_code=400, detail='接收配置校验失败: ' + '; '.join(errors))
        saved = save_config(target, data)
        if not saved.get('ok'):
            raise ServiceError(status_code=500, detail=f"保存失败: {saved.get('error')}")
        pool = load_model_pool()
        mgr = init_resource_manager(target, pool.models if pool.models else None, controller.db)
        set_bot_notify_global(controller.bot_gateway.notify)
        publish_event('resource_config', {'ok': True, 'pools': len(mgr.list_resources()), 'source': 'secret-sync'})
        logger.info('[S1] 已接收加密密钥分发, 应用 %d 个资源池', len(mgr.list_resources()))
        return {'ok': True, 'applied': True, 'detail': '已应用', 'pools': len(mgr.list_resources())}

    def sync_secrets_all(self) -> dict:
        """F1: 手动触发全网密钥对齐 (角色无关, config_ts 仲裁)。"""
        controller = self.context.controller
        state = controller.state
        db = getattr(controller, 'db', None)
        summary = controller._align_config_with_peers()
        return {'ok': bool(summary['pushed'] or summary['pulled']), 'pushed': len(summary['pushed']), 'pulled': len(summary['pulled']), 'skipped': summary['skipped'], 'failed': summary['failed']}
