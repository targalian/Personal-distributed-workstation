"""ResourceAccountingService: domain behavior separated from the public compatibility facade."""

from __future__ import annotations
from . import model_resources as policy
from .component_service import ComponentService

class ResourceAccountingService(ComponentService):
    """ResourceAccountingService business operations with explicit state ownership."""
    OPERATIONS = ('record_usage', 'get_usage', 'is_available', 'probe_balances', 'summarize')

    def record_usage(self, model_id: str, input_tokens: int, output_tokens: int, usage_id: str='', task_id: str='', project_id: str='') -> dict:
        """记录一次 LLM 调用消耗。

        payg 池按价格目录折算金额, token/coding 池直接计 token 数。
        无资源匹配或未启用 → 不追踪 (返回 tracked=False)。

        Args:
            usage_id: 幂等键 (R3 跨主机上报去重); 留空自动生成。
            task_id/project_id: 成本归因 (R6); 留空时自动取线程级
                归因上下文 (set_usage_context 注入)。

        Returns:
            {"tracked", "resource_id", "plan_type", "consumed", "unit",
             "rate", "alert"?, "duplicate"?}
        """
        if not self.owner._enabled or self.owner._db is None:
            return {'tracked': False}
        pool = self.owner._find_pool(model_id)
        if not pool:
            policy.logger.debug('模型 %s 无关联资源池, 用量未追踪', model_id)
            return {'tracked': False}
        if not task_id or not project_id:
            ctx_task, ctx_proj = policy._get_usage_context()
            task_id = task_id or ctx_task
            project_id = project_id or ctx_proj
        in_tok = max(0, int(input_tokens or 0))
        out_tok = max(0, int(output_tokens or 0))
        if pool.is_payg:
            p_in, p_out = self.owner._prices.get(model_id, (0.0, 0.0))
            consumed = round(in_tok / 1000 * p_in + out_tok / 1000 * p_out, 6)
            unit = pool.unit
        else:
            consumed = in_tok + out_tok
            unit = 'token'
        uid = usage_id or policy.uuid.uuid4().hex
        try:
            inserted = self.owner._db.insert_resource_usage(pool.id, model_id, pool.plan_type, in_tok, out_tok, consumed, usage_id=uid, task_id=task_id, project_id=project_id)
        except Exception as e:
            policy.logger.warning('用量写入失败: %s', e)
            return {'tracked': False}
        if not inserted:
            policy.logger.debug('用量记录 %s 重复上报, 已忽略 (幂等)', uid)
            return {'tracked': True, 'duplicate': True, 'usage_id': uid}
        usage = self.owner.get_usage(pool.id)
        result = {'tracked': True, 'resource_id': pool.id, 'plan_type': pool.plan_type, 'consumed': consumed, 'unit': unit, 'rate': usage.get('rate', 0.0), 'usage_id': uid}
        rate = usage.get('rate', 0.0)
        if rate >= pool.alert_threshold and pool.id not in self.owner._alerted:
            self.owner._alerted.add(pool.id)
            policy.logger.warning('资源池 %s 使用率已达 %.0f%% (阈值 %.0f%%)', pool.id, rate * 100, pool.alert_threshold * 100)
            result['alert'] = True
        return result

    def get_usage(self, resource_id: str) -> dict:
        """资源池当前周期用量。"""
        pool = self.owner._resources.get(resource_id)
        if not pool:
            return {}
        window_start = pool.window_start(policy.time.time())
        agg = {'tokens': 0.0, 'cost': 0.0}
        if self.owner._db:
            try:
                agg = self.owner._db.sum_resource_usage(resource_id, window_start) or agg
            except Exception:
                pass
        used = agg['cost'] if pool.is_payg else agg['tokens']
        rate = used / pool.quota if pool.quota > 0 else 0.0
        return {'resource_id': pool.id, 'plan_type': pool.plan_type, 'quota': pool.quota, 'used': round(used, 6), 'remaining': round(max(0.0, pool.quota - used), 6), 'rate': round(min(rate, 1.0), 4), 'unit': pool.unit if pool.is_payg else 'token', 'status': pool.status, 'expire_at': pool.expire_at, 'renew_at': pool.renew_at}

    def is_available(self, model_id: str) -> bool:
        """模型当前是否可用 (供路由过滤)。

        - 未启用 → 全部放行
        - 无关联池 → strict 模式禁用, 否则放行
        - 池暂停/耗尽/过期 → 禁用
        """
        if not self.owner._enabled:
            return True
        pool = self.owner._find_pool(model_id)
        if not pool:
            return not self.owner._strict
        if pool.status != 'active':
            return False
        if pool.expire_at and policy.time.time() > pool.expire_at:
            return False
        usage = self.owner.get_usage(pool.id)
        if pool.quota > 0 and usage.get('used', 0.0) >= pool.quota:
            return False
        return True

    def probe_balances(self, timeout: float=10.0) -> dict:
        """探测所有资源池的服务商余额 (R2, 自动获取)。

        仅探测配置了 api_key (直填) 或 api_key_env (环境变量) 的池;
        结果缓存到 self._balances, 供 summarize/API 展示。

        Returns:
            {"probed": int, "supported": int, "results": {rid: result}}
        """
        from .balance_probe import probe_resource
        results: dict[str, dict] = {}
        probed = supported = 0
        for rid, pool in self.owner._resources.items():
            has_key = (pool.api_key or '').strip() or (pool.api_key_env or '').strip()
            if not has_key:
                continue
            probed += 1
            res = probe_resource({'id': rid, 'provider': pool.provider, 'api_key_env': pool.api_key_env, 'api_key': pool.api_key}, timeout=timeout)
            results[rid] = res
            if res.get('supported'):
                supported += 1
            if res.get('supported') and res.get('balance') is not None and (float(res.get('balance')) <= 0):
                if pool.status != 'exhausted':
                    policy.logger.warning('资源池 %s 余额为 0, 自动标记 exhausted', rid)
                    pool.status = 'exhausted'
        self.owner._balances = results
        return {'probed': probed, 'supported': supported, 'results': results}

    def summarize(self) -> dict:
        """全池汇总报告 (API / CLI / Web UI 使用)。"""
        return {'enabled': self.owner._enabled, 'strict': self.owner._strict, 'resources': [{**self.owner.get_usage(rid), 'provider': p.provider, 'models': p.models, 'alert_threshold': p.alert_threshold, 'note': p.note, 'balance': self.owner._balances.get(rid, {})} for rid, p in self.owner._resources.items()], 'alerts': list(self.owner._active_alerts)}
