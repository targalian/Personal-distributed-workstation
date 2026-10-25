"""Project budget domain over the shared project policy."""

from .project_policy import (
    time, ProjectStatus, logger,
    ECONOMY_MODEL, calculate_cost,
)
from .component_service import ComponentService

class ProjectBudgetService(ComponentService):
    """Single domain implementation behind the existing facade."""

    OPERATIONS = ('check_budget', 'get_budget_ratio', 'should_use_economy_model', 'get_recommended_model', 'record_usage', 'suspend_if_over_budget')

    def check_budget(self, project_id: str) -> bool:
        """检查项目预算是否充足 (未超支且状态为 active)。

        Returns:
            True 表示可以继续提交任务
        """
        project = self.owner.db.get_project(project_id)
        if not project:
            return False
        if project.status != ProjectStatus.ACTIVE:
            return False
        # 预算为 0 表示无限制
        if project.budget_limit_usd <= 0:
            return True
        return project.budget_used_usd < project.budget_limit_usd


    def get_budget_ratio(self, project_id: str) -> float:
        """返回预算使用率 (0.0 ~ 1.0+)。"""
        project = self.owner.db.get_project(project_id)
        if not project or project.budget_limit_usd <= 0:
            return 0.0
        return project.budget_used_usd / project.budget_limit_usd


    def should_use_economy_model(self, project_id: str) -> bool:
        """预算使用率超过警告阈值时,建议切换经济模型。"""
        return self.owner.get_budget_ratio(project_id) >= self.owner.BUDGET_WARN_RATIO


    def get_recommended_model(self, project_id: str, default_model: str = "deepseek-chat") -> str:
        """根据项目预算状况和路由策略推荐模型。

        - cost_first: 始终用经济模型
        - quality_first: 始终用 default_model
        - balanced: 预算充足用 default_model, 超过 80% 切经济模型
        """
        project = self.owner.db.get_project(project_id)
        if not project:
            return default_model

        # 检查模型白名单
        if project.allowed_models and default_model not in project.allowed_models:
            # 模型不在白名单中, 从白名单中选第一个
            return project.allowed_models[0]

        if project.routing_strategy == "cost_first":
            return ECONOMY_MODEL
        if project.routing_strategy == "quality_first":
            return default_model
        # balanced
        if self.owner.should_use_economy_model(project_id):
            # 预算紧张, 切经济模型
            allowed = project.allowed_models
            if not allowed or ECONOMY_MODEL in allowed:
                return ECONOMY_MODEL
            return allowed[0]
        return default_model


    def record_usage(
        self,
        project_id: str,
        task_id: str,
        subtask_id: str = "",
        model: str = "",
        input_tokens: int = 0,
        output_tokens: int = 0,
    ) -> float:
        """记录一次模型调用消费,自动计算成本并更新项目预算。

        Returns:
            本次调用成本 (美元)
        """
        cost = calculate_cost(model, input_tokens, output_tokens)

        # 写入消费记录
        self.owner.db.record_usage(
            project_id=project_id,
            task_id=task_id,
            subtask_id=subtask_id,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost,
        )

        # 更新项目已消费金额
        project = self.owner.db.get_project(project_id)
        if project:
            project.budget_used_usd = round(project.budget_used_usd + cost, 6)
            project.updated_at = time.time()
            self.owner.db.upsert_project(project)

            # 检查是否超支
            self.owner.suspend_if_over_budget(project_id)

        return cost


    def suspend_if_over_budget(self, project_id: str) -> bool:
        """如果项目超支,自动暂停。

        Returns:
            True 表示已触发暂停
        """
        project = self.owner.db.get_project(project_id)
        if not project or project.status != ProjectStatus.ACTIVE:
            return False
        if project.budget_limit_usd <= 0:
            return False
        if project.budget_used_usd >= project.budget_limit_usd:
            self.owner.db.update_project_status(project_id, ProjectStatus.SUSPENDED)
            logger.warning(
                "项目超支已暂停: %s (已用 $%.4f / 预算 $%.2f)",
                project.name, project.budget_used_usd, project.budget_limit_usd,
            )
            return True
        return False

