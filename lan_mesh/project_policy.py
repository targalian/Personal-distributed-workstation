"""Compatibility facade and composition root for independently routed domains."""

"""
项目管理与预算控制 — Phase 3 项目隔离核心

职责:
1. 项目的创建、查询、更新、归档
2. 模型调用成本计算 (基于 token 用量与模型定价)
3. 预算护栏: 超支自动暂停项目, 切换经济模型
4. 消费记录追踪

每个项目拥有:
- 独立工作空间目录 (上下文隔离)
- 独立预算配额 (互不影响)
- 允许模型白名单 (限制可用模型)
- 路由策略 (cost_first / quality_first / balanced)
"""
import time
import uuid
from pathlib import Path
from typing import Optional

from .database import Database
from .protocol import Project, ProjectStatus
from .logger import get_logger

logger = get_logger("project")


# ── 模型定价表 (美元 / 1K tokens) ────────────────────────────────
# 数据来源: 各厂商官方定价, 仅供参考, 实际以 API 返回为准

MODEL_PRICING = {
    # DeepSeek
    "deepseek-chat":       {"input_per_1k": 0.0014, "output_per_1k": 0.0028},
    "deepseek-coder":      {"input_per_1k": 0.0014, "output_per_1k": 0.0028},
    "deepseek-reasoner":   {"input_per_1k": 0.0040, "output_per_1k": 0.0160},
    # OpenAI
    "gpt-4o-mini":         {"input_per_1k": 0.00015, "output_per_1k": 0.0006},
    "gpt-4o":              {"input_per_1k": 0.0025, "output_per_1k": 0.01},
    "gpt-4-turbo":         {"input_per_1k": 0.01, "output_per_1k": 0.03},
    # Anthropic
    "claude-3-haiku":      {"input_per_1k": 0.00025, "output_per_1k": 0.00125},
    "claude-3-5-sonnet":   {"input_per_1k": 0.003, "output_per_1k": 0.015},
    # 通义千问
    "qwen-turbo":          {"input_per_1k": 0.0003, "output_per_1k": 0.0006},
    "qwen-plus":           {"input_per_1k": 0.0013, "output_per_1k": 0.0038},
}

# 默认经济模型 (预算超支 80% 后切换)
ECONOMY_MODEL = "deepseek-chat"


def calculate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """根据模型定价和 token 用量计算单次调用成本 (美元)。

    未知模型使用 deepseek-chat 的定价作为保守估算。
    """
    pricing = MODEL_PRICING.get(model, MODEL_PRICING[ECONOMY_MODEL])
    cost = (
        (input_tokens / 1000) * pricing["input_per_1k"]
        + (output_tokens / 1000) * pricing["output_per_1k"]
    )
    return round(cost, 6)


def _current_phase_index(roadmap: list):
    """返回当前路线图阶段下标: 优先 in_progress/active, 其次首个未完成。

    与 pm_planner._current_roadmap_phase 的选取口径保持一致, 确保
    「PM 读到的阶段」与「交付回流写入的阶段」是同一个。
    """
    active_words = ("in_progress", "doing", "active")
    done_words = ("done", "completed")
    for i, item in enumerate(roadmap):
        if isinstance(item, dict) and \
                str(item.get("status", "")).strip().lower() in active_words:
            return i
    for i, item in enumerate(roadmap):
        if isinstance(item, dict) and \
                str(item.get("status", "")).strip().lower() not in done_words:
            return i
    return None



