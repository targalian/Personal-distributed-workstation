"""
运行时追踪与性能审计 — P0/P1 运行时分析引擎

P0: JSON Lines 结构化追踪日志 (~/.lan_mesh/trace.jsonl)
    - 每次子任务执行写一条 trace 记录
    - 每次 LLM 调用写一条 call 记录
    - 可用 jq / pandas 快速聚合分析

P1: SQLite llm_call_log 审计表 (via database.py)
    - 持久化 LLM 调用明细 (延迟/token/成本)
    - 支持 /api/runtime/metrics 聚合查询

写入路径:
    agent_runtime.execute()
        → trace_subtask_start() → trace_subtask_end()
        → _call_openai_compatible() / _call_openai_with_tools()
            → trace_llm_call()  (同时写 JSONL + SQLite)

P3: Task Flow Trace 任务流全链路追踪 (iter-38)
    - 任务提交/PM 状态转换/子任务结果/交付 各阶段写 task_flow 记录
    - task_flow_waterfall() 按 task_id 聚合出瀑布时间线
    - iter-40: task_flow_overview 停滞检测 (stalled/idle_ms)
    - iter-41: check_stall_alerts 后台守护 — 停滞档位升级主动告警 (event_bus + Bot)

线程安全: JSONL 追加写入使用 Lock 保护; SQLite 通过 Database 线程局部连接。
"""
import json
import threading
import time
import uuid
from pathlib import Path
from typing import Callable, Optional

from .logger import get_logger

logger = get_logger("runtime_trace")

# ── JSONL 追踪日志 ─────────────────────────────────────────────

_TRACE_DIR = Path.home() / ".lan_mesh"
_TRACE_FILE = _TRACE_DIR / "trace.jsonl"
_MAX_FILE_SIZE = 50 * 1024 * 1024  # 50MB 自动轮转

_lock = threading.Lock()
_db_ref = None  # 延迟注入 Database 引用 (避免循环导入)


def set_db(db) -> None:
    """注入 Database 引用, 由 station_api 启动时调用一次。"""
    global _db_ref
    _db_ref = db


def _rotate_if_needed():
    """文件大小超限时轮转: trace.jsonl → trace.jsonl.1"""
    try:
        if _TRACE_FILE.is_file() and _TRACE_FILE.stat().st_size > _MAX_FILE_SIZE:
            backup = _TRACE_FILE.with_suffix(".jsonl.1")
            if backup.is_file():
                backup.unlink()
            _TRACE_FILE.rename(backup)
            logger.info("[Trace] 日志轮转: %s → %s", _TRACE_FILE.name, backup.name)
    except Exception as e:
        logger.debug("[Trace] 轮转检查失败: %s", e)


def _write_jsonl(record: dict) -> None:
    """线程安全地追加一条 JSONL 记录。"""
    with _lock:
        try:
            _TRACE_DIR.mkdir(parents=True, exist_ok=True)
            _rotate_if_needed()
            with open(_TRACE_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        except Exception as e:
            logger.debug("[Trace] JSONL 写入失败: %s", e)


# ── 子任务追踪 ─────────────────────────────────────────────────

def trace_subtask_start(skill: str, task_id: str = "",
                        model_pref: str = "") -> str:
    """子任务执行开始, 返回 trace_id 供 end 关联。"""
    trace_id = uuid.uuid4().hex[:12]
    record = {
        "type": "subtask_start",
        "trace_id": trace_id,
        "skill": skill,
        "task_id": task_id,
        "model_pref": model_pref,
        "ts": time.time(),
    }
    _write_jsonl(record)
    return trace_id


def trace_subtask_end(trace_id: str, skill: str, status: str,
                      elapsed_ms: float, model: str = "",
                      input_tokens: int = 0, output_tokens: int = 0,
                      error: str = "", task_id: str = "") -> None:
    """子任务执行结束。"""
    record = {
        "type": "subtask_end",
        "trace_id": trace_id,
        "skill": skill,
        "task_id": task_id,
        "status": status,
        "elapsed_ms": round(elapsed_ms, 1),
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "error": error[:500] if error else "",
        "ts": time.time(),
    }
    _write_jsonl(record)


# ── LLM 调用追踪 ──────────────────────────────────────────────

def trace_llm_call(model: str, input_tokens: int, output_tokens: int,
                   ttft_ms: float, total_ms: float, status: str = "ok",
                   task_id: str = "", error: str = "",
                   call_type: str = "chat") -> None:
    """记录一次 LLM 调用 (JSONL + SQLite 双写)。

    Args:
        call_type: "chat" (单轮) | "tools" (ReAct) | "cli" (CLI Agent)
    """
    record = {
        "type": "llm_call",
        "call_type": call_type,
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "ttft_ms": round(ttft_ms, 1),
        "total_ms": round(total_ms, 1),
        "status": status,
        "task_id": task_id,
        "error": error[:500] if error else "",
        "ts": time.time(),
    }
    _write_jsonl(record)

    # P1: SQLite 审计表双写 (异常不影响主流程)
    _write_sqlite(record)


def _write_sqlite(record: dict) -> None:
    """将 LLM 调用记录写入 SQLite 审计表 (异常静默)。"""
    if _db_ref is None:
        return
    try:
        _db_ref.insert_llm_call(
            call_type=record.get("call_type", "chat"),
            model=record.get("model", ""),
            input_tokens=record.get("input_tokens", 0),
            output_tokens=record.get("output_tokens", 0),
            ttft_ms=record.get("ttft_ms", 0),
            total_ms=record.get("total_ms", 0),
            status=record.get("status", "ok"),
            task_id=record.get("task_id", ""),
            error=record.get("error", ""),
        )
    except Exception as e:
        logger.debug("[Trace] SQLite 写入失败: %s", e)


# ── 任务流追踪 (P3: Task Flow Trace) ─────────────────────

# 任务生命周期阶段 → 展示名映射 (供瀑布图渲染)
TASK_STAGE_LABELS: dict[str, str] = {
    "submitted": "任务提交",
    "pm:planning": "PM 规划中",
    "pm:executing": "PM 执行中",
    "pm:monitoring": "PM 监控中",
    "pm:awaiting_input": "等待 Boss 决策",
    "pm:completed": "任务完成",
    "pm:failed": "任务失败",
    "pm:cancelled": "任务取消",
    "pm:paused": "任务暂停",
    "subtask_result": "子任务结果",
    "delivered": "交付上报",
}


def trace_task_event(task_id: str, stage: str, detail: str = "",
                     pm_id: str = "") -> None:
    """记录一条任务流阶段事件 (P3 Task Flow Trace, 异常静默)。

    Args:
        task_id: 任务 ID (空则不写)
        stage: 阶段标识 (submitted / pm:<status> / subtask_result / delivered)
        detail: 阶段补充说明 (截断 200 字)
        pm_id: 关联 PM Agent ID (可空)
    """
    if not task_id:
        return
    try:
        record = {
            "type": "task_flow",
            "task_id": task_id,
            "stage": stage,
            "detail": (detail or "")[:200],
            "pm_id": pm_id,
            "ts": time.time(),
        }
        _write_jsonl(record)
    except Exception as e:
        logger.debug("[Trace] task_flow 写入失败: %s", e)


def read_task_flow(task_id: str, limit: int = 200) -> list[dict]:
    """按时间正序读取指定任务的阶段事件 (最多回溯 5000 行)。"""
    if not task_id or not _TRACE_FILE.is_file():
        return []
    events: list[dict] = []
    try:
        with open(_TRACE_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()[-5000:]
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                continue
            if rec.get("type") != "task_flow":
                continue
            if rec.get("task_id") != task_id:
                continue
            events.append(rec)
    except Exception as e:
        logger.debug("[Trace] task_flow 读取失败: %s", e)
    events.sort(key=lambda r: r.get("ts", 0))
    return events[-limit:]


def task_flow_waterfall(task_id: str, limit: int = 200) -> dict:
    """聚合任务流瀑布: 事件按时间排序, 附每阶段距上一步的间隔与总耗时。

    Returns:
        {
            "task_id": "...",
            "events": [{"stage", "label", "detail", "pm_id", "ts", "gap_ms"}, ...],
            "total_ms": 12345.6,
            "stage_count": 6,
        }
    """
    events = read_task_flow(task_id, limit=limit)
    rows: list[dict] = []
    prev_ts = 0.0
    for rec in events:
        ts = float(rec.get("ts", 0) or 0)
        stage = rec.get("stage", "")
        rows.append({
            "stage": stage,
            "label": TASK_STAGE_LABELS.get(stage, stage),
            "detail": rec.get("detail", ""),
            "pm_id": rec.get("pm_id", ""),
            "ts": ts,
            "gap_ms": round((ts - prev_ts) * 1000, 1) if prev_ts else 0,
        })
        prev_ts = ts
    total_ms = round((rows[-1]["ts"] - rows[0]["ts"]) * 1000, 1) if len(rows) > 1 else 0
    return {
        "task_id": task_id,
        "events": rows,
        "total_ms": total_ms,
        "stage_count": len(rows),
    }


# 终态阶段: 到达其一视为任务流已收尾 (供总览表状态判断)
TASK_FLOW_TERMINAL_STAGES = frozenset({
    "pm:completed", "pm:failed", "pm:cancelled", "delivered",
})


def task_flow_overview(limit: int = 20, stall_minutes: float = 30.0) -> list[dict]:
    """任务流总览: 按任务聚合最近阶段事件, 末活动时间倒序。

    用于总览表一眼看清哪些任务在跑/已收尾/可能停滞。
    最多回溯 5000 行。

    Args:
        limit: 最多返回任务数
        stall_minutes: 停滞判定阈值 (分钟); 未到终态且空闲超过该值标记 stalled, ≤0 禁用检测 (iter-40)

    Returns:
        [{"task_id", "first_ts", "last_ts", "stage_count",
          "last_stage", "last_label", "total_ms", "done",
          "idle_ms", "stalled"}, ...]
    """
    if not _TRACE_FILE.is_file():
        return []
    agg: dict[str, dict] = {}
    try:
        with open(_TRACE_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()[-5000:]
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                continue
            if rec.get("type") != "task_flow":
                continue
            tid = rec.get("task_id", "")
            if not tid:
                continue
            ts = float(rec.get("ts", 0) or 0)
            stage = rec.get("stage", "")
            cur = agg.get(tid)
            if cur is None:
                agg[tid] = {
                    "task_id": tid, "first_ts": ts, "last_ts": ts,
                    "stage_count": 1, "last_stage": stage,
                }
            else:
                cur["stage_count"] += 1
                if ts < cur["first_ts"]:
                    cur["first_ts"] = ts
                if ts >= cur["last_ts"]:
                    cur["last_ts"] = ts
                    cur["last_stage"] = stage
    except Exception as e:
        logger.debug("[Trace] task_flow 总览聚合失败: %s", e)
    rows = []
    now = time.time()
    stall_ms = stall_minutes * 60 * 1000 if stall_minutes > 0 else 0
    for cur in agg.values():
        cur["last_label"] = TASK_STAGE_LABELS.get(cur["last_stage"], cur["last_stage"])
        cur["total_ms"] = round((cur["last_ts"] - cur["first_ts"]) * 1000, 1)
        cur["done"] = cur["last_stage"] in TASK_FLOW_TERMINAL_STAGES
        # iter-40: 停滞检测 — 未到终态且空闲超阈值 (已收尾任务永不标停滞)
        cur["idle_ms"] = round(max(0.0, (now - cur["last_ts"]) * 1000), 1)
        cur["stalled"] = (not cur["done"]) and stall_ms > 0 and cur["idle_ms"] > stall_ms
        rows.append(cur)
    rows.sort(key=lambda r: r["last_ts"], reverse=True)
    return rows[:max(1, limit)]


# ── 读取与分析 ─────────────────────────────────────────────────

def read_trace_lines(limit: int = 100, line_type: str = "") -> list[dict]:
    """读取最近的 trace 记录 (从文件尾部倒序)。

    Args:
        limit: 最多返回条数
        line_type: 过滤类型 ("llm_call" / "subtask_end" / ""); 空=全部
    """
    if not _TRACE_FILE.is_file():
        return []
    results = []
    try:
        with open(_TRACE_FILE, "r", encoding="utf-8") as f:
            # 读取最后 N 行 (简单策略: 全读后取尾)
            lines = f.readlines()
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                continue
            if line_type and rec.get("type") != line_type:
                continue
            results.append(rec)
            if len(results) >= limit:
                break
    except Exception as e:
        logger.debug("[Trace] 读取失败: %s", e)
    return results


def trace_stats(hours: float = 1.0) -> dict:
    """从 JSONL 快速聚合最近 N 小时的运行时指标。

    Returns:
        {
            "window_hours": 1.0,
            "llm_calls": 47,
            "avg_latency_ms": 3200,
            "p99_latency_ms": 12000,
            "total_input_tokens": 85000,
            "total_output_tokens": 12000,
            "by_model": {"qwen3.8-max": 30, ...},
            "by_skill": {"code_generation": 20, ...},
            "by_status": {"ok": 45, "timeout": 2},
            "subtasks": {"total": 15, "completed": 12, "failed": 3},
            "errors": [{"model": "...", "error": "...", "count": 3}],
        }
    """
    cutoff = time.time() - hours * 3600
    calls = read_trace_lines(limit=10000, line_type="llm_call")
    calls = [c for c in calls if c.get("ts", 0) >= cutoff]
    subtasks = read_trace_lines(limit=10000, line_type="subtask_end")
    subtasks = [s for s in subtasks if s.get("ts", 0) >= cutoff]

    # LLM 调用聚合
    latencies = [c["total_ms"] for c in calls if c.get("total_ms")]
    latencies.sort()
    by_model: dict[str, int] = {}
    by_status: dict[str, int] = {}
    total_in = total_out = 0
    error_map: dict[str, int] = {}

    for c in calls:
        m = c.get("model", "unknown")
        by_model[m] = by_model.get(m, 0) + 1
        s = c.get("status", "unknown")
        by_status[s] = by_status.get(s, 0) + 1
        total_in += c.get("input_tokens", 0)
        total_out += c.get("output_tokens", 0)
        if s != "ok":
            key = f"{m}: {c.get('error', s)[:80]}"
            error_map[key] = error_map.get(key, 0) + 1

    avg_latency = sum(latencies) / len(latencies) if latencies else 0
    p99_latency = latencies[int(len(latencies) * 0.99)] if latencies else 0

    # 子任务聚合
    st_total = len(subtasks)
    st_completed = sum(1 for s in subtasks if s.get("status") == "completed")
    st_failed = sum(1 for s in subtasks if s.get("status") == "failed")
    by_skill: dict[str, int] = {}
    for s in subtasks:
        sk = s.get("skill", "unknown")
        by_skill[sk] = by_skill.get(sk, 0) + 1

    errors = sorted(
        [{"model": k.split(":")[0].strip(),
          "error": k.split(":", 1)[1].strip() if ":" in k else k,
          "count": v}
         for k, v in error_map.items()],
        key=lambda x: -x["count"]
    )[:5]

    return {
        "window_hours": hours,
        "llm_calls": len(calls),
        "avg_latency_ms": round(avg_latency, 1),
        "p99_latency_ms": round(p99_latency, 1),
        "avg_ttft_ms": round(
            sum(c.get("ttft_ms", 0) for c in calls) / len(calls), 1
        ) if calls else 0,
        "total_input_tokens": total_in,
        "total_output_tokens": total_out,
        "by_model": dict(sorted(by_model.items(), key=lambda x: -x[1])),
        "by_skill": dict(sorted(by_skill.items(), key=lambda x: -x[1])),
        "by_status": by_status,
        "subtasks": {
            "total": st_total,
            "completed": st_completed,
            "failed": st_failed,
            "success_rate": round(st_completed / st_total, 3) if st_total else 0,
        },
        "errors": errors,
    }


# ── 任务停滞主动告警 (iter-41) ───────────────────────────

_stall_watcher: Optional[threading.Thread] = None
_stall_stop_evt = threading.Event()
_stall_interval = 60.0            # 后台检查周期 (秒)
_stall_minutes = 30.0             # 停滞判定阈值 (分钟)
_stall_state: dict[str, int] = {}      # task_id → 已推档位 (仅升级重推; 恢复清除)
_stall_active: list[dict] = []         # 最近一轮活跃停滞告警 (全量, 供 API/总览)
_stall_bot_notify: Optional[Callable] = None  # Bot 推送回调 (可选注入)

# 告警事件档位 → Bot 事件类型 (三档, 与资源预警对齐)
_STALL_ALERT_EVENT = {1: "task_stall_alert_low", 2: "task_stall_alert",
                      3: "task_stall_alert_high"}


def _stall_level(idle_ms: float, stall_ms: float) -> int:
    """停滞档位: 空闲达阈值 1 倍→Lv1, 2 倍→Lv2, 4 倍→Lv3。"""
    if stall_ms <= 0:
        return 0
    ratio = idle_ms / stall_ms
    if ratio >= 4:
        return 3
    if ratio >= 2:
        return 2
    return 1


def set_stall_bot_notify(callback: Optional[Callable]) -> None:
    """注入停滞告警 Bot 推送回调 fn(event_type, data); 未注入时仅日志+事件总线。"""
    global _stall_bot_notify
    _stall_bot_notify = callback


def check_stall_alerts(now: float = None) -> list[dict]:
    """执行一轮停滞告警检查: 仅新停滞/档位升级时推送 (防刷屏)。

    任务恢复活动 (不再停滞) 后清除其档位记录, 再次停滞可重新告警。
    已收尾 (终态) 任务永不告警。返回本轮新推送的告警。
    """
    global _stall_active
    now = now if now is not None else time.time()
    stall_ms = _stall_minutes * 60 * 1000
    alerts: list[dict] = []
    if stall_ms > 0:
        try:
            for r in task_flow_overview(limit=100,
                                        stall_minutes=_stall_minutes):
                if not r.get("stalled"):
                    continue
                lv = _stall_level(r["idle_ms"], stall_ms)
                idle_min = r["idle_ms"] / 60000
                alerts.append({
                    "task_id": r["task_id"],
                    "level": lv,
                    "last_stage": r["last_stage"],
                    "last_label": r["last_label"],
                    "idle_min": round(idle_min, 1),
                    "message": (f"已 {idle_min:.0f} 分钟无阶段事件, "
                                f"末阶段 {r['last_label']}"),
                })
        except Exception as e:
            logger.debug("[Trace] 停滞检查聚合失败: %s", e)
    _stall_active = alerts
    stalled_ids = {a["task_id"] for a in alerts}
    # 恢复清理: 不再停滞的任务清除档位, 允许再次停滞时重新告警
    for tid in list(_stall_state.keys()):
        if tid not in stalled_ids:
            del _stall_state[tid]
    pushed: list[dict] = []
    for a in alerts:
        if a["level"] <= _stall_state.get(a["task_id"], 0):
            continue
        _stall_state[a["task_id"]] = a["level"]
        pushed.append(a)
        logger.warning("[Trace] 任务停滞告警 %s (Lv%d): %s",
                       a["task_id"], a["level"], a["message"])
        if _stall_bot_notify:
            try:
                _stall_bot_notify(
                    _STALL_ALERT_EVENT.get(a["level"], "task_stall_alert"),
                    {"task_id": a["task_id"], "message": a["message"]})
            except Exception as e:
                logger.warning("[Trace] 停滞告警推送失败: %s", e)
    # M5: 事件总线实时推送 (Dashboard 总览表刷新 + toast)
    if pushed:
        try:
            from .event_bus import publish_event
            publish_event("task_stall_alert",
                          {"count": len(pushed),
                           "max_level": max(x["level"] for x in pushed),
                           "tasks": [x["task_id"] for x in pushed][:5]})
        except Exception:
            pass
    return pushed


def start_stall_watcher(interval: float = 60.0,
                        stall_minutes: float = 30.0) -> bool:
    """启动停滞检测守护线程 (daemon, 异常隔离); 启动时立即检查一轮。

    Args:
        interval: 检查周期 (秒, 最小 10)
        stall_minutes: 停滞阈值 (分钟); ≤0 禁用检测 (不启动线程)
    """
    global _stall_watcher, _stall_interval, _stall_minutes
    if stall_minutes <= 0:
        logger.info("[Trace] 停滞检测已禁用 (stall_minutes≤0)")
        return False
    _stall_interval = max(10.0, float(interval or 60.0))
    _stall_minutes = float(stall_minutes)
    if _stall_watcher is not None and _stall_watcher.is_alive():
        return True
    _stall_stop_evt.clear()

    def _loop():
        while not _stall_stop_evt.wait(_stall_interval):
            try:
                check_stall_alerts()
            except Exception as e:
                logger.debug("[Trace] 停滞检查轮次异常: %s", e)

    _stall_watcher = threading.Thread(target=_loop, daemon=True,
                                      name="task-stall-watcher")
    _stall_watcher.start()
    try:
        check_stall_alerts()
    except Exception as e:
        logger.warning("[Trace] 首次停滞检查异常: %s", e)
    logger.info("[Trace] 停滞检测已启动 (周期 %.0fs, 阈值 %.0f 分钟)",
                _stall_interval, _stall_minutes)
    return True


def stop_stall_watcher() -> None:
    """停止停滞检测守护线程 (幂等)。"""
    global _stall_watcher
    _stall_stop_evt.set()
    _stall_watcher = None


def active_stall_alerts() -> list[dict]:
    """当前活跃停滞告警 (最近一轮全量, 未做档位去重)。"""
    return list(_stall_active)


def stall_watcher_status() -> dict:
    """停滞检测守护状态 (API 展示用)。"""
    return {
        "watching": _stall_watcher is not None and _stall_watcher.is_alive(),
        "interval": _stall_interval,
        "stall_minutes": _stall_minutes,
    }
