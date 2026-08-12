"""
模型资源管理 — 多主机 / 多 API Key 预算池管理 (R1)

核心概念: 资源池 (Resource Pool) = 一个可计量的预算来源。

三种计划类型 (plan_type):
- payg         按量付费: 预算单位为金额 (usd/cny), 消耗按 model_pool.yaml 价格实时折算
- token_plan   token 包:  预算单位为 token 数, 一次性额度 (可带有效期 expire_at)
- coding_plan  编程订阅:  预算单位为 token 数, 周期性重置 (以 renew_at 为锚点的窗口)

配置: resources.yaml (用户维护, 与 model_pool.yaml 同目录)
用量: SQLite resource_usage_log 表 (每次 LLM 调用记一行, 可审计可聚合)

与 model_router 集成: 资源耗尽/过期/暂停的模型自动从路由候选与降级链剔除。
与 agent_runtime 集成: 每次真实 LLM 调用成功后自动记账。
未配置 resources.yaml 时全局 no-op (向后兼容, 不影响原有功能)。

价格目录: 单一事实源 = model_pool.yaml 的 cost_input_per_1k / cost_output_per_1k;
用户可在 resources.yaml 的 pricing 段为未收录模型补充覆盖。
"""
import datetime
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Optional, Union

import yaml

from .logger import get_logger

if TYPE_CHECKING:
    from .database import Database

logger = get_logger("resources")

VALID_PLAN_TYPES = ("payg", "token_plan", "coding_plan")


@dataclass
class ModelResource:
    """资源池定义 — 对应 resources.yaml 中一个资源条目。"""
    id: str                                   # 资源池 ID (唯一)
    provider: str                             # 服务商 (与 model_pool 条目 provider 一致)
    plan_type: str                            # payg | token_plan | coding_plan
    quota: float                              # 总额度 (payg: 金额; token/coding: token 数)
    unit: str = "usd"                         # payg 金额单位 (usd / cny)
    billing_period: str = "one_time"          # one_time | monthly | renew
    expire_at: float = 0.0                    # 一次性额度有效期 (0 = 不过期)
    renew_at: float = 0.0                     # coding_plan 续费锚点 (unix ts)
    period_days: int = 30                     # renew 周期窗口天数
    api_key_env: str = ""                     # 关联 API Key 环境变量名 (文档用途)
    models: list = field(default_factory=list)  # 关联模型 id 列表 (空 = 按 provider 匹配)
    alert_threshold: float = 0.8              # 使用率告警阈值 (0~1)
    status: str = "active"                    # active | paused | exhausted
    note: str = ""                            # 备注 (套餐名称/购买日期等)

    @property
    def is_payg(self) -> bool:
        return self.plan_type == "payg"

    def window_start(self, now: float) -> float:
        """当前计费窗口起点 (用于聚合周期用量)。"""
        if self.billing_period == "monthly":
            dt = datetime.datetime.fromtimestamp(now)
            return datetime.datetime(dt.year, dt.month, 1).timestamp()
        if self.billing_period == "renew" and self.renew_at > 0:
            step = self.period_days * 86400
            k = int((now - self.renew_at) / step)
            return self.renew_at + k * step
        return 0.0  # one_time: 全量累计


class ModelResourceManager:
    """模型资源管理器 — 台账 + 记账 + 可用性判定 + 报告。"""

    def __init__(self):
        self._resources: dict[str, ModelResource] = {}
        self._prices: dict[str, tuple[float, float]] = {}   # model_id → (in/1k, out/1k)
        self._model_provider: dict[str, str] = {}           # model_id → provider
        self._db = None
        self._enabled = False
        self._alerted: set[str] = set()                     # 已告警资源 (防刷屏)
        self._strict = False                                # strict: 无池模型禁用

    @property
    def enabled(self) -> bool:
        return self._enabled

    # ── 加载 ────────────────────────────────────────────────────

    def load(self, yaml_path: Union[str, Path], pool_entries: list = None,
             db: Optional["Database"] = None) -> bool:
        """加载资源配置 + 价格目录。

        Args:
            yaml_path: resources.yaml 路径 (不存在 → disabled no-op)
            pool_entries: model_pool 条目列表 (价格单一事实源)
            db: Database 实例 (用量日志落库)

        Returns:
            是否成功启用
        """
        self._db = db
        if pool_entries:
            for e in pool_entries:
                self._prices[e.id] = (e.cost_input_per_1k, e.cost_output_per_1k)
                self._model_provider[e.id] = e.provider

        path = Path(yaml_path)
        if not path.is_file():
            logger.info("未找到 %s, 模型资源管理未启用 (no-op)", path.name)
            self._enabled = False
            return False
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception as e:
            logger.error("资源配置解析失败: %s", e)
            self._enabled = False
            return False

        self._strict = bool(data.get("strict", False))

        # 用户定价覆盖 (model_pool 未收录的模型)
        for mid, price in (data.get("pricing") or {}).items():
            self._prices[mid] = (price.get("input_per_1k", 0.0),
                                 price.get("output_per_1k", 0.0))

        self._resources = {}
        for item in data.get("resources") or []:
            rid = item.get("id", "")
            if not rid or item.get("plan_type") not in VALID_PLAN_TYPES:
                logger.warning("资源 %s 配置无效 (plan_type 缺失或非法), 跳过", rid or "?")
                continue
            try:
                self._resources[rid] = ModelResource(**item)
            except Exception as e:
                logger.warning("资源 %s 配置无效, 跳过: %s", rid, e)

        self._enabled = bool(self._resources)
        if self._enabled:
            logger.info("模型资源管理已启用: %d 个资源池, %d 条价格, strict=%s",
                        len(self._resources), len(self._prices), self._strict)
        return self._enabled

    # ── 台账 ────────────────────────────────────────────────────

    def list_resources(self) -> list[dict]:
        """资源池清单 (不含动态用量)。"""
        return [
            {
                "id": r.id, "provider": r.provider, "plan_type": r.plan_type,
                "quota": r.quota, "unit": r.unit if r.is_payg else "token",
                "billing_period": r.billing_period, "expire_at": r.expire_at,
                "renew_at": r.renew_at, "models": r.models,
                "alert_threshold": r.alert_threshold, "status": r.status,
                "note": r.note,
            }
            for r in self._resources.values()
        ]

    def _find_pool(self, model_id: str) -> Optional[ModelResource]:
        """为模型匹配资源池: 显式 models 列表优先, 其次按 provider 兜底。"""
        for pool in self._resources.values():
            if model_id in pool.models:
                return pool
        provider = self._model_provider.get(model_id, "")
        if provider:
            for pool in self._resources.values():
                if not pool.models and pool.provider == provider:
                    return pool
        return None

    # ── 记账 ────────────────────────────────────────────────────

    def record_usage(self, model_id: str, input_tokens: int,
                     output_tokens: int) -> dict:
        """记录一次 LLM 调用消耗。

        payg 池按价格目录折算金额, token/coding 池直接计 token 数。
        无资源匹配或未启用 → 不追踪 (返回 tracked=False)。

        Returns:
            {"tracked", "resource_id", "plan_type", "consumed", "unit",
             "rate", "alert"?}
        """
        if not self._enabled or self._db is None:
            return {"tracked": False}
        pool = self._find_pool(model_id)
        if not pool:
            logger.debug("模型 %s 无关联资源池, 用量未追踪", model_id)
            return {"tracked": False}

        in_tok = max(0, int(input_tokens or 0))
        out_tok = max(0, int(output_tokens or 0))
        if pool.is_payg:
            p_in, p_out = self._prices.get(model_id, (0.0, 0.0))
            consumed = round(in_tok / 1000 * p_in + out_tok / 1000 * p_out, 6)
            unit = pool.unit
        else:
            consumed = in_tok + out_tok
            unit = "token"

        try:
            self._db.insert_resource_usage(
                pool.id, model_id, pool.plan_type, in_tok, out_tok, consumed)
        except Exception as e:
            logger.warning("用量写入失败: %s", e)
            return {"tracked": False}

        usage = self.get_usage(pool.id)
        result = {
            "tracked": True, "resource_id": pool.id,
            "plan_type": pool.plan_type, "consumed": consumed, "unit": unit,
            "rate": usage.get("rate", 0.0),
        }
        rate = usage.get("rate", 0.0)
        if rate >= pool.alert_threshold and pool.id not in self._alerted:
            self._alerted.add(pool.id)
            logger.warning("资源池 %s 使用率已达 %.0f%% (阈值 %.0f%%)",
                           pool.id, rate * 100, pool.alert_threshold * 100)
            result["alert"] = True
        return result

    def get_usage(self, resource_id: str) -> dict:
        """资源池当前周期用量。"""
        pool = self._resources.get(resource_id)
        if not pool:
            return {}
        window_start = pool.window_start(time.time())
        agg = {"tokens": 0.0, "cost": 0.0}
        if self._db:
            try:
                agg = self._db.sum_resource_usage(resource_id, window_start) or agg
            except Exception:
                pass
        used = agg["cost"] if pool.is_payg else agg["tokens"]
        rate = used / pool.quota if pool.quota > 0 else 0.0
        return {
            "resource_id": pool.id,
            "plan_type": pool.plan_type,
            "quota": pool.quota,
            "used": round(used, 6),
            "remaining": round(max(0.0, pool.quota - used), 6),
            "rate": round(min(rate, 1.0), 4),
            "unit": pool.unit if pool.is_payg else "token",
            "status": pool.status,
            "expire_at": pool.expire_at,
            "renew_at": pool.renew_at,
        }

    # ── 可用性 ──────────────────────────────────────────────────

    def is_available(self, model_id: str) -> bool:
        """模型当前是否可用 (供路由过滤)。

        - 未启用 → 全部放行
        - 无关联池 → strict 模式禁用, 否则放行
        - 池暂停/耗尽/过期 → 禁用
        """
        if not self._enabled:
            return True
        pool = self._find_pool(model_id)
        if not pool:
            return not self._strict
        if pool.status != "active":
            return False
        if pool.expire_at and time.time() > pool.expire_at:
            return False
        usage = self.get_usage(pool.id)
        if pool.quota > 0 and usage.get("used", 0.0) >= pool.quota:
            return False
        return True

    # ── 报告 ────────────────────────────────────────────────────

    def summarize(self) -> dict:
        """全池汇总报告 (API / CLI / Web UI 使用)。"""
        return {
            "enabled": self._enabled,
            "strict": self._strict,
            "resources": [
                {**self.get_usage(rid), "provider": p.provider,
                 "models": p.models, "alert_threshold": p.alert_threshold,
                 "note": p.note}
                for rid, p in self._resources.items()
            ],
        }


# ── 全局单例 + 轻量钩子 (供 agent_runtime / model_router 无侵入调用) ──

_mgr = ModelResourceManager()


def init_resource_manager(yaml_path: Union[str, Path] = None,
                          pool_entries: list = None,
                          db: Optional["Database"] = None) -> ModelResourceManager:
    """启动时调用一次, 加载资源配置。"""
    if yaml_path:
        _mgr.load(yaml_path, pool_entries, db)
    return _mgr


def record_usage_global(model_id: str, input_tokens: int, output_tokens: int) -> dict:
    """记账钩子 — 每次真实 LLM 调用成功后调用, 异常不影响主流程。"""
    try:
        return _mgr.record_usage(model_id, input_tokens, output_tokens)
    except Exception:
        return {"tracked": False}


def resource_available(model_id: str) -> bool:
    """可用性钩子 — 路由过滤用, 未启用时放行。"""
    try:
        return _mgr.is_available(model_id)
    except Exception:
        return True


def resource_summary() -> dict:
    """汇总钩子 — API 端点用。"""
    try:
        return _mgr.summarize()
    except Exception:
        return {"enabled": False, "resources": []}
