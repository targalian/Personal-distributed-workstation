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
import threading
import time
import uuid
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
    api_key: str = ""                         # API Key 直填值 (R4, 仅余额探测用;
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
        self._stop_evt = threading.Event()

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
        # R4: 热重载安全 — 先停旧上报线程, 重置动态状态
        self.stop_reporter()
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
            # R3: 配置了 Secretary 地址 → 启用用量上报 (Worker 主机)
            self.set_report_target(data.get("secretary_url", ""),
                                   float(data.get("report_interval", 60)))
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

    def set_report_target(self, url: str, interval: float = 60.0) -> bool:
        """设置/启动用量上报目标 (Worker 主机配置或运行时注入)。

        Args:
            url: Secretary 站点地址 (空 → 不启用上报)
            interval: 上报周期 (秒)

        Returns:
            是否已启用上报线程
        """
        target = (url or "").strip().rstrip("/")
        self._secretary_url = target
        self._report_interval = max(5.0, float(interval or 60.0))
        if not target or not self._enabled or self._db is None:
            return False
        if self._reporter and self._reporter.is_alive():
            return True  # 线程已在运行 (仅更新目标地址)
        self._stop_evt.clear()
        self._reporter = threading.Thread(
            target=self._report_loop, name="resource-usage-reporter",
            daemon=True)
        self._reporter.start()
        logger.info("[resources] 用量上报已启用 → %s (周期 %.0fs)",
                    target, self._report_interval)
        return True

    def stop_reporter(self):
        """停止上报线程 (进程退出前调用)。"""
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
        }


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


def save_config(yaml_path: Union[str, Path], data: dict) -> dict:
    """保存配置到 resources.yaml (先备份 .bak)。

    Returns:
        {"ok": bool, "error": str, "backup": str}
    """
    path = Path(yaml_path)
    backup = ""
    try:
        if path.is_file():
            backup = str(path) + ".bak"
            path.replace(backup)
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


def set_report_target_global(url: str, interval: float = 60.0) -> bool:
    """上报目标注入钩子 (R3) — Worker 收到任务后注入 Secretary 地址。"""
    try:
        return _mgr.set_report_target(url, interval)
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


def report_usage_global() -> dict:
    """手动上报钩子 (R3) — API 端点用, 立即执行一轮上报。"""
    try:
        return _mgr.report_once()
    except Exception as e:
        return {"reported": 0, "error": str(e)}
