"""
模型路由器 — Phase 2 核心模块

职责:
1. 任务难度分级 (L1-L4), 基于规则分类
2. 加权评分路由算法: 为每个任务选择最优模型
   Score = (能力匹配度 × W_cap) + (成本反向指数 × W_cost) + (响应速度 × W_speed) - (负载率 × W_load)
3. 降级链 (Fallback Chain): 首选模型失败时沿链路重试
4. 策略适配: cost_first / quality_first / balanced

评分算法详解:
- 能力匹配度: 模型 capabilities 与难度所需能力的 Jaccard 相似度 (0~1)
- 成本反向指数: 1 - (归一化成本), 越便宜得分越高 (0~1)
- 响应速度: 直接使用模型配置的 speed_score (0~1)
- 当前负载率: 默认 0 (未来可接入实时监控)
"""
import os
from typing import Optional

from .logger import get_logger
from .protocol import DifficultyLevel, RoutingResult
from .model_resources import resource_available

logger = get_logger("model_router")


# ── 难度 → 所需能力映射 ────────────────────────────────────────

DIFFICULTY_CAPS = {
    "L1": {"lightweight"},
    "L2": {"general", "lightweight"},
    "L3": {"reasoning", "coding"},
    "L4": {"reasoning", "coding", "long_context"},
}

# ── 难度分类关键词 ──────────────────────────────────────────────

_L3_SKILLS = {"code_generation", "code_review"}
_L2_SKILLS = {"document_summary"}

_L4_KEYWORDS = [
    "架构", "architecture", "设计模式", "design pattern",
    "分布式", "distributed", "微服务", "microservice",
    "深度分析", "deep analysis", "全面评估", "comprehensive",
    "大规模", "large scale", "系统级", "system level",
    "性能优化", "performance optimization",
]

_L3_KEYWORDS = [
    "代码", "code", "函数", "function", "算法", "algorithm",
    "实现", "implement", "编程", "program", "重构", "refactor",
    "调试", "debug", "逻辑", "logic", "推理", "reason",
    "bug", "错误修复", "fix",
]

_L1_KEYWORDS = [
    "格式转换", "format", "转换", "convert",
    "提取", "extract", "关键词", "keyword",
    "分类", "classify", "标签", "tag",
    "翻译", "translate", "替换", "replace",
]


def classify_difficulty(text: str, skill: str = "") -> str:
    """基于规则的任务难度分类。

    分级策略:
    1. 先根据技能类型粗筛 (code_generation → L3+, document_summary → L2)
    2. 再根据文本长度和关键词精调

    Args:
        text: 任务描述/需求文本
        skill: 任务所需技能类型

    Returns:
        难度级别: "L1" / "L2" / "L3" / "L4"
    """
    text_lower = text.lower()
    text_len = len(text)

    # ── 技能类型粗筛 ──
    base_level = "L2"  # 默认
    if skill in _L3_SKILLS:
        base_level = "L3"
    elif skill in _L2_SKILLS:
        base_level = "L2"

    # ── 关键词精调 ──

    # L4: 长文本 + 复杂关键词
    l4_hits = sum(1 for kw in _L4_KEYWORDS if kw in text_lower)
    if l4_hits >= 2 or (l4_hits >= 1 and text_len > 1500):
        return DifficultyLevel.L4.value

    # L3: 代码/推理关键词, 或基础技能为 L3
    l3_hits = sum(1 for kw in _L3_KEYWORDS if kw in text_lower)
    if l3_hits >= 2 or (base_level == "L3" and l3_hits >= 1):
        return DifficultyLevel.L3.value
    if base_level == "L3" and text_len > 500:
        return DifficultyLevel.L3.value

    # L1: 短文本 + 简单关键词
    l1_hits = sum(1 for kw in _L1_KEYWORDS if kw in text_lower)
    if text_len < 200 and (l1_hits >= 1 or skill not in _L3_SKILLS):
        if text_len < 100 or l1_hits >= 2:
            return DifficultyLevel.L1.value

    return base_level


# ── 策略权重配置 ─────────────────────────────────────────────────

STRATEGY_WEIGHTS = {
    "balanced":      {"cap": 0.4, "cost": 0.3, "speed": 0.2, "load": 0.1},
    "cost_first":    {"cap": 0.2, "cost": 0.5, "speed": 0.2, "load": 0.1},
    "quality_first": {"cap": 0.6, "cost": 0.1, "speed": 0.2, "load": 0.1},
}


class ModelRouter:
    """模型路由器 — 多目标优化决策引擎。

    部署在 Secretary 节点, 与 Orchestrator 协同:
    - 任务提交时: 根据难度 + 项目策略选择最优模型
    - 失败降级: 首选模型不可用时沿 Fallback Chain 重试
    """

    def __init__(self, pool_entries: list, project_manager=None):
        """初始化路由器。

        Args:
            pool_entries: ModelEntryConfig 列表 (来自 config.load_model_pool)
            project_manager: ProjectManager 实例 (可选, 用于获取项目策略)
        """
        self._entries = {e.id: e for e in pool_entries}
        self._pool_list = list(pool_entries)
        self.project_manager = project_manager

        # 预计算成本归一化基准 (最大成本)
        all_costs = [
            e.cost_input_per_1k + e.cost_output_per_1k
            for e in pool_entries
        ]
        self._max_cost = max(all_costs) if all_costs else 1.0
        self._min_cost = min(all_costs) if all_costs else 0.0

    @property
    def pool_size(self) -> int:
        """模型池大小。"""
        return len(self._entries)

    def list_models(self) -> list[dict]:
        """返回模型池摘要列表。"""
        return [
            {
                "id": e.id,
                "provider": e.provider,
                "capabilities": e.capabilities,
                "quality_score": e.quality_score,
                "cost_input_per_1k": e.cost_input_per_1k,
                "cost_output_per_1k": e.cost_output_per_1k,
            }
            for e in self._pool_list
        ]

    # ── 主路由方法 ─────────────────────────────────────────────────

    def route(
        self,
        text: str,
        skill: str = "",
        project_id: str = "",
        preferred_model: str = "",
    ) -> RoutingResult:
        """路由决策: 为给定任务选择最优模型。

        Args:
            text: 任务描述文本
            skill: 任务所需技能
            project_id: 关联的项目 ID (用于获取策略和白名单)
            preferred_model: 用户/系统偏好的模型 (可选)

        Returns:
            RoutingResult 含推荐模型、评分详情和降级链
        """
        # 1. 难度分类
        difficulty = classify_difficulty(text, skill)

        # 2. 确定策略和候选模型
        strategy = "balanced"
        allowed_models = []

        if project_id and self.project_manager:
            project = self.project_manager.get_project(project_id)
            if project:
                strategy = project.routing_strategy or "balanced"
                allowed_models = project.allowed_models or []
                # 预算紧张时强制经济策略
                if self.project_manager.should_use_economy_model(project_id):
                    if strategy == "quality_first":
                        strategy = "balanced"
                    elif strategy == "balanced":
                        strategy = "cost_first"

        weights = STRATEGY_WEIGHTS.get(strategy, STRATEGY_WEIGHTS["balanced"])

        # 3. 过滤候选模型 (按白名单 + API Key 可用性)
        candidates = self._filter_candidates(allowed_models)

        if not candidates:
            # 无可用模型, 返回默认
            return RoutingResult(
                selected_model="deepseek-chat",
                provider="deepseek",
                base_url="https://api.deepseek.com/v1",
                difficulty=difficulty,
                score=0.0,
                candidates=[],
                fallback_chain=[],
                strategy=strategy,
            )

        # 4. 对每个候选模型计算评分
        scored = []
        for entry in candidates:
            score = self._compute_score(entry, difficulty, weights)
            scored.append({"model": entry.id, "score": round(score, 4)})

        # 5. 排序选最高分
        scored.sort(key=lambda x: x["score"], reverse=True)
        best_id = scored[0]["model"]
        best_entry = self._entries[best_id]

        # 6. 构建降级链
        fallback = self.get_fallback_chain(best_id, allowed_models)

        return RoutingResult(
            selected_model=best_entry.id,
            provider=best_entry.provider,
            base_url=best_entry.base_url,
            difficulty=difficulty,
            score=scored[0]["score"],
            candidates=scored,
            fallback_chain=fallback,
            strategy=strategy,
        )

    # ── 评分算法 ───────────────────────────────────────────────────

    def _compute_score(self, entry, difficulty: str, weights: dict) -> float:
        """计算单个模型的综合评分。

        Score = (能力匹配度 × W_cap) + (成本反向指数 × W_cost)
              + (响应速度 × W_speed) - (当前负载率 × W_load)
        """
        # 能力覆盖率: 模型 capabilities 覆盖所需能力的比例 (0~1)
        # 使用覆盖率而非 Jaccard, 避免惩罚能力更全面的模型
        required = DIFFICULTY_CAPS.get(difficulty, DIFFICULTY_CAPS["L2"])
        model_caps = set(entry.capabilities)
        if required:
            covered = sum(1 for cap in required if cap in model_caps)
            cap_match = covered / len(required)
        else:
            cap_match = 1.0

        # 成本反向指数 (越便宜越高)
        total_cost = entry.cost_input_per_1k + entry.cost_output_per_1k
        cost_range = self._max_cost - self._min_cost
        if cost_range > 0:
            cost_index = 1.0 - (total_cost - self._min_cost) / cost_range
        else:
            cost_index = 1.0

        # 响应速度
        speed = entry.speed_score

        # 当前负载率 (未来可接入实时监控, 默认 0)
        load = 0.0

        # 加权求和
        score = (
            cap_match * weights["cap"]
            + cost_index * weights["cost"]
            + speed * weights["speed"]
            - load * weights["load"]
        )

        return score

    # ── 候选过滤 ───────────────────────────────────────────────────

    def _filter_candidates(self, allowed_models: list = None) -> list:
        """过滤可用模型: 白名单 + API Key 环境变量检查。"""
        candidates = []
        for entry in self._pool_list:
            # 白名单过滤
            if allowed_models and entry.id not in allowed_models:
                continue
            # API Key 可用性检查
            api_key = os.environ.get(entry.api_key_env, "")
            if not api_key:
                continue
            # R1: 资源池可用性 (耗尽/过期/暂停 → 剔除; 未启用时放行)
            if not resource_available(entry.id):
                logger.debug("模型 %s 资源池不可用, 从候选剔除", entry.id)
                continue
            candidates.append(entry)
        return candidates

    # ── 降级链 ─────────────────────────────────────────────────────

    def get_fallback_chain(self, model_id: str, allowed_models: list = None) -> list:
        """获取模型的降级链。

        返回模型 ID 列表 (不含首选模型本身), 按优先级排序。
        仅包含 API Key 可用且在白名单内的模型。
        """
        entry = self._entries.get(model_id)
        if not entry:
            return []

        chain = []
        for fallback_id in entry.fallback:
            fb_entry = self._entries.get(fallback_id)
            if not fb_entry:
                continue
            if allowed_models and fb_entry.id not in allowed_models:
                continue
            api_key = os.environ.get(fb_entry.api_key_env, "")
            if not api_key:
                continue
            # R1: 资源池可用性
            if not resource_available(fb_entry.id):
                continue
            chain.append(fb_entry.id)

        return chain
