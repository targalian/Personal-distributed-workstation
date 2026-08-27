"""
预算顾问 — F4.4 成本感知调度 (iter-52)

职责:
1. 任务 Token 预算预估 (文本启发式基线 + 历史任务实际消耗修正)
2. 预算适配检查 (预估 vs 资源池剩余额度 + 项目预算剩余换算)
3. 提交链路接入: 预估落盘 task.input_data._cost_estimate,
   tight/insufficient 时 Bot/WS 告警 (不阻断执行 — 启发式预估
   仅作参考, 硬护栏仍是项目 check_budget)

预估口径: 一次完整任务 ≈ PM 规划 + 拆解 + 子任务执行 + 聚合多轮
LLM 调用, 按输入文本 token 数 × 编排放大系数粗估; 近 30 天有
历史样本时与历史任务均值混合 (提升可信度)。
"""
import time
from typing import Optional

from .logger import get_logger

logger = get_logger("budget")

# ── 常量 ─────────────────────────────────────────────────────

PM_MULTIPLIER = 6.0        # 编排放大系数 (规划/拆解/子任务/聚合多轮 LLM)
SAFETY_FACTOR = 1.2        # 剩余 ≥ 预估×1.2 视为充足
HISTORY_WINDOW_DAYS = 30   # 历史消耗统计窗口
HISTORY_MIN_SAMPLES = 3    # 历史修正最少样本数
HISTORY_WEIGHT = 0.6       # 混合预估中历史均值的权重

# 预算适配状态 → 说明文案
FIT_LABELS = {
    "ok": "预算充足",
    "tight": "预算紧张",
    "insufficient": "预算不足",
    "unknown": "无预算数据",
}


# ── 预估层 ────────────────────────────────────────────────────

def estimate_text_tokens(text: str) -> int:
    """估算文本 token 数 (中文每字≈1, 其余每 4 字符≈1)。"""
    if not text:
        return 0
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    other = len(text) - cjk
    return max(1, cjk + max(1, other // 4))


def estimate_task_tokens(task_name: str, task_description: str,
                         db=None) -> dict:
    """预估任务全链路 Token 消耗。

    - 基线: (名称+描述) token 估算 × 编排放大系数
    - 历史: 近 30 天有任务归因的实际消耗均值 (db.avg_tokens_per_task)
    - 混合: 历史样本 ≥ 3 时 0.4×基线 + 0.6×历史, 否则纯基线

    Returns:
        {task_name, estimated_tokens, baseline_tokens, history_avg_tokens,
         history_samples, basis, confidence, text_tokens}
    """
    text = f"{task_name or ''}\n{task_description or ''}"
    text_tokens = estimate_text_tokens(text)
    baseline = max(1, int(text_tokens * PM_MULTIPLIER))

    hist_avg, samples = 0.0, 0
    if db is not None:
        try:
            hist = db.avg_tokens_per_task(days=HISTORY_WINDOW_DAYS) or {}
            hist_avg = float(hist.get("avg", 0.0) or 0.0)
            samples = int(hist.get("samples", 0) or 0)
        except Exception:
            pass  # 历史统计失败不影响预估

    if samples >= HISTORY_MIN_SAMPLES and hist_avg > 0:
        estimated = int(baseline * (1.0 - HISTORY_WEIGHT)
                        + hist_avg * HISTORY_WEIGHT)
        basis, confidence = "mixed", "high"
    else:
        estimated = baseline
        basis, confidence = "heuristic", "low"

    return {
        "task_name": task_name,
        "estimated_tokens": estimated,
        "baseline_tokens": baseline,
        "history_avg_tokens": int(hist_avg),
        "history_samples": samples,
        "basis": basis,
        "confidence": confidence,
        "text_tokens": text_tokens,
    }


# ── 约束层 ────────────────────────────────────────────────────

def check_budget_fit(estimated_tokens: int, mgr=None,
                     project_id: str = "", project_manager=None) -> dict:
    """预算适配检查 (F4.4): 预估 vs 可用预算。

    - 池层: token 口径池 (token/coding 计划) 中剩余额度最大者
      vs 预估×安全系数; 按量池金额口径不可比, 跳过
    - 项目层: 项目预算剩余 (美元) 按经济模型单价换算 token 预算
    - status: ok / tight / insufficient / unknown (两层均无数据)

    Returns:
        {status, label, pool_id, pool_remaining, project_token_budget,
         required, advice}
    """
    est = max(0, int(estimated_tokens or 0))
    required = max(1, int(est * SAFETY_FACTOR))
    advice_parts: list[str] = []

    # 池层: 选 token 口径池中剩余额度最大者 (近似任务将消耗的池)
    pool_id, pool_remaining = "", 0.0
    if mgr is not None:
        try:
            best = 0.0
            for pool in mgr.list_resources():
                if pool.status != "active":
                    continue
                if pool.is_payg:
                    continue  # 金额口径不可与 token 预估直接比较
                usage = mgr.get_usage(pool.id) or {}
                remaining = float(usage.get("remaining", 0.0) or 0.0)
                if remaining > best:
                    best = remaining
                    pool_id = pool.id
                    pool_remaining = remaining
        except Exception:
            pass  # 池层异常静默

    # 项目层: 预算剩余金额按经济模型单价换算 token 预算
    project_token_budget = 0.0
    if project_id and project_manager is not None:
        try:
            from .project import ECONOMY_MODEL, calculate_cost
            project = project_manager.get_project(project_id)
            if project and project.budget_limit_usd > 0:
                remaining_usd = max(
                    0.0, project.budget_limit_usd - project.budget_used_usd)
                # 经济模型 1 input + 1 output token 的成本 → 单 token 均价
                per_token = calculate_cost(ECONOMY_MODEL, 1, 1) / 2.0
                if per_token > 0:
                    project_token_budget = remaining_usd / per_token
        except Exception:
            pass  # 项目层异常静默

    # 状态判定: 两层中取最紧的一层
    layers = []
    if pool_remaining > 0:
        layers.append(("pool", pool_id, pool_remaining))
    if project_token_budget > 0:
        layers.append(("project", project_id, project_token_budget))

    if not layers:
        return {
            "status": "unknown", "label": FIT_LABELS["unknown"],
            "pool_id": pool_id, "pool_remaining": round(pool_remaining, 1),
            "project_token_budget": round(project_token_budget, 1),
            "required": required,
            "advice": "无可用预算数据, 未做预算适配检查",
        }

    worst = min(layers, key=lambda x: x[2])
    worst_name, worst_value = worst[0], worst[2]
    if worst_value < est:
        status = "insufficient"
        advice_parts.append(
            f"{'资源池 ' + worst[1] if worst_name == 'pool' else '项目预算'} "
            f"剩余 {worst_value:,.0f} tokens 低于预估 {est:,}, 建议换更经济模型或补充额度")
    elif worst_value < required:
        status = "tight"
        advice_parts.append(
            f"{'资源池 ' + worst[1] if worst_name == 'pool' else '项目预算'} "
            f"剩余 {worst_value:,.0f} tokens 偏紧 (预估 {est:,}×{SAFETY_FACTOR}), "
            f"建议关注消耗")
    else:
        status = "ok"
        advice_parts.append("预算充足, 可按常规策略调度")

    return {
        "status": status, "label": FIT_LABELS[status],
        "pool_id": pool_id, "pool_remaining": round(pool_remaining, 1),
        "project_token_budget": round(project_token_budget, 1),
        "required": required,
        "advice": "; ".join(advice_parts),
    }


# ── 组合入口 ──────────────────────────────────────────────────

def build_task_cost_estimate(task_name: str, task_description: str,
                             db=None, mgr=None, project_id: str = "",
                             project_manager=None) -> dict:
    """F4.4 组合入口: 预估 + 适配检查, 供任务提交链路调用。

    Returns:
        预估结果附 budget_fit 字段; 全链异常隔离, 失败返回保守结果。
    """
    try:
        est = estimate_task_tokens(task_name, task_description, db)
        est["budget_fit"] = check_budget_fit(
            est["estimated_tokens"], mgr, project_id, project_manager)
        return est
    except Exception as e:
        logger.warning("[budget] 预算预估异常 (不阻断): %s", e)
        return {
            "task_name": task_name, "estimated_tokens": 0,
            "basis": "failed", "confidence": "none",
            "budget_fit": {"status": "unknown",
                           "label": FIT_LABELS["unknown"],
                           "advice": f"预估异常: {e}"},
        }
