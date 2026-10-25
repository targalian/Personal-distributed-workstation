"""ResourceSelectionService: domain behavior separated from the public compatibility facade."""

from __future__ import annotations
from typing import Any
from . import model_resources as policy
from .component_service import ComponentService

class ResourceSelectionService(ComponentService):
    """ResourceSelectionService business operations with explicit state ownership."""
    OPERATIONS = ('_find_pool', '_pool_priority', '_pool_priority_rule', '_pool_score', '_window_urgency', '_time_bonus', 'set_usage_mode', 'rotation_plan')

    def _find_pool(self, model_id: str) -> policy.Optional[policy.ModelResource]:
        """为模型匹配资源池 (R5/R5-2 轮换调度)。
    
        候选收集: 显式 models 列表匹配优先, 其次按 provider 兜底。
        多池候选时按调度优先级动态选择 (预付费先耗/临期先耗/
        高水位先耗), 而非静态首匹配; 无候选返回 None。
        """
        cands = [p for p in self.owner._resources.values() if model_id in p.models]
        if not cands:
            provider = self.owner._model_provider.get(model_id, '')
            if provider:
                cands = [p for p in self.owner._resources.values() if not p.models and p.provider == provider]
        if not cands:
            return None
        if self.owner._usage_mode == 'batch' and self.owner._rotation_cfg.get('batch_block_subscription'):
            cands = [p for p in cands if p.is_payg]
            if not cands:
                return None
        if len(cands) == 1:
            return cands[0]
        now = policy.time.time()
        active = [p for p in cands if p.status == 'active' and (not p.expire_at or now <= p.expire_at)]
        return max(active or cands, key=lambda p: self.owner._pool_priority(p, model_id))

    def _pool_priority(self, pool: policy.ModelResource, model_id: str='') -> float:
        """R5-2: 资源池轮换调度优先级 (越大越优先消耗其额度)。

        quant 模式 (默认): 量化价值公式 = 基线 + 沉没成本压力 +
        时段折扣 + 临期加压 + 高水位收尾, 分量见 _pool_score;
        quant=false 回退 R5 首版纯规则 (_pool_priority_rule)。
        """
        if not self.owner._rotation_cfg.get('quant', True):
            return self.owner._pool_priority_rule(pool)
        return sum(self.owner._pool_score(pool, model_id).values())

    def _pool_priority_rule(self, pool: policy.ModelResource) -> float:
        """R5 首版纯规则 (quant=false 回退路径, 保留对照)。

        - 预付费计划 (token/coding) 优先于按量消耗 — 不用即沉没成本
        - 临期加压: expire_at 14 天内越近越优先; renew 窗口剩余
          <=3 天加压 (到期前用完)
        - 高水位先耗: 使用率越接近阈值越优先收尾
        """
        score = 5.0 if pool.is_payg else 10.0
        now = policy.time.time()
        if pool.expire_at > 0:
            days = (pool.expire_at - now) / 86400
            if days <= 14:
                score += max(0.0, 14.0 - days)
        if pool.billing_period == 'renew' and pool.renew_at > 0:
            step = max(1, pool.period_days) * 86400
            days_left = (pool.window_start(now) + step - now) / 86400
            if 0 < days_left <= 3:
                score += 3.0
        if pool.quota > 0:
            try:
                score += self.owner.get_usage(pool.id).get('rate', 0.0) * 3.0
            except Exception:
                pass
        return score

    def _pool_score(self, pool: policy.ModelResource, model_id: str='') -> dict:
        """R5-2: 量化评分分量拆解 (供 rotation_plan 展示审计)。

        base      预付费基线 10 (不用即沉没) / 按量基线 5 (现金保留)
        sunk      沉没成本压力 = 剩余额度比例 × 窗口紧迫度 × W_sunk
        time      时段折扣窗口 (按量空闲半价 / 百炼夜间五折) × W_time
        deadline  临期加压 (expire_at 14 天内线性)
        watermark 高水位收尾 = 使用率 × W_watermark
        """
        w = self.owner._rotation_cfg['weights']
        now = policy.time.time()
        d = {'base': 5.0 if pool.is_payg else 10.0, 'sunk': 0.0, 'time': 0.0, 'deadline': 0.0, 'watermark': 0.0}
        if not pool.is_payg:
            rate = 0.0
            if pool.quota > 0:
                try:
                    rate = min(1.0, self.owner.get_usage(pool.id).get('rate', 0.0))
                except Exception:
                    rate = 0.0
            d['sunk'] = round(w['sunk'] * (1.0 - rate) * self.owner._window_urgency(pool, now), 3)
        d['time'] = round(w['time'] * self.owner._time_bonus(pool, model_id), 3)
        if pool.expire_at > 0:
            days = (pool.expire_at - now) / 86400
            if days <= 14:
                d['deadline'] = round(max(0.0, 14.0 - days), 2)
        if pool.quota > 0:
            try:
                d['watermark'] = round(self.owner.get_usage(pool.id).get('rate', 0.0) * w['watermark'], 3)
            except Exception:
                pass
        return d

    def _window_urgency(self, pool: policy.ModelResource, now: float) -> float:
        """计费窗口紧迫度 (0~1): 窗口已逝比例, 越接近重置越紧迫。

        monthly = 当月已逝比例; renew = 周期窗口已逝比例;
        one_time 额度不刷新 → 恒 1.0 (尽早消耗避免浪费)。
        """
        if pool.billing_period == 'monthly':
            dt = policy.datetime.datetime.fromtimestamp(now, policy._BEIJING_TZ)
            cur = policy.datetime.datetime(dt.year, dt.month, 1, tzinfo=policy._BEIJING_TZ)
            if dt.month == 12:
                nxt = policy.datetime.datetime(dt.year + 1, 1, 1, tzinfo=policy._BEIJING_TZ)
            else:
                nxt = policy.datetime.datetime(dt.year, dt.month + 1, 1, tzinfo=policy._BEIJING_TZ)
            total = (nxt - cur).total_seconds()
            return min(1.0, max(0.0, (now - cur.timestamp()) / total))
        if pool.billing_period == 'renew' and pool.renew_at > 0:
            step = max(1, pool.period_days) * 86400
            return min(1.0, max(0.0, (now - pool.window_start(now)) / step))
        return 1.0

    def _time_bonus(self, pool: policy.ModelResource, model_id: str) -> float:
        """R5-2: 时段折扣窗口命中 → 1.0, 否则 0。

        payg: DeepSeek 空闲时段半价 → 鼓励低价窗口消耗现金余额;
        订阅池: 百炼夜间五折模型 (qwen3.8-max 等) 鼓励夜间消耗。
        """
        hour = policy._beijing_now().hour
        cfg = self.owner._rotation_cfg
        if pool.is_payg:
            return 1.0 if policy._hour_in_ranges(hour, cfg['payg_offpeak']) else 0.0
        nd = cfg['night_discount']
        if model_id and pool.provider in nd['providers'] and (model_id in nd['models']) and policy._hour_in_ranges(hour, [tuple(nd['hours'])]):
            return 1.0
        return 0.0

    def set_usage_mode(self, mode: str) -> Any:
        """R5-2: 设置用量场景模式 — interactive (默认) | batch。

        batch 模式配合 rotation.batch_block_subscription=true 时,
        订阅池从候选剔除 (供应商合规红线: 套餐禁非交互批量调用)。
        """
        self.owner._usage_mode = mode if mode in ('interactive', 'batch') else 'interactive'

    def rotation_plan(self) -> list:
        """R5: 模型轮换调度方案 (供 API/Web 展示)。
    
        逐模型列出候选池优先级排序与实际选中的池。
        """
        models: set = set()
        for p in self.owner._resources.values():
            models.update(p.models)
        plan = []
        for m in sorted(models):
            cands = [p for p in self.owner._resources.values() if m in p.models]
            if not cands:
                continue
            ranked = sorted(cands, key=lambda p: self.owner._pool_priority(p, m), reverse=True)
            chosen = self.owner._find_pool(m)
            quant = self.owner._rotation_cfg.get('quant', True)
            plan.append({'model': m, 'chosen': chosen.id if chosen else '', 'pools': [{'id': p.id, 'plan_type': p.plan_type, 'priority': round(self.owner._pool_priority(p, m), 1), 'detail': self.owner._pool_score(p, m) if quant else {}, 'status': p.status} for p in ranked]})
        return plan
