"""ResourceConfigService: domain behavior separated from the public compatibility facade."""

from __future__ import annotations
from typing import Any
from . import model_resources as policy
from .component_service import ComponentService

class ResourceConfigService(ComponentService):
    """ResourceConfigService business operations with explicit state ownership."""
    OPERATIONS = ('load', '_inject_direct_keys', 'list_resources', 'set_pool_status')

    def load(self, yaml_path: policy.Union[str, policy.Path], pool_entries: list=None, db: policy.Optional['Database']=None) -> bool:
        """加载资源配置 + 价格目录。

        Args:
            yaml_path: resources.yaml 路径 (不存在 → disabled no-op)
            pool_entries: model_pool 条目列表 (价格单一事实源)
            db: Database 实例 (用量日志落库)

        Returns:
            是否成功启用
        """
        if not self.owner.stop_reporter():
            raise RuntimeError('资源后台线程尚未退出，暂缓配置热重载')
        self.owner._db = db
        self.owner._stop_evt.set()
        self.owner._alerted = set()
        self.owner._balances = {}
        if pool_entries:
            for e in pool_entries:
                self.owner._prices[e.id] = (e.cost_input_per_1k, e.cost_output_per_1k)
                self.owner._model_provider[e.id] = e.provider
        path = policy.Path(yaml_path)
        if not path.is_file():
            policy.logger.info('未找到 %s, 模型资源管理未启用 (no-op)', path.name)
            self.owner._enabled = False
            return False
        try:
            data = policy.yaml.safe_load(path.read_text(encoding='utf-8')) or {}
        except Exception as e:
            policy.logger.error('资源配置解析失败: %s', e)
            self.owner._enabled = False
            return False
        self.owner._strict = bool(data.get('strict', False))
        self.owner._rotation_cfg = policy._parse_rotation_cfg(data.get('rotation') or {})
        self.owner._usage_mode = 'interactive'
        for mid, price in (data.get('pricing') or {}).items():
            self.owner._prices[mid] = (price.get('input_per_1k', 0.0), price.get('output_per_1k', 0.0))
        self.owner._resources = {}
        for item in data.get('resources') or []:
            rid = item.get('id', '')
            if not rid or item.get('plan_type') not in policy.VALID_PLAN_TYPES:
                policy.logger.warning('资源 %s 配置无效 (plan_type 缺失或非法), 跳过', rid or '?')
                continue
            try:
                self.owner._resources[rid] = policy.ModelResource(**item)
            except Exception as e:
                policy.logger.warning('资源 %s 配置无效, 跳过: %s', rid, e)
        self.owner._enabled = bool(self.owner._resources)
        if self.owner._enabled:
            policy.logger.info('模型资源管理已启用: %d 个资源池, %d 条价格, strict=%s', len(self.owner._resources), len(self.owner._prices), self.owner._strict)
            self.owner._inject_direct_keys()
            self.owner.set_report_target(data.get('secretary_url', ''), float(data.get('report_interval', 60)))
            if data.get('alert_check', True) is not False:
                self.owner.start_alert_checker(float(data.get('alert_interval', 300)))
        return self.owner._enabled

    def _inject_direct_keys(self) -> Any:
        """S1: 将资源池直填 api_key 注入对应环境变量。

        配置值为准 (覆盖同名 env), 不写日志明文; 无 api_key_env
        名的池跳过 (路由仍无法选中, 需向导补全关联)。
        """
        injected = 0
        for pool in self.owner._resources.values():
            key = (pool.api_key or '').strip()
            env_name = (pool.api_key_env or '').strip()
            if not key or not env_name:
                continue
            if policy.os.environ.get(env_name) != key:
                policy.os.environ[env_name] = key
                injected += 1
        if injected:
            policy.logger.info('[S1] 已注入 %d 个直填 API Key 到环境变量', injected)

    def list_resources(self) -> list[dict]:
        """资源池清单 (不含动态用量)。"""
        return [{'id': r.id, 'provider': r.provider, 'plan_type': r.plan_type, 'quota': r.quota, 'unit': r.unit if r.is_payg else 'token', 'billing_period': r.billing_period, 'expire_at': r.expire_at, 'renew_at': r.renew_at, 'models': r.models, 'alert_threshold': r.alert_threshold, 'status': r.status, 'note': r.note} for r in self.owner._resources.values()]

    def set_pool_status(self, rid: str, status: str) -> bool:
        """iter-60: 运行时标记资源池状态 (自愈写动作入口)。

        status 取值: active | paused | exhausted; 未知池或非法状态返回
        False (no-op)。paused/exhausted 池会被路由剔除 (is_available 过滤)。
        """
        pool = self.owner._resources.get(rid)
        if pool is None or status not in ('active', 'paused', 'exhausted'):
            return False
        if pool.status == status:
            return True
        old = pool.status
        pool.status = status
        policy.logger.warning('资源池 %s 状态变更: %s → %s (自愈动作)', rid, old, status)
        return True
