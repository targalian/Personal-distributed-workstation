"""ModelRoutingService: domain behavior separated from the public compatibility facade."""

from __future__ import annotations
from . import model_router as policy
from .component_service import ComponentService

class ModelRoutingService(ComponentService):
    """ModelRoutingService business operations with explicit state ownership."""
    OPERATIONS = ('list_models', 'route', '_filter_candidates', 'get_fallback_chain')

    def list_models(self) -> list[dict]:
        """返回模型池摘要列表。"""
        return [{'id': e.id, 'provider': e.provider, 'capabilities': e.capabilities, 'quality_score': e.quality_score, 'cost_input_per_1k': e.cost_input_per_1k, 'cost_output_per_1k': e.cost_output_per_1k} for e in self.owner._pool_list]

    def route(self, text: str, skill: str='', project_id: str='', preferred_model: str='') -> policy.RoutingResult:
        """路由决策: 为给定任务选择最优模型。

        Args:
            text: 任务描述文本
            skill: 任务所需技能
            project_id: 关联的项目 ID (用于获取策略和白名单)
            preferred_model: 用户/系统偏好的模型 (可选)

        Returns:
            RoutingResult 含推荐模型、评分详情和降级链
        """
        difficulty = policy.classify_difficulty(text, skill)
        strategy = 'balanced'
        allowed_models = []
        if project_id and self.owner.project_manager:
            project = self.owner.project_manager.get_project(project_id)
            if project:
                strategy = project.routing_strategy or 'balanced'
                allowed_models = project.allowed_models or []
                if self.owner.project_manager.should_use_economy_model(project_id):
                    if strategy == 'quality_first':
                        strategy = 'balanced'
                    elif strategy == 'balanced':
                        strategy = 'cost_first'
        weights = policy.STRATEGY_WEIGHTS.get(strategy, policy.STRATEGY_WEIGHTS['balanced'])
        candidates = self.owner._filter_candidates(allowed_models)
        if not candidates:
            return policy.RoutingResult(selected_model='deepseek-chat', provider='deepseek', base_url='https://api.deepseek.com/v1', difficulty=difficulty, score=0.0, candidates=[], fallback_chain=[], strategy=strategy)
        scored = []
        for entry in candidates:
            score = self.owner._compute_score(entry, difficulty, weights)
            scored.append({'model': entry.id, 'score': round(score, 4)})
        scored.sort(key=lambda x: x['score'], reverse=True)
        best_id = scored[0]['model']
        best_entry = self.owner._entries[best_id]
        fallback = self.owner.get_fallback_chain(best_id, allowed_models)
        return policy.RoutingResult(selected_model=best_entry.id, provider=best_entry.provider, base_url=best_entry.base_url, difficulty=difficulty, score=scored[0]['score'], candidates=scored, fallback_chain=fallback, strategy=strategy)

    def _filter_candidates(self, allowed_models: list=None) -> list:
        """过滤可用模型: 白名单 + API Key 环境变量检查。"""
        candidates = []
        for entry in self.owner._pool_list:
            if allowed_models and entry.id not in allowed_models:
                continue
            api_key = policy.os.environ.get(entry.api_key_env, '')
            if not api_key:
                continue
            if not self.owner._resource_available(entry.id):
                policy.logger.debug('模型 %s 资源池不可用, 从候选剔除', entry.id)
                continue
            candidates.append(entry)
        return candidates

    def get_fallback_chain(self, model_id: str, allowed_models: list=None) -> list:
        """获取模型的降级链。

        返回模型 ID 列表 (不含首选模型本身), 按优先级排序。
        仅包含 API Key 可用且在白名单内的模型。
        """
        entry = self.owner._entries.get(model_id)
        if not entry:
            return []
        chain = []
        for fallback_id in entry.fallback:
            fb_entry = self.owner._entries.get(fallback_id)
            if not fb_entry:
                continue
            if allowed_models and fb_entry.id not in allowed_models:
                continue
            api_key = policy.os.environ.get(fb_entry.api_key_env, '')
            if not api_key:
                continue
            if not self.owner._resource_available(fb_entry.id):
                continue
            chain.append(fb_entry.id)
        chain.sort(key=lambda mid: self.owner._rotation_bias(mid), reverse=True)
        return chain
