"""Project composition facade preserving the public manager API."""

from .project_policy import (
    time, uuid, Path, Optional, Database, Project, ProjectStatus, logger,
    MODEL_PRICING, ECONOMY_MODEL, calculate_cost, _current_phase_index,
)

from typing import Any
from .component_service import call_component
from .project_blueprint_service import ProjectBlueprintService
from .project_budget_service import ProjectBudgetService
from .project_lifecycle_service import ProjectLifecycleService

class ProjectManager:
    """项目管理器 — 负责项目生命周期与预算控制。

    部署在 Secretary 节点,与 Orchestrator 协同工作:
    - 任务提交前: 检查项目预算是否充足
    - 任务完成后: 记录模型调用消费
    - 预算超支: 自动暂停项目或切换经济模型
    """

    # 预算使用率阈值: 超过此值切换经济模型
    BUDGET_WARN_RATIO = 0.8    # 80%
    BUDGRESS_LIMIT_RATIO = 1.0  # 100% (超支暂停)

    def __init__(self, db: Database):
        self.db = db

    def create_project(self, name: str, description: str='', budget_limit_usd: float=10.0, allowed_models: list=None, routing_strategy: str='balanced', workspace_base: str='', charter: dict=None, roadmap: list=None, decisions: list=None) -> Project:
        return call_component(self, ProjectLifecycleService, "create_project", name=name, description=description, budget_limit_usd=budget_limit_usd, allowed_models=allowed_models, routing_strategy=routing_strategy, workspace_base=workspace_base, charter=charter, roadmap=roadmap, decisions=decisions)

    def get_project(self, project_id: str) -> Optional[Project]:
        return call_component(self, ProjectLifecycleService, "get_project", project_id=project_id)

    def list_projects(self, status: str=None) -> list[Project]:
        return call_component(self, ProjectLifecycleService, "list_projects", status=status)

    def update_project(self, project_id: str, name: str=None, description: str=None, budget_limit_usd: float=None, allowed_models: list=None, routing_strategy: str=None, status: str=None, charter: dict=None, roadmap: list=None, decisions: list=None) -> Optional[Project]:
        return call_component(self, ProjectLifecycleService, "update_project", project_id=project_id, name=name, description=description, budget_limit_usd=budget_limit_usd, allowed_models=allowed_models, routing_strategy=routing_strategy, status=status, charter=charter, roadmap=roadmap, decisions=decisions)

    def update_project_blueprint(self, project_id: str, charter: dict, roadmap: list, decisions: list) -> Optional[Project]:
        return call_component(self, ProjectBlueprintService, "update_project_blueprint", project_id=project_id, charter=charter, roadmap=roadmap, decisions=decisions)

    def record_delivery_to_blueprint(self, project_id: str, task_name: str, acceptance_review: dict, task_id: str='') -> Optional[Project]:
        return call_component(self, ProjectBlueprintService, "record_delivery_to_blueprint", project_id=project_id, task_name=task_name, acceptance_review=acceptance_review, task_id=task_id)

    def archive_project(self, project_id: str) -> bool:
        return call_component(self, ProjectLifecycleService, "archive_project", project_id=project_id)

    # ── 预算控制 ─────────────────────────────────────────────────

    def check_budget(self, project_id: str) -> bool:
        return call_component(self, ProjectBudgetService, "check_budget", project_id=project_id)

    def get_budget_ratio(self, project_id: str) -> float:
        return call_component(self, ProjectBudgetService, "get_budget_ratio", project_id=project_id)

    def should_use_economy_model(self, project_id: str) -> bool:
        return call_component(self, ProjectBudgetService, "should_use_economy_model", project_id=project_id)

    def get_recommended_model(self, project_id: str, default_model: str='deepseek-chat') -> str:
        return call_component(self, ProjectBudgetService, "get_recommended_model", project_id=project_id, default_model=default_model)

    # ── 消费记录 ─────────────────────────────────────────────────

    def record_usage(self, project_id: str, task_id: str, subtask_id: str='', model: str='', input_tokens: int=0, output_tokens: int=0) -> float:
        return call_component(self, ProjectBudgetService, "record_usage", project_id=project_id, task_id=task_id, subtask_id=subtask_id, model=model, input_tokens=input_tokens, output_tokens=output_tokens)

    def suspend_if_over_budget(self, project_id: str) -> bool:
        return call_component(self, ProjectBudgetService, "suspend_if_over_budget", project_id=project_id)

    # ── 状态查询 ─────────────────────────────────────────────────

    def get_project_status(self, project_id: str) -> Optional[dict]:
        return call_component(self, ProjectLifecycleService, "get_project_status", project_id=project_id)
