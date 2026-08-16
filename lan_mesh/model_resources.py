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

轮换调度 (R5 → R5-2): 多池候选按量化价值公式排序 — 订阅池沉没成本
压力 (剩余额度 × 窗口紧迫度) + 时段折扣窗口 + 临期/高水位加压;
按量池保守保留, 仅在空闲半价窗口加压。供应商合规红线: batch 模式下
可配置剔除订阅池 (套餐禁止非交互式批量调用)。

价格目录: 单一事实源 = model_pool.yaml 的 cost_input_per_1k / cost_output_per_1k;
用户可在 resources.yaml 的 pricing 段为未收录模型补充覆盖。
"""
import copy
import datetime
import json
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Optional, Union

import yaml

from .logger import get_logger

if TYPE_CHECKING:
    from .database import Database

logger = get_logger("resources")

VALID_PLAN_TYPES = ("payg", "token_plan", "coding_plan")

# ── R5-2: 轮换量化默认参数 ───────────────────────────────────────────────
# 供应商能力依据 docs/reference/vendor-capability/ (2026-08):
# - DeepSeek 按量: 高峰 9-12/14-18 (北京时间), 空闲时段半价
# - 百炼 Token Plan: qwen3.8-max / deepseek-v4-pro-0813 夜间 22-08 五折
# - 订阅套餐禁止 API/自动化批量调用 (合规红线, batch_block_subscription)
ROTATION_DEFAULTS = {
    "quant": True,                       # false → 回退 R5 首版纯规则
    "batch_block_subscription": False,   # batch 模式剔除订阅池 (合规开关)
    "weights": {"sunk": 4.0, "time": 1.5, "watermark": 3.0},
    "payg_offpeak": [[0, 9], [12, 14], [18, 24]],   # DeepSeek 空闲时段
    "night_discount": {
        "hours": [22, 8],
        "models": ["qwen3.8-max", "deepseek-v4-pro-0813"],
        "providers": ["aliyun-tokenplan"],
    },
}

_BEIJING_TZ = datetime.timezone(datetime.timedelta(hours=8))


def _beijing_now() -> datetime.datetime:
    """当前北京时间 (UTC+8) — 时段折扣判定基准 (可 monkeypatch)。"""
    return datetime.datetime.now(_BEIJING_TZ)


def _hour_in_ranges(hour: int, ranges) -> bool:
    """小时是否落在 [start, end) 区间列表内 (支持跨零点区间)。"""
    for start, end in ranges or []:
        if start <= end:
            if start <= hour < end:
                return True
        elif hour >= start or hour < end:   # 跨零点 (如 22-08)
            return True
    return False


def _parse_rotation_cfg(rc: dict) -> dict:
    """合并用户 rotation 配置到默认值 (load 时调用)。"""
    cfg = copy.deepcopy(ROTATION_DEFAULTS)
    if not isinstance(rc, dict):
        return cfg
    cfg["quant"] = bool(rc.get("quant", True))
    cfg["batch_block_subscription"] = bool(
        rc.get("batch_block_subscription", False))
    for k, v in (rc.get("weights") or {}).items():
        if k in cfg["weights"]:
            cfg["weights"][k] = float(v)
    if rc.get("payg_offpeak"):
        cfg["payg_offpeak"] = [[int(a), int(b)]
                               for a, b in rc["payg_offpeak"]]
    nd = rc.get("night_discount") or {}
    if nd.get("hours"):
        cfg["night_discount"]["hours"] = [int(x) for x in nd["hours"]]
    if nd.get("models"):
        cfg["night_discount"]["models"] = [str(m) for m in nd["models"]]
    if nd.get("providers"):
        cfg["night_discount"]["providers"] = [str(p)
                                              for p in nd["providers"]]
    return cfg


def _build_ws_url(http_url: str, token: str = "") -> str:
    """M5-2: Secretary http(s) 地址 → /ws/worker 直推地址。

    mesh_token 以 query 参数携带 (握手阶段无 header 注入点);
    空地址返回空串。
    """
    u = (http_url or "").strip().rstrip("/")
    if u.startswith("https://"):
        u = "wss://" + u[len("https://"):]
    elif u.startswith("http://"):
        u = "ws://" + u[len("http://"):]
    else:
        return ""
    u += "/ws/worker"
    if token:
        from urllib.parse import quote
        u += f"?token={quote(str(token), safe='')}"
    return u


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
    api_key_env: str = ""                     # 关联 API Key 环境变量名
    api_key: str = ""                         # API Key 直填值 (R4/S1: 加载时自动
                                              # 注入 api_key_env 同名环境变量;
                                              # resources.yaml 已 gitignore)
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
        self._balances: dict[str, dict] = {}                # resource_id → 探测结果 (R2)
        self._secretary_url = ""                            # 上报目标 (R3)
        self._report_interval = 60.0                        # 上报周期 (秒, R3)
        self._reporter = None                               # 上报线程 (R3)
        # ── M5-2: WS 直推 (Worker → Secretary 实时用量通道) ──
        self._ws_push_thread = None                         # WS 推送线程
        self._ws_url = ""                                   # ws://…/ws/worker?token=…
        self._ws_token = ""                                 # mesh_token (鉴权参数)
        self._ws_last_ok = 0.0                              # 最近一次 WS 成功轮次 ts
        self._ws_push_interval = 3.0                        # WS 轮询推送周期 (秒)
        # ── R7: 到期/额度预警 ──
        self._bot_notify = None                             # Bot 推送回调 (可选注入)
        self._alert_state: dict[tuple, int] = {}            # (rid,kind) → 已推档位 (仅升级重推)
        self._active_alerts: list[dict] = []                # 最近一轮预警 (summarize/API 展示)
        self._alert_checker = None                          # 预警检查线程
        self._alert_interval = 300.0                        # 检查周期 (秒)
        self._stop_evt = threading.Event()
        # ── R5-2: 轮换量化 ──
        self._rotation_cfg = copy.deepcopy(ROTATION_DEFAULTS)
        self._usage_mode = "interactive"   # interactive | batch (合规红线)

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
        # R4: 热重载安全 — 先停旧上报/检查线程, 重置动态状态
        self.stop_reporter()
        self._stop_evt.set()  # 通知旧检查线程退出 (下方按需重启)
        self._alerted = set()
        self._balances = {}
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
        # R5-2: 轮换量化配置 (rotation 段, 缺省用 ROTATION_DEFAULTS)
        self._rotation_cfg = _parse_rotation_cfg(
            data.get("rotation") or {})
        self._usage_mode = "interactive"

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
            # S1: 直填 key 注入环境变量 (model_router/agent_runtime 取值零改动,
            # 密钥自动分发到节点后无需手工维护 .env)
            self._inject_direct_keys()
            # R3: 配置了 Secretary 地址 → 启用用量上报 (Worker 主机)
            self.set_report_target(data.get("secretary_url", ""),
                                   float(data.get("report_interval", 60)))
            # R7: 启动到期/额度预警后台检查 (alert_check: false 可禁用)
            if data.get("alert_check", True) is not False:
                self.start_alert_checker(float(data.get("alert_interval", 300)))
        return self._enabled

    def _inject_direct_keys(self):
        """S1: 将资源池直填 api_key 注入对应环境变量。

        配置值为准 (覆盖同名 env), 不写日志明文; 无 api_key_env
        名的池跳过 (路由仍无法选中, 需向导补全关联)。
        """
        injected = 0
        for pool in self._resources.values():
            key = (pool.api_key or "").strip()
            env_name = (pool.api_key_env or "").strip()
            if not key or not env_name:
                continue
            if os.environ.get(env_name) != key:
                os.environ[env_name] = key
                injected += 1
        if injected:
            logger.info("[S1] 已注入 %d 个直填 API Key 到环境变量", injected)

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
        """为模型匹配资源池 (R5/R5-2 轮换调度)。
    
        候选收集: 显式 models 列表匹配优先, 其次按 provider 兜底。
        多池候选时按调度优先级动态选择 (预付费先耗/临期先耗/
        高水位先耗), 而非静态首匹配; 无候选返回 None。
        """
        cands = [p for p in self._resources.values()
                 if model_id in p.models]
        if not cands:
            provider = self._model_provider.get(model_id, "")
            if provider:
                cands = [p for p in self._resources.values()
                         if not p.models and p.provider == provider]
        if not cands:
            return None
        # R5-2: batch 场景合规红线 — 订阅池 (token/coding) 禁批量调用
        if (self._usage_mode == "batch"
                and self._rotation_cfg.get("batch_block_subscription")):
            cands = [p for p in cands if p.is_payg]
            if not cands:
                return None
        if len(cands) == 1:
            return cands[0]
        # R5: 优先在 active 且未过期的池中轮换
        now = time.time()
        active = [p for p in cands if p.status == "active"
                  and (not p.expire_at or now <= p.expire_at)]
        return max(active or cands,
                   key=lambda p: self._pool_priority(p, model_id))
    
    def _pool_priority(self, pool: ModelResource,
                       model_id: str = "") -> float:
        """R5-2: 资源池轮换调度优先级 (越大越优先消耗其额度)。

        quant 模式 (默认): 量化价值公式 = 基线 + 沉没成本压力 +
        时段折扣 + 临期加压 + 高水位收尾, 分量见 _pool_score;
        quant=false 回退 R5 首版纯规则 (_pool_priority_rule)。
        """
        if not self._rotation_cfg.get("quant", True):
            return self._pool_priority_rule(pool)
        return sum(self._pool_score(pool, model_id).values())

    def _pool_priority_rule(self, pool: ModelResource) -> float:
        """R5 首版纯规则 (quant=false 回退路径, 保留对照)。

        - 预付费计划 (token/coding) 优先于按量消耗 — 不用即沉没成本
        - 临期加压: expire_at 14 天内越近越优先; renew 窗口剩余
          <=3 天加压 (到期前用完)
        - 高水位先耗: 使用率越接近阈值越优先收尾
        """
        score = 5.0 if pool.is_payg else 10.0
        now = time.time()
        if pool.expire_at > 0:
            days = (pool.expire_at - now) / 86400
            if days <= 14:
                score += max(0.0, 14.0 - days)
        if pool.billing_period == "renew" and pool.renew_at > 0:
            step = max(1, pool.period_days) * 86400
            days_left = (pool.window_start(now) + step - now) / 86400
            if 0 < days_left <= 3:
                score += 3.0
        if pool.quota > 0:
            try:
                score += self.get_usage(pool.id).get("rate", 0.0) * 3.0
            except Exception:
                pass
        return score

    def _pool_score(self, pool: ModelResource,
                    model_id: str = "") -> dict:
        """R5-2: 量化评分分量拆解 (供 rotation_plan 展示审计)。

        base      预付费基线 10 (不用即沉没) / 按量基线 5 (现金保留)
        sunk      沉没成本压力 = 剩余额度比例 × 窗口紧迫度 × W_sunk
        time      时段折扣窗口 (按量空闲半价 / 百炼夜间五折) × W_time
        deadline  临期加压 (expire_at 14 天内线性)
        watermark 高水位收尾 = 使用率 × W_watermark
        """
        w = self._rotation_cfg["weights"]
        now = time.time()
        d = {"base": 5.0 if pool.is_payg else 10.0, "sunk": 0.0,
             "time": 0.0, "deadline": 0.0, "watermark": 0.0}
        if not pool.is_payg:
            rate = 0.0
            if pool.quota > 0:
                try:
                    rate = min(1.0,
                               self.get_usage(pool.id).get("rate", 0.0))
                except Exception:
                    rate = 0.0
            d["sunk"] = round(w["sunk"] * (1.0 - rate)
                              * self._window_urgency(pool, now), 3)
        d["time"] = round(w["time"] * self._time_bonus(pool, model_id), 3)
        if pool.expire_at > 0:
            days = (pool.expire_at - now) / 86400
            if days <= 14:
                d["deadline"] = round(max(0.0, 14.0 - days), 2)
        if pool.quota > 0:
            try:
                d["watermark"] = round(
                    self.get_usage(pool.id).get("rate", 0.0)
                    * w["watermark"], 3)
            except Exception:
                pass
        return d

    def _window_urgency(self, pool: ModelResource, now: float) -> float:
        """计费窗口紧迫度 (0~1): 窗口已逝比例, 越接近重置越紧迫。

        monthly = 当月已逝比例; renew = 周期窗口已逝比例;
        one_time 额度不刷新 → 恒 1.0 (尽早消耗避免浪费)。
        """
        if pool.billing_period == "monthly":
            dt = datetime.datetime.fromtimestamp(now, _BEIJING_TZ)
            cur = datetime.datetime(dt.year, dt.month, 1,
                                    tzinfo=_BEIJING_TZ)
            if dt.month == 12:
                nxt = datetime.datetime(dt.year + 1, 1, 1,
                                        tzinfo=_BEIJING_TZ)
            else:
                nxt = datetime.datetime(dt.year, dt.month + 1, 1,
                                        tzinfo=_BEIJING_TZ)
            total = (nxt - cur).total_seconds()
            return min(1.0, max(0.0, (now - cur.timestamp()) / total))
        if pool.billing_period == "renew" and pool.renew_at > 0:
            step = max(1, pool.period_days) * 86400
            return min(1.0, max(0.0,
                                (now - pool.window_start(now)) / step))
        return 1.0

    def _time_bonus(self, pool: ModelResource, model_id: str) -> float:
        """R5-2: 时段折扣窗口命中 → 1.0, 否则 0。

        payg: DeepSeek 空闲时段半价 → 鼓励低价窗口消耗现金余额;
        订阅池: 百炼夜间五折模型 (qwen3.8-max 等) 鼓励夜间消耗。
        """
        hour = _beijing_now().hour
        cfg = self._rotation_cfg
        if pool.is_payg:
            return 1.0 if _hour_in_ranges(
                hour, cfg["payg_offpeak"]) else 0.0
        nd = cfg["night_discount"]
        if (model_id and pool.provider in nd["providers"]
                and model_id in nd["models"]
                and _hour_in_ranges(hour, [tuple(nd["hours"])])):
            return 1.0
        return 0.0

    def set_usage_mode(self, mode: str):
        """R5-2: 设置用量场景模式 — interactive (默认) | batch。

        batch 模式配合 rotation.batch_block_subscription=true 时,
        订阅池从候选剔除 (供应商合规红线: 套餐禁非交互批量调用)。
        """
        self._usage_mode = (mode if mode in ("interactive", "batch")
                            else "interactive")
    
    def rotation_plan(self) -> list:
        """R5: 模型轮换调度方案 (供 API/Web 展示)。
    
        逐模型列出候选池优先级排序与实际选中的池。
        """
        models: set = set()
        for p in self._resources.values():
            models.update(p.models)
        plan = []
        for m in sorted(models):
            cands = [p for p in self._resources.values() if m in p.models]
            if not cands:
                continue
            ranked = sorted(cands,
                            key=lambda p: self._pool_priority(p, m),
                            reverse=True)
            chosen = self._find_pool(m)
            quant = self._rotation_cfg.get("quant", True)
            plan.append({
                "model": m,
                "chosen": chosen.id if chosen else "",
                "pools": [
                    {"id": p.id, "plan_type": p.plan_type,
                     "priority": round(self._pool_priority(p, m), 1),
                     "detail": (self._pool_score(p, m) if quant else {}),
                     "status": p.status}
                    for p in ranked
                ],
            })
        return plan

    # ── 记账 ────────────────────────────────────────────────────

    def record_usage(self, model_id: str, input_tokens: int,
                     output_tokens: int, usage_id: str = "",
                     task_id: str = "", project_id: str = "") -> dict:
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
        if not self._enabled or self._db is None:
            return {"tracked": False}
        pool = self._find_pool(model_id)
        if not pool:
            logger.debug("模型 %s 无关联资源池, 用量未追踪", model_id)
            return {"tracked": False}

        # R6: 显式参数优先, 否则回退线程级归因上下文
        if not task_id or not project_id:
            ctx_task, ctx_proj = _get_usage_context()
            task_id = task_id or ctx_task
            project_id = project_id or ctx_proj

        in_tok = max(0, int(input_tokens or 0))
        out_tok = max(0, int(output_tokens or 0))
        if pool.is_payg:
            p_in, p_out = self._prices.get(model_id, (0.0, 0.0))
            consumed = round(in_tok / 1000 * p_in + out_tok / 1000 * p_out, 6)
            unit = pool.unit
        else:
            consumed = in_tok + out_tok
            unit = "token"

        uid = usage_id or uuid.uuid4().hex
        try:
            inserted = self._db.insert_resource_usage(
                pool.id, model_id, pool.plan_type, in_tok, out_tok,
                consumed, usage_id=uid, task_id=task_id,
                project_id=project_id)
        except Exception as e:
            logger.warning("用量写入失败: %s", e)
            return {"tracked": False}
        if not inserted:
            logger.debug("用量记录 %s 重复上报, 已忽略 (幂等)", uid)
            return {"tracked": True, "duplicate": True, "usage_id": uid}

        usage = self.get_usage(pool.id)
        result = {
            "tracked": True, "resource_id": pool.id,
            "plan_type": pool.plan_type, "consumed": consumed, "unit": unit,
            "rate": usage.get("rate", 0.0), "usage_id": uid,
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

    def probe_balances(self, timeout: float = 10.0) -> dict:
        """探测所有资源池的服务商余额 (R2, 自动获取)。

        仅探测配置了 api_key (直填) 或 api_key_env (环境变量) 的池;
        结果缓存到 self._balances, 供 summarize/API 展示。

        Returns:
            {"probed": int, "supported": int, "results": {rid: result}}
        """
        from .balance_probe import probe_resource

        results: dict[str, dict] = {}
        probed = supported = 0
        for rid, pool in self._resources.items():
            # R4: api_key 直填 或 api_key_env 任一有值即可探测
            has_key = ((pool.api_key or "").strip()
                       or (pool.api_key_env or "").strip())
            if not has_key:
                continue
            probed += 1
            res = probe_resource({
                "id": rid, "provider": pool.provider,
                "api_key_env": pool.api_key_env,
                "api_key": pool.api_key,
            }, timeout=timeout)
            results[rid] = res
            if res.get("supported"):
                supported += 1
            # 探测成功且余额已耗尽 → 自动置为 exhausted (供路由剔除)
            if res.get("supported") and res.get("balance") is not None \
                    and float(res.get("balance")) <= 0:
                if pool.status != "exhausted":
                    logger.warning("资源池 %s 余额为 0, 自动标记 exhausted", rid)
                    pool.status = "exhausted"
        self._balances = results
        return {"probed": probed, "supported": supported, "results": results}

    # ── R3: 跨主机用量上报 (Worker → Secretary) ──────────────────

    def set_report_target(self, url: str, interval: float = 60.0,
                          token: str = "") -> bool:
        """设置/启动用量上报目标 (Worker 主机配置或运行时注入)。

        Args:
            url: Secretary 站点地址 (空 → 不启用上报)
            interval: 上报周期 (秒)
            token: mesh_token (M5-2: 非空时启用 WS 直推通道鉴权)

        Returns:
            是否已启用上报线程
        """
        target = (url or "").strip().rstrip("/")
        self._secretary_url = target
        self._report_interval = max(5.0, float(interval or 60.0))
        # M5-2: 派生 WS 直推地址 (token 变化时即时刷新; 未传 token
        # 沿用旧值, 支持 yaml 配置与注册注入两种来源交替)
        if token:
            self._ws_token = str(token)
        ws_url = _build_ws_url(target, self._ws_token)
        if ws_url:
            self._ws_url = ws_url
        if not target or not self._enabled or self._db is None:
            return False
        self._stop_evt.clear()
        if not (self._reporter and self._reporter.is_alive()):
            self._reporter = threading.Thread(
                target=self._report_loop, name="resource-usage-reporter",
                daemon=True)
            self._reporter.start()
            logger.info("[resources] 用量上报已启用 → %s (周期 %.0fs)",
                        target, self._report_interval)
        # M5-2: WS 直推线程 (幂等启动; 断线期间 HTTP 批量兜底不变)
        if self._ws_url and not (
                self._ws_push_thread and self._ws_push_thread.is_alive()):
            self._ws_push_thread = threading.Thread(
                target=self._ws_push_loop,
                name="resource-usage-ws-push", daemon=True)
            self._ws_push_thread.start()
            logger.info("[resources] WS 直推已启用 → %s", self._ws_url)
        return True

    def stop_reporter(self):
        """停止上报线程 (含 M5-2 WS 推送线程, 共用 _stop_evt)。"""
        self._stop_evt.set()

    def report_once(self, batch: int = 200) -> dict:
        """执行一轮上报 (同步, 供测试/手动触发)。

        未上报记录 → POST 到 Secretary /api/resources/usage (批量),
        成功后标记 reported; 失败不推进游标, 下轮重试 (离线容错)。

        Returns:
            {"reported": 条数, "duplicate": 重复条数, "error"?}
        """
        if not self._secretary_url or self._db is None:
            return {"reported": 0, "error": "no_report_target"}
        # M5-2: WS 直推通道健康 (最近一轮成功) → 跳过 HTTP 批量,
        # 避免双通道重复推送 (Secretary 按 usage_id 幂等去重, 重复亦无害)
        if self._ws_last_ok and (
                time.time() - self._ws_last_ok
                < max(30.0, self._report_interval)):
            return {"reported": 0, "via": "ws"}
        try:
            rows = self._db.query_unreported_usage(batch)
        except Exception as e:
            return {"reported": 0, "error": f"query_failed: {e}"}
        if not rows:
            return {"reported": 0}
        payload = {
            "records": [
                {"usage_id": r["usage_id"], "model": r["model_id"],
                 "input_tokens": r["input_tokens"],
                 "output_tokens": r["output_tokens"],
                 "task_id": r.get("task_id", ""),
                 "project_id": r.get("project_id", "")}
                for r in rows
            ]
        }
        try:
            import requests
            resp = requests.post(
                f"{self._secretary_url}/api/resources/usage",
                json=payload, timeout=10)
            resp.raise_for_status()
            body = resp.json() if resp.text else {}
        except Exception as e:
            logger.warning("[resources] 用量上报失败 (%d 条, 下轮重试): %s",
                           len(rows), e)
            return {"reported": 0, "pending": len(rows), "error": str(e)}
        self._db.mark_usage_reported([r["id"] for r in rows])
        dup = int(body.get("duplicate", 0))
        logger.info("[resources] 用量已上报 Secretary: %d 条 (重复 %d)",
                    len(rows), dup)
        return {"reported": len(rows), "duplicate": dup}

    def _report_loop(self):
        """上报线程主循环 — 异常隔离, 永不退出 (直到 stop)。"""
        while not self._stop_evt.wait(self._report_interval):
            try:
                self.report_once()
            except Exception as e:  # 双保险 (report_once 内部已兜底)
                logger.warning("[resources] 上报轮次异常: %s", e)

    # ── M5-2: WS 直推 (实时用量通道, HTTP 批量兜底) ────────────

    def _push_once_ws(self, conn) -> bool:
        """一轮 WS 推送: 查未上报记录 → 发批 → 等 ack 推游标。

        Returns:
            True  — 连接可继续使用 (含空轮次/ack 超时重试/被拒暂缓)
            False — 连接已坏或不可用, 外层断开重连
        """
        if self._db is None:
            return False
        try:
            rows = self._db.query_unreported_usage(200)
        except Exception as e:
            logger.warning("[resources] WS 推送查询失败: %s", e)
            return not self._stop_evt.wait(self._ws_push_interval)
        if not rows:
            self._ws_last_ok = time.time()
            return not self._stop_evt.wait(self._ws_push_interval)
        payload = {
            "type": "usage_batch",
            "records": [
                {"usage_id": r["usage_id"], "model": r["model_id"],
                 "input_tokens": r["input_tokens"],
                 "output_tokens": r["output_tokens"],
                 "task_id": r.get("task_id", ""),
                 "project_id": r.get("project_id", "")}
                for r in rows
            ],
        }
        try:
            conn.send(json.dumps(payload))
            raw = conn.recv(timeout=10)
        except TimeoutError:
            # ack 超时: 不推游标, 下轮重发 (Secretary 幂等去重)
            logger.debug("[resources] WS ack 超时, %d 条下轮重发",
                         len(rows))
            return True
        except Exception as e:
            logger.warning("[resources] WS 收发失败: %s", e)
            return False
        try:
            ack = json.loads(raw if isinstance(raw, str)
                             else raw.decode("utf-8"))
        except Exception:
            logger.warning("[resources] WS ack 解析失败")
            return False
        if isinstance(ack, dict) and ack.get("ok"):
            self._db.mark_usage_reported([r["id"] for r in rows])
            self._ws_last_ok = time.time()
            logger.info("[resources] 用量已 WS 直推 Secretary: %d 条 "
                        "(重复 %d)", len(rows),
                        int(ack.get("duplicate", 0)))
            return True
        # Secretary 拒绝 (未激活等): 不推游标, 暂缓等待 HTTP 兜底接手
        logger.debug("[resources] WS 推送被拒: %s",
                     ack.get("error", "") if isinstance(ack, dict) else "?")
        return not self._stop_evt.wait(self._report_interval)

    def _ws_push_loop(self):
        """M5-2: WS 直推线程主循环 — 实时推送未上报用量。

        连接失败/断开按指数退避重连 (5s→60s 封顶); 断线期间记录
        留存本地库, report_once (HTTP 批量) 自动接手兜底。
        异常隔离, 直到 stop 才退出。
        """
        try:
            from websockets.sync.client import connect as ws_connect
        except Exception as e:
            logger.warning("[resources] websockets 不可用, WS 直推停用 "
                           "(HTTP 批量兜底仍有效): %s", e)
            return
        backoff = 5.0
        while not self._stop_evt.is_set():
            url = self._ws_url
            if not url:
                if self._stop_evt.wait(10.0):
                    return
                continue
            try:
                conn = ws_connect(url, open_timeout=5)
            except Exception as e:
                logger.debug("[resources] WS 直推连接失败 "
                             "(%.0fs 后重试): %s", backoff, e)
                if self._stop_evt.wait(backoff):
                    return
                backoff = min(backoff * 2, 60.0)
                continue
            backoff = 5.0
            self._ws_last_ok = time.time()
            logger.info("[resources] WS 直推通道已建立")
            try:
                while not self._stop_evt.is_set():
                    if not self._push_once_ws(conn):
                        break
            except Exception as e:
                logger.warning("[resources] WS 直推轮次异常: %s", e)
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
            logger.info("[resources] WS 直推断开, 稍后重连")
            self._stop_evt.wait(5.0)

    def summarize(self) -> dict:
        """全池汇总报告 (API / CLI / Web UI 使用)。"""
        return {
            "enabled": self._enabled,
            "strict": self._strict,
            "resources": [
                {**self.get_usage(rid), "provider": p.provider,
                 "models": p.models, "alert_threshold": p.alert_threshold,
                 "note": p.note,
                 "balance": self._balances.get(rid, {})}
                for rid, p in self._resources.items()
            ],
            "alerts": list(self._active_alerts),  # R7: 最近一轮预警
        }

    # ── R7: 到期/额度预警 ────────────────────────────────

    _ALERT_EVENT = {1: "resource_alert_low", 2: "resource_alert",
                    3: "resource_alert_high"}

    def set_bot_notify(self, callback: Optional[Callable]) -> None:
        """注入 Bot 推送回调 fn(event_type, data); 未注入时仅日志+内存记录。"""
        self._bot_notify = callback

    def check_alerts(self, now: float = None) -> list:
        """扫描全部资源池, 生成到期/额度/重置预警。

        规则:
        - 到期 (expire_at>0): 14 天内 low / 7 天内 normal / 3 天内或已过期 high
        - 额度: 达阈值 normal / >=95% 或耗尽 high
        - 重置 (billing_period=renew): 距窗口重置 <=3 天 low (每窗口一次)

        去重: 同 (池,类别) 仅在档位升级时重新推送, 避免刷屏。

        Returns:
            本轮新推送的预警列表 (全量活跃预警存 _active_alerts)
        """
        now = now or time.time()
        alerts: list[dict] = []
        for rid, pool in self._resources.items():
            if pool.status == "paused":
                continue
            # 1) 到期预警
            if pool.expire_at > 0:
                days = (pool.expire_at - now) / 86400
                if days <= 0:
                    lv, msg = 3, f"已过期 ({-days:.1f} 天前), 相关模型已从路由剔除"
                elif days <= 3:
                    lv, msg = 3, f"{days:.1f} 天后到期, 请尽快续费或调整关联模型"
                elif days <= 7:
                    lv, msg = 2, f"{days:.1f} 天后到期"
                elif days <= 14:
                    lv, msg = 1, f"{days:.1f} 天后到期"
                else:
                    lv, msg = 0, ""
                if lv:
                    alerts.append({"resource_id": rid, "kind": "expire",
                                   "level": lv, "message": msg})
            # 2) 额度预警
            if pool.quota > 0:
                usage = self.get_usage(rid)
                rate = usage.get("rate", 0.0)
                if rate >= 1.0:
                    lv, msg = 3, (f"额度已耗尽 ({usage.get('used')}/"
                                  f"{pool.quota}), 相关模型已从路由剔除")
                elif rate >= max(pool.alert_threshold, 0.95):
                    lv, msg = 3, (f"使用率 {rate*100:.0f}%, 剩余不足 "
                                  f"{(1-rate)*pool.quota:.0f}")
                elif rate >= pool.alert_threshold:
                    lv, msg = 2, (f"使用率 {rate*100:.0f}% 已达告警阈值 "
                                  f"{pool.alert_threshold*100:.0f}%")
                else:
                    lv, msg = 0, ""
                if lv:
                    alerts.append({"resource_id": rid, "kind": "quota",
                                   "level": lv, "message": msg})
            # 3) 周期重置提醒 (每窗口一次)
            if pool.billing_period == "renew" and pool.renew_at > 0:
                step = max(1, pool.period_days) * 86400
                ws = pool.window_start(now)
                days_left = (ws + step - now) / 86400
                if 0 < days_left <= 3:
                    alerts.append({"resource_id": rid, "kind": "renew",
                                   "level": 1,
                                   "message": f"额度窗口将于 {days_left:.1f} 天后重置",
                                   "_window": ws})
        # 去重后推送 (仅档位升级/新窗口触发)
        pushed: list[dict] = []
        for a in alerts:
            key = (a["resource_id"], a["kind"])
            if a["kind"] == "renew":
                if self._alert_state.get(key) == a["_window"]:
                    continue
                self._alert_state[key] = a["_window"]
            else:
                if a["level"] <= self._alert_state.get(key, 0):
                    continue
                self._alert_state[key] = a["level"]
            pushed.append(a)
            logger.warning("[resources] 预警 %s/%s (Lv%d): %s",
                           a["resource_id"], a["kind"], a["level"],
                           a["message"])
            if self._bot_notify:
                try:
                    self._bot_notify(self._ALERT_EVENT.get(a["level"],
                                                           "resource_alert"),
                                     {"resource_id": a["resource_id"],
                                      "message": a["message"]})
                except Exception as e:
                    logger.warning("[resources] 预警推送失败: %s", e)
        for a in alerts:
            a.pop("_window", None)
        self._active_alerts = alerts
        # M5: 预警到达发事件总线 (UI 实时刷新, 后台线程触发亦生效)
        if pushed:
            try:
                from .event_bus import publish_event
                publish_event("resource_alert",
                              {"count": len(pushed),
                               "max_level": max(x["level"] for x in pushed)})
            except Exception:
                pass
        return pushed

    def start_alert_checker(self, interval: float = 300.0) -> bool:
        """启动后台预警检查 (daemon 线程, 异常隔离); 启动时立即检查一轮。"""
        if not self._enabled:
            return False
        self._alert_interval = max(30.0, float(interval or 300.0))
        if self._alert_checker and self._alert_checker.is_alive():
            return True
        self._stop_evt.clear()
        self._alert_checker = threading.Thread(
            target=self._alert_loop, name="resource-alert-checker",
            daemon=True)
        self._alert_checker.start()
        try:
            self.check_alerts()
        except Exception as e:
            logger.warning("[resources] 首次预警检查异常: %s", e)
        logger.info("[resources] 预警检查已启动 (周期 %.0fs)",
                    self._alert_interval)
        return True

    def _alert_loop(self):
        """预警检查线程主循环 — 与上报线程共用 _stop_evt。"""
        while not self._stop_evt.wait(self._alert_interval):
            try:
                self.check_alerts()
            except Exception as e:
                logger.warning("[resources] 预警检查轮次异常: %s", e)


# ── 全局单例 + 轻量钩子 (供 agent_runtime / model_router 无侵入调用) ──

_mgr = ModelResourceManager()

# ── R6: 线程级成本归因上下文 ─────────────────────────────

_ctx = threading.local()


def set_usage_context(task_id: str = "", project_id: str = "") -> None:
    """设置当前线程的用量归因上下文 (R6)。

    任务执行入口 (agent_runtime.execute) 注入; 之后该线程内
    的所有记账自动带上 task_id/project_id, 无需修改底层 LLM 调用签名。
    """
    _ctx.task_id = str(task_id or "")
    _ctx.project_id = str(project_id or "")


def _get_usage_context() -> tuple:
    """读取当前线程归因上下文; 未设置返回 ("", "")。"""
    return getattr(_ctx, "task_id", ""), getattr(_ctx, "project_id", "")


def init_resource_manager(yaml_path: Union[str, Path] = None,
                          pool_entries: list = None,
                          db: Optional["Database"] = None) -> ModelResourceManager:
    """启动时调用一次, 加载资源配置。"""
    if yaml_path:
        _mgr.load(yaml_path, pool_entries, db)
    return _mgr


# ── R4: 配置读写 (UI 配置向导用) ───────────────────────────────

def read_config_data(yaml_path: Union[str, Path]) -> dict:
    """读取 resources.yaml 原始配置 (供 UI 配置向导回显)。

    Returns:
        {"exists": bool, "data": dict, "error": str}
    """
    path = Path(yaml_path)
    if not path.is_file():
        return {"exists": False, "data": {}, "error": ""}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return {"exists": True, "data": data, "error": ""}
    except Exception as e:
        return {"exists": True, "data": {}, "error": str(e)}


def validate_config(data: dict) -> list:
    """保存前校验资源配置, 返回错误列表 (空 = 合法)。"""
    errors = []
    if not isinstance(data, dict):
        return ["配置必须是键值结构"]
    resources = data.get("resources") or []
    if not isinstance(resources, list):
        return ["resources 必须是列表"]
    seen = set()
    for i, item in enumerate(resources):
        tag = f"第 {i + 1} 个资源池"
        if not isinstance(item, dict):
            errors.append(f"{tag}: 格式非法")
            continue
        rid = str(item.get("id", "")).strip()
        if not rid:
            errors.append(f"{tag}: 缺少 id")
        elif rid in seen:
            errors.append(f"{tag}: id '{rid}' 重复")
        seen.add(rid)
        if item.get("plan_type") not in VALID_PLAN_TYPES:
            errors.append(f"{tag}: plan_type 必须是 "
                          f"{'/'.join(VALID_PLAN_TYPES)}")
        try:
            if float(item.get("quota", 0)) < 0:
                errors.append(f"{tag}: quota 不能为负数")
        except (TypeError, ValueError):
            errors.append(f"{tag}: quota 必须是数字")
        thr = item.get("alert_threshold", 0.8)
        try:
            if not 0.0 <= float(thr) <= 1.0:
                errors.append(f"{tag}: alert_threshold 必须在 0~1")
        except (TypeError, ValueError):
            errors.append(f"{tag}: alert_threshold 必须是数字")
        if item.get("status", "active") not in ("active", "paused",
                                                 "exhausted"):
            errors.append(f"{tag}: status 必须是 active/paused/exhausted")
    return errors


def _backup_config(path: Path) -> str:
    """P2 #6: 配置备份移位到数据目录 (~/.lan_mesh/backups/), 保留最近 3 代。

    备份含 API Key 明文, 不再与源码同目录 (避免误入版本库/代码分发)。
    备份失败不阻断保存, 返回空串。
    """
    import shutil
    try:
        bak_dir = Path.home() / ".lan_mesh" / "backups"
        bak_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        dst = bak_dir / f"resources-{ts}.bak"
        shutil.copy2(path, dst)
        # 仅保留最近 3 代
        olds = sorted(bak_dir.glob("resources-*.bak"))
        for stale in olds[:-3]:
            try:
                stale.unlink()
            except OSError:
                pass
        return str(dst)
    except Exception as e:
        logger.warning("[resources] 配置备份失败 (不影响保存): %s", e)
        return ""


def save_config(yaml_path: Union[str, Path], data: dict) -> dict:
    """保存配置到 resources.yaml (先备份到数据目录)。

    F1: 自动注入 config_ts (Unix 时间戳) — 角色无关密钥对齐的
    仲裁依据 (谁新谁胜); config_hash 计算时排除该字段。

    Returns:
        {"ok": bool, "error": str, "backup": str}
    """
    path = Path(yaml_path)
    backup = ""
    data = dict(data)
    data["config_ts"] = time.time()  # F1: 对齐仲裁时间戳
    try:
        if path.is_file():
            backup = _backup_config(path)
        header = ("# 模型资源管理配置 (由 Web 配置向导生成, 可手工编辑)\n"
                  "# 本文件已在 .gitignore 中排除, 不会提交到版本库\n")
        path.write_text(
            header + yaml.safe_dump(data, allow_unicode=True,
                                    sort_keys=False),
            encoding="utf-8")
        logger.info("[resources] 配置已保存: %s", path.name)
        return {"ok": True, "error": "", "backup": backup}
    except Exception as e:
        logger.error("[resources] 配置保存失败: %s", e)
        return {"ok": False, "error": str(e), "backup": backup}


def record_usage_global(model_id: str, input_tokens: int, output_tokens: int,
                        usage_id: str = "", task_id: str = "",
                        project_id: str = "") -> dict:
    """记账钩子 — 每次真实 LLM 调用成功后调用, 异常不影响主流程。"""
    try:
        return _mgr.record_usage(model_id, input_tokens, output_tokens,
                                 usage_id=usage_id, task_id=task_id,
                                 project_id=project_id)
    except Exception:
        return {"tracked": False}


def set_report_target_global(url: str, interval: float = 60.0,
                             token: str = "") -> bool:
    """上报目标注入钩子 (R3/M5-2) — Worker 注册后注入 Secretary 地址。

    token 非空时同步启用 WS 直推通道鉴权 (M5-2)。
    """
    try:
        return _mgr.set_report_target(url, interval, token)
    except Exception:
        return False


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


def probe_balances_global(timeout: float = 10.0) -> dict:
    """余额探测钩子 — API 端点用, 异常不影响主流程。"""
    try:
        return _mgr.probe_balances(timeout=timeout)
    except Exception as e:
        return {"probed": 0, "supported": 0, "results": {}, "error": str(e)}


def set_bot_notify_global(callback: Optional[Callable]) -> None:
    """R7: 注入预警 Bot 推送回调 (启动/热重载后调用)。"""
    try:
        _mgr.set_bot_notify(callback)
    except Exception:
        pass


def check_alerts_global() -> list:
    """R7: 手动触发一轮预警检查 (API 端点用)。"""
    try:
        return _mgr.check_alerts()
    except Exception as e:
        return [{"error": str(e)}]


def rotation_plan_global() -> list:
    """R5: 轮换调度方案钩子 — API 端点用, 未启用时返回空。"""
    try:
        return _mgr.rotation_plan() if _mgr.enabled else []
    except Exception:
        return []


def rotation_bias_global(model_id: str) -> float:
    """R5: 模型轮换调度路由加分 (0~0.1)。

    模型对应池的调度优先级越高加分越大 — 能力相近时优先
    消耗预付费/临期额度; 未启用或未匹配池时返回 0。
    """
    try:
        if not _mgr.enabled:
            return 0.0
        pool = _mgr._find_pool(model_id)
        if pool is None:
            return 0.0
        # 优先级基线 5~10, 加分项上限 ~17; 映射到 0~0.1 封顶,
        # 确保不会反超能力匹配主导的评分
        return min(0.1, max(
            0.0, _mgr._pool_priority(pool, model_id) * 0.005))
    except Exception:
        return 0.0


def set_usage_mode_global(mode: str):
    """R5-2: 用量场景模式钩子 — 批量任务前设 batch, 结束后恢复 interactive。"""
    try:
        _mgr.set_usage_mode(mode)
    except Exception:
        pass


def report_usage_global() -> dict:
    """手动上报钩子 (R3) — API 端点用, 立即执行一轮上报。"""
    try:
        return _mgr.report_once()
    except Exception as e:
        return {"reported": 0, "error": str(e)}
