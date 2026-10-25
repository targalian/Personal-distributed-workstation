"""ResourceApiService: transport-neutral interaction operations behind the original API."""

import threading
from .event_bus import publish_event, recent_events
from .resource_usage_batch import apply_usage_batch

from .logger import get_logger
from .service_context import StationService, StationServiceContext
from .service_runtime import ServiceError

logger = get_logger("station_api")

class ResourceApiService(StationService):
    """Resolve current components per call and preserve legacy response contracts."""

    def __init__(self, server, context: StationServiceContext):
        super().__init__(server, context, "/ResourceApiService")

    def get_model_resources(self) -> dict:
        """查询模型资源池汇总 (额度/已用/剩余/状态)。未启用时返回空列表。"""
        controller = self.context.controller
        state = controller.state
        db = getattr(controller, 'db', None)
        self.require_secretary()
        from .model_resources import resource_summary
        return resource_summary()

    def record_model_usage(self, payload: dict) -> dict:
        """记录 LLM 调用消耗 (模型 → 资源池自动匹配)。

        单条: {model, input_tokens, output_tokens, usage_id?,
            task_id?, project_id?}
        批量 (R3 Worker 上报): {records: [{usage_id, model,
            input_tokens, output_tokens, task_id?, project_id?}, ...]}
            — usage_id 幂等去重。
        """
        controller = self.context.controller
        state = controller.state
        db = getattr(controller, 'db', None)
        self.require_secretary()
        from .model_resources import record_usage_global
        records = payload.get('records')
        if isinstance(records, list) and records:
            return {'batch': True, **apply_usage_batch(records)}
        return record_usage_global(payload.get('model', ''), payload.get('input_tokens', 0), payload.get('output_tokens', 0), usage_id=str(payload.get('usage_id', '')), task_id=str(payload.get('task_id', '')), project_id=str(payload.get('project_id', '')))

    def get_cost_by_task(self, limit: int=100) -> dict:
        """R6: 成本分摊聚合 — 按 task_id 分组统计调用次数/token/金额。

        无归因记录 task_id 为空串 (前端展示为「未归因」)。
        """
        controller = self.context.controller
        state = controller.state
        db = getattr(controller, 'db', None)
        self.require_secretary()
        if not controller.db:
            return {'enabled': False, 'items': []}
        try:
            items = controller.db.query_cost_by_task(limit=max(1, min(int(limit or 100), 500)))
        except Exception:
            items = []
        return {'enabled': True, 'items': items}

    def probe_model_balances(self) -> dict:
        """触发资源池服务商余额自动探测 (R2)。

        调用各家余额 API (SiliconFlow/DeepSeek/Moonshot/智谱), 结果
        缓存并附到 /api/resources 汇总; 未支持的服务商返回引导提示。
        """
        controller = self.context.controller
        state = controller.state
        db = getattr(controller, 'db', None)
        self.require_secretary()
        from .model_resources import probe_balances_global
        return probe_balances_global()

    def get_resources_config(self) -> dict:
        """读取资源配置 (UI 配置向导): 当前配置 + 模型目录 + 探测能力。"""
        controller = self.context.controller
        state = controller.state
        db = getattr(controller, 'db', None)
        self.require_secretary()
        from pathlib import Path
        from .balance_probe import UNSUPPORTED_HINTS, supported_providers
        from .config import load_model_pool
        from .model_resources import read_config_data
        target = Path(__file__).parent / 'resources.yaml'
        cfg = read_config_data(target)
        catalog: dict = {}
        try:
            pool = load_model_pool()
            for e in pool.models or []:
                catalog.setdefault(e.provider, []).append(e.id)
        except Exception:
            pass
        return {'exists': cfg['exists'], 'data': cfg['data'], 'parse_error': cfg['error'], 'catalog': [{'provider': p, 'models': m} for p, m in sorted(catalog.items())], 'probe_supported': supported_providers(), 'probe_hints': UNSUPPORTED_HINTS}

    def save_resources_config(self, payload: dict) -> dict:
        """保存资源配置并热重载 (UI 配置向导)。

        校验不通过 → 400 携带具体错误; 保存前自动备份至 ~/.lan_mesh/backups/。
        F1: 任意节点均可保存 (不再限 Secretary) — 保存后与全网
        对端自动对齐, 主从无关。
        """
        controller = self.context.controller
        state = controller.state
        db = getattr(controller, 'db', None)
        from pathlib import Path
        from .config import load_model_pool
        from .model_resources import init_resource_manager, save_config, validate_config
        data = payload.get('config')
        if not isinstance(data, dict):
            raise ServiceError(status_code=400, detail='缺少 config 字段')
        if data.get('report_interval') not in (None, ''):
            try:
                data['report_interval'] = float(data['report_interval'])
            except (TypeError, ValueError):
                data.pop('report_interval', None)
        if not (data.get('secretary_url') or '').strip():
            data.pop('secretary_url', None)
        for item in data.get('resources') or []:
            if not isinstance(item, dict):
                continue
            if isinstance(item.get('models'), str):
                item['models'] = [m.strip() for m in item['models'].split(',') if m.strip()]
            for num_key in ('quota', 'alert_threshold', 'expire_at', 'renew_at', 'period_days'):
                if item.get(num_key) in (None, ''):
                    item.pop(num_key, None)
                    continue
                try:
                    item[num_key] = float(item[num_key]) if num_key in ('quota', 'alert_threshold', 'expire_at', 'renew_at') else int(float(item[num_key]))
                except (TypeError, ValueError):
                    pass
            if not (item.get('api_key') or '').strip():
                item.pop('api_key', None)
        errors = validate_config(data)
        if errors:
            raise ServiceError(status_code=400, detail='; '.join(errors))
        target = Path(__file__).parent / 'resources.yaml'
        saved = save_config(target, data)
        if not saved.get('ok'):
            raise ServiceError(status_code=500, detail=f"保存失败: {saved.get('error')}")
        pool = load_model_pool()
        mgr = init_resource_manager(target, pool.models if pool.models else None, controller.db)
        from .model_resources import set_bot_notify_global
        set_bot_notify_global(controller.bot_gateway.notify)
        publish_event('resource_config', {'ok': True, 'pools': len(mgr.list_resources())})
        threading.Thread(target=controller._align_config_with_peers, daemon=True, name='secret-align-save').start()
        return {'ok': True, 'enabled': mgr.enabled, 'pools': len(mgr.list_resources()), 'backup': saved.get('backup', '')}

    def test_resource_key(self, payload: dict) -> dict:
        """单个 API Key 有效性测试 + 余额查询 (UI 一键测试)。"""
        controller = self.context.controller
        state = controller.state
        db = getattr(controller, 'db', None)
        self.require_secretary()
        from .balance_probe import probe_balance
        return probe_balance(payload.get('provider', ''), payload.get('api_key', ''))

    def check_resource_alerts(self) -> dict:
        """R7: 手动触发一轮到期/额度预警检查 (返回新推送的预警)。"""
        controller = self.context.controller
        state = controller.state
        db = getattr(controller, 'db', None)
        self.require_secretary()
        from .model_resources import check_alerts_global, resource_summary
        pushed = check_alerts_global()
        return {'pushed': pushed, 'active': resource_summary().get('alerts', [])}

    def get_rotation_plan(self) -> dict:
        """R5: 多池轮换调度方案 — 逐模型候选池优先级与选中池。"""
        controller = self.context.controller
        state = controller.state
        db = getattr(controller, 'db', None)
        self.require_secretary()
        from .model_resources import rotation_plan_global
        return {'plan': rotation_plan_global()}

    def get_recent_events(self, n: int=20) -> dict:
        """M5: 最近事件查询 (事件总线历史, 供 UI 补拉)。"""
        controller = self.context.controller
        state = controller.state
        db = getattr(controller, 'db', None)
        self.require_secretary()
        return {'events': recent_events(max(1, min(int(n or 20), 100)))}

    def get_role_cards(self) -> dict:
        """M6: 角色卡摘要 (Secretary/PM/Worker 人格单一事实源, 供 UI/quest 页面展示)。"""
        controller = self.context.controller
        state = controller.state
        db = getattr(controller, 'db', None)
        self.require_secretary()
        from .role_cards import list_role_cards
        return {'roles': list_role_cards()}
