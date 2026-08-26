"""
F1.4: 本地错误聚合追踪

职责:
1. 捕获并聚合系统运行中的异常 (按模块/类型分组)
2. 提供错误统计 API (频率、最近出现时间、影响模块)
3. 支持错误率告警 (阈值触发通知)
4. 轻量级实现: 内存环形缓冲 + 落盘回调持久化 SQLite (iter-47)

用法:
    from .error_tracker import error_tracker

    # 记录错误
    error_tracker.capture("pm", exc, context={"task_id": "xxx"})

    # 查询统计
    stats = error_tracker.get_stats()
    recent = error_tracker.get_recent(limit=20)

    # 装饰器模式
    @error_tracker.track("station")
    def risky_function():
        ...
"""
import threading
import time
import traceback
from collections import defaultdict
from typing import Optional, Callable

from .logger import get_logger

logger = get_logger("error_tracker")

# iter-46 (F4.2 异常自愈): 错误模式 → 诊断建议规则表 (小写子串匹配, 首命中归属)
# pattern 为匹配词元 (error_type + message 拼接后小写匹配); action 为建议动作标识
DIAGNOSIS_RULES: list[dict] = [
    {"pattern": ("timeout", "超时", "timed out"), "category": "timeout",
     "suggestion": "网络或下游服务响应超时: 检查对端服务是否存活, 或延长超时/启用重试",
     "action": "check_peer"},
    {"pattern": ("refused", "reset", "unreachable", "拒绝连接", "无法连接"),
     "category": "connection",
     "suggestion": "连接被拒/重置: 对端端口未监听或网络不可达, 检查对端进程与 IP/端口",
     "action": "check_peer"},
    {"pattern": ("401", "unauthorized", "invalid key", "api key", "鉴权失败"),
     "category": "auth",
     "suggestion": "认证失败: API Key 无效或已过期, 在资源面板更新密钥并重新同步",
     "action": "rotate_key"},
    {"pattern": ("429", "rate limit", "too many requests", "quota"),
     "category": "rate_limit",
     "suggestion": "限流/额度耗尽: 切换到其他模型池或等待窗口重置",
     "action": "switch_pool"},
    {"pattern": ("500", "502", "503", "504", "bad gateway", "internal server error"),
     "category": "upstream_5xx",
     "suggestion": "上游服务 5xx: 多为瞬时故障, 重试仍失败可考虑切换模型",
     "action": "retry_or_switch"},
]


def diagnose_records(records: list[dict]) -> dict:
    """iter-48 (F4.2): 对任意记录列表执行模式规则诊断 (纯函数, 无状态)。

    规则按 DIAGNOSIS_RULES 顺序匹配, 每条记录首命中归属 (防重复计数);
    findings 按命中数降序; 未命中任何规则计入 unmatched。
    供 diagnose() (进程内缓冲) 与历史诊断 (error_log 落盘记录) 复用。
    """
    findings: list[dict] = []
    matched: set[int] = set()
    for rule in DIAGNOSIS_RULES:
        hits: list[tuple[int, dict]] = []
        for j, r in enumerate(records):
            if j in matched:
                continue
            text = (str(r.get("error_type", "")) + " " +
                    str(r.get("message", ""))).lower()
            if any(p in text for p in rule["pattern"]):
                hits.append((j, r))
        if not hits:
            continue
        matched.update(j for j, _ in hits)
        findings.append({
            "category": rule["category"],
            "count": len(hits),
            "modules": sorted({str(r.get("module", "")) for _, r in hits}),
            "last_at": max(float(r.get("timestamp") or 0) for _, r in hits),
            "sample": str(hits[-1][1].get("message", ""))[:200],
            "suggestion": rule["suggestion"],
            "action": rule["action"],
        })
    findings.sort(key=lambda f: f["count"], reverse=True)
    return {"scanned": len(records), "findings": findings,
            "unmatched": len(records) - len(matched)}


class ErrorRecord:
    """单条错误记录。"""
    __slots__ = ("timestamp", "module", "error_type", "message", "context", "traceback")

    def __init__(self, module: str, error_type: str, message: str,
                 context: dict = None, tb: str = ""):
        self.timestamp = time.time()
        self.module = module
        self.error_type = error_type
        self.message = message[:500]  # 截断过长消息
        self.context = context or {}
        self.traceback = tb[:2000] if tb else ""

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "module": self.module,
            "error_type": self.error_type,
            "message": self.message,
            "context": self.context,
            "traceback": self.traceback,
        }


class ErrorTracker:
    """本地错误聚合追踪器 (单例)。

    特性:
    - 环形缓冲区 (默认保留最近 500 条)
    - 按模块/错误类型聚合统计
    - 线程安全
    - 错误率告警 (1分钟内超过阈值触发回调)
    """

    def __init__(self, max_records: int = 500, alert_threshold: int = 10,
                 alert_window: float = 60.0):
        self._max_records = max_records
        self._alert_threshold = alert_threshold  # 窗口内错误数阈值
        self._alert_window = alert_window        # 告警窗口 (秒)

        self._records: list[ErrorRecord] = []
        self._lock = threading.Lock()

        # 聚合统计: module → {error_type → count}
        self._stats: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        self._total_count: int = 0
        self._window_timestamps: list[float] = []  # 用于告警窗口计算

        # 告警回调 (突发: 窗口内超阈值触发)
        self._alert_callback: Optional[Callable] = None
        # iter-44: 突发告警冷却去重 — module → 上次告警时间 (防错误风暴时刷屏)
        self._last_alert_at: dict[str, float] = {}
        self._alert_cooldown: float = alert_window  # 同模块两次告警最小间隔 (秒)

        # iter-44: 全局事件回调 — 每条错误触发 (WS 实时推送/面板刷新, 异常隔离)
        self._event_callback: Optional[Callable] = None

        # iter-47: 落盘持久化回调 — 每条错误触发 (SQLite error_log 表, 异常隔离)
        self._persist_callback: Optional[Callable] = None

    def set_alert_callback(self, callback: Callable):
        """设置突发告警回调: callback(module, count, window_secs)。"""
        self._alert_callback = callback

    def set_event_callback(self, callback: Optional[Callable]):
        """iter-44: 设置全局事件回调: callback(record_dict), 每条 capture 触发。"""
        self._event_callback = callback

    def set_persist_callback(self, callback: Optional[Callable]):
        """iter-47: 设置落盘持久化回调: callback(record_dict), 每条 capture 触发。"""
        self._persist_callback = callback

    def capture(self, module: str, exc: Exception = None, *,
                error_type: str = "", message: str = "", context: dict = None):
        """捕获一条错误。

        Args:
            module: 来源模块 (如 "pm", "station", "bot")
            exc: 异常实例 (可选, 自动提取类型和消息)
            error_type: 错误类型 (不传则从 exc 推断)
            message: 错误消息 (不传则从 exc 推断)
            context: 附加上下文 (如 task_id, agent_id)
        """
        if exc:
            error_type = error_type or type(exc).__name__
            message = message or str(exc)
            tb = traceback.format_exc()
        else:
            error_type = error_type or "Unknown"
            message = message or "未指定错误"
            tb = ""

        record = ErrorRecord(module, error_type, message, context, tb)

        with self._lock:
            # 环形缓冲
            self._records.append(record)
            if len(self._records) > self._max_records:
                self._records = self._records[-self._max_records:]

            # 聚合统计
            self._stats[module][error_type] += 1
            self._total_count += 1

            # 告警窗口
            now = time.time()
            self._window_timestamps.append(now)
            # 清理过期
            cutoff = now - self._alert_window
            self._window_timestamps = [t for t in self._window_timestamps if t > cutoff]

        # 日志输出
        logger.error("[%s] %s: %s", module, error_type, message[:200])

        # iter-44: 全局事件回调 (每条错误实时推送, 异常隔离不影响捕获)
        if self._event_callback:
            try:
                self._event_callback(record.to_dict())
            except Exception as e:
                logger.warning("[ErrorTracker] 事件回调失败: %s", e)

        # iter-47: 落盘持久化回调 (SQLite 存储, 异常隔离不影响捕获)
        if self._persist_callback:
            try:
                self._persist_callback(record.to_dict())
            except Exception as e:
                logger.warning("[ErrorTracker] 持久化回调失败: %s", e)

        # 告警检查 (冷却期内同模块不重复触发)
        if len(self._window_timestamps) >= self._alert_threshold and self._alert_callback:
            with self._lock:
                last = self._last_alert_at.get(module, 0.0)
                due = (now - last) >= self._alert_cooldown
                if due:
                    self._last_alert_at[module] = now
            if due:
                try:
                    self._alert_callback(module, len(self._window_timestamps),
                                         self._alert_window)
                except Exception:
                    pass

    def track(self, module: str):
        """装饰器: 自动捕获函数异常并记录。

        用法:
            @error_tracker.track("station")
            def risky():
                ...
        """
        def decorator(func):
            def wrapper(*args, **kwargs):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    self.capture(module, e, context={"function": func.__name__})
                    raise
            wrapper.__name__ = func.__name__
            wrapper.__doc__ = func.__doc__
            return wrapper
        return decorator

    def get_stats(self) -> dict:
        """获取错误统计摘要。"""
        with self._lock:
            by_module = {}
            for mod, types in self._stats.items():
                by_module[mod] = {
                    "total": sum(types.values()),
                    "by_type": dict(types),
                }
            return {
                "total_errors": self._total_count,
                "buffered_records": len(self._records),
                "by_module": by_module,
                "recent_window_count": len(self._window_timestamps),
                "alert_threshold": self._alert_threshold,
            }

    def get_recent(self, limit: int = 20, module: str = "") -> list[dict]:
        """获取最近的错误记录。"""
        with self._lock:
            records = self._records
            if module:
                records = [r for r in records if r.module == module]
            return [r.to_dict() for r in records[-limit:]]

    def clear(self):
        """清空所有记录 (用于测试/重置)。"""
        with self._lock:
            self._records.clear()
            self._stats.clear()
            self._total_count = 0
            self._window_timestamps.clear()
            self._last_alert_at.clear()

    def diagnose(self, window_records: int = 200) -> dict:
        """iter-46 (F4.2): 按模式规则表诊断缓冲错误, 返回分组自愈建议。

        规则匹配逻辑见模块级 diagnose_records() (iter-48 抽出复用);
        window 夹取 1~500。
        """
        window_records = max(1, min(window_records, 500))
        with self._lock:
            records = [r.to_dict() for r in self._records[-window_records:]]
        return diagnose_records(records)


# ── 全局单例 ──────────────────────────────────────────────────
error_tracker = ErrorTracker()
