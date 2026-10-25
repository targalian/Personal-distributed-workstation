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
from contextlib import contextmanager
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
    api_key: str = field(default="", repr=False)  # API Key 直填值 (R4/S1: 加载时自动
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


from typing import Any

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

    def load(self, yaml_path: Union[str, Path], pool_entries: list=None, db: Optional['Database']=None) -> bool:
        return call_component(self, ResourceConfigService, 'load', yaml_path=yaml_path, pool_entries=pool_entries, db=db)

    def _inject_direct_keys(self) -> Any:
        return call_component(self, ResourceConfigService, '_inject_direct_keys')

    # ── 台账 ────────────────────────────────────────────────────

    def list_resources(self) -> list[dict]:
        return call_component(self, ResourceConfigService, 'list_resources')

    def _find_pool(self, model_id: str) -> Optional[ModelResource]:
        return call_component(self, ResourceSelectionService, '_find_pool', model_id=model_id)
    
    def _pool_priority(self, pool: ModelResource, model_id: str='') -> float:
        return call_component(self, ResourceSelectionService, '_pool_priority', pool=pool, model_id=model_id)

    def _pool_priority_rule(self, pool: ModelResource) -> float:
        return call_component(self, ResourceSelectionService, '_pool_priority_rule', pool=pool)

    def _pool_score(self, pool: ModelResource, model_id: str='') -> dict:
        return call_component(self, ResourceSelectionService, '_pool_score', pool=pool, model_id=model_id)

    def _window_urgency(self, pool: ModelResource, now: float) -> float:
        return call_component(self, ResourceSelectionService, '_window_urgency', pool=pool, now=now)

    def _time_bonus(self, pool: ModelResource, model_id: str) -> float:
        return call_component(self, ResourceSelectionService, '_time_bonus', pool=pool, model_id=model_id)

    def set_usage_mode(self, mode: str) -> Any:
        return call_component(self, ResourceSelectionService, 'set_usage_mode', mode=mode)
    
    def rotation_plan(self) -> list:
        return call_component(self, ResourceSelectionService, 'rotation_plan')

    # ── 记账 ────────────────────────────────────────────────────

    def record_usage(self, model_id: str, input_tokens: int, output_tokens: int, usage_id: str='', task_id: str='', project_id: str='') -> dict:
        return call_component(self, ResourceAccountingService, 'record_usage', model_id=model_id, input_tokens=input_tokens, output_tokens=output_tokens, usage_id=usage_id, task_id=task_id, project_id=project_id)

    def get_usage(self, resource_id: str) -> dict:
        return call_component(self, ResourceAccountingService, 'get_usage', resource_id=resource_id)

    # ── 可用性 ──────────────────────────────────────────────────

    def is_available(self, model_id: str) -> bool:
        return call_component(self, ResourceAccountingService, 'is_available', model_id=model_id)

    # ── 报告 ────────────────────────────────────────────────────

    def probe_balances(self, timeout: float=10.0) -> dict:
        return call_component(self, ResourceAccountingService, 'probe_balances', timeout=timeout)

    def set_pool_status(self, rid: str, status: str) -> bool:
        return call_component(self, ResourceConfigService, 'set_pool_status', rid=rid, status=status)

    # ── R3: 跨主机用量上报 (Worker → Secretary) ──────────────────

    def set_report_target(self, url: str, interval: float=60.0, token: str='') -> bool:
        return call_component(self, ResourceReportingService, 'set_report_target', url=url, interval=interval, token=token)

    def stop_reporter(self) -> Any:
        return call_component(self, ResourceReportingService, 'stop_reporter')

    def report_once(self, batch: int=200) -> dict:
        return call_component(self, ResourceReportingService, 'report_once', batch=batch)

    def _report_loop(self) -> Any:
        return call_component(self, ResourceReportingService, '_report_loop')

    # ── M5-2: WS 直推 (实时用量通道, HTTP 批量兜底) ────────────

    def _push_once_ws(self, conn) -> bool:
        return call_component(self, ResourceReportingService, '_push_once_ws', conn=conn)

    def _ws_push_loop(self) -> Any:
        return call_component(self, ResourceReportingService, '_ws_push_loop')

    def summarize(self) -> dict:
        return call_component(self, ResourceAccountingService, 'summarize')

    # ── R7: 到期/额度预警 ────────────────────────────────

    _ALERT_EVENT = {1: "resource_alert_low", 2: "resource_alert",
                    3: "resource_alert_high"}

    def set_bot_notify(self, callback: Optional[Callable]) -> None:
        return call_component(self, ResourceAlertService, 'set_bot_notify', callback=callback)

    def check_alerts(self, now: float=None) -> list:
        return call_component(self, ResourceAlertService, 'check_alerts', now=now)

    def start_alert_checker(self, interval: float=300.0) -> bool:
        return call_component(self, ResourceAlertService, 'start_alert_checker', interval=interval)

    def _alert_loop(self) -> Any:
        return call_component(self, ResourceAlertService, '_alert_loop')


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


@contextmanager
def usage_context(task_id: str = "", project_id: str = ""):
    """Restore the caller's attribution when a reused thread finishes a task."""
    previous = _get_usage_context()
    set_usage_context(task_id, project_id)
    try:
        yield
    finally:
        set_usage_context(*previous)


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


def set_pool_status_global(rid: str, status: str) -> bool:
    """iter-60: 池状态标记钩子 — 自愈写动作用, 异常不影响主流程。"""
    try:
        return _mgr.set_pool_status(rid, status)
    except Exception:
        return False


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


from typing import Any
from .component_service import call_component
from .resource_config_service import ResourceConfigService
from .resource_selection_service import ResourceSelectionService
from .resource_accounting_service import ResourceAccountingService
from .resource_reporting_service import ResourceReportingService
from .resource_alert_service import ResourceAlertService
