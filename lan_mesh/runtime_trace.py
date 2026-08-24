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

线程安全: JSONL 追加写入使用 Lock 保护; SQLite 通过 Database 线程局部连接。
"""
import json
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

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
