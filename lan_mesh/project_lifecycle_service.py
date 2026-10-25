"""Project lifecycle domain over the shared project policy."""

from .project_policy import (
    time, uuid, Path, Optional, Project, ProjectStatus, logger,
)
from .component_service import ComponentService

class ProjectLifecycleService(ComponentService):
    """Single domain implementation behind the existing facade."""

    OPERATIONS = ('create_project', 'get_project', 'list_projects', 'update_project', 'archive_project', 'get_project_status')

    def create_project(
        self,
        name: str,
        description: str = "",
        budget_limit_usd: float = 10.0,
        allowed_models: list = None,
        routing_strategy: str = "balanced",
        workspace_base: str = "",
        charter: dict = None,
        roadmap: list = None,
        decisions: list = None,
    ) -> Project:
        """创建新项目。

        Args:
            name: 项目名称
            description: 项目描述
            budget_limit_usd: 月度预算上限 (美元)
            allowed_models: 允许使用的模型 ID 列表 (空=全部允许)
            routing_strategy: 路由策略 (cost_first / quality_first / balanced)
            workspace_base: 工作空间根目录 (默认 ~/.lan_mesh/workspaces)

        Returns:
            创建的 Project 对象
        """
        project_id = str(uuid.uuid4())
        now = time.time()

        # 创建独立工作空间目录
        base = Path(workspace_base) if workspace_base else (Path.home() / ".lan_mesh" / "workspaces")
        ws_path = base / project_id[:8]
        ws_path.mkdir(parents=True, exist_ok=True)

        project = Project(
            project_id=project_id,
            name=name,
            description=description,
            workspace_path=str(ws_path),
            budget_limit_usd=budget_limit_usd,
            budget_used_usd=0.0,
            allowed_models=allowed_models or [],
            routing_strategy=routing_strategy,
            status=ProjectStatus.ACTIVE,
            charter=charter or {},
            roadmap=roadmap or [],
            decisions=decisions or [],
            created_at=now,
            updated_at=now,
        )
        self.owner.db.upsert_project(project)
        logger.info("项目已创建: %s (%s) 预算=$%s", name, project_id[:8], budget_limit_usd)
        return project


    def get_project(self, project_id: str) -> Optional[Project]:
        """查询项目详情。"""
        return self.owner.db.get_project(project_id)


    def list_projects(self, status: str = None) -> list[Project]:
        """列出所有项目,可按状态过滤。"""
        return self.owner.db.list_projects(status=status)


    def update_project(
        self,
        project_id: str,
        name: str = None,
        description: str = None,
        budget_limit_usd: float = None,
        allowed_models: list = None,
        routing_strategy: str = None,
        status: str = None,
        charter: dict = None,
        roadmap: list = None,
        decisions: list = None,
    ) -> Optional[Project]:
        """更新项目字段 (仅更新非 None 的字段)。"""
        project = self.owner.db.get_project(project_id)
        if not project:
            return None

        if name is not None:
            project.name = name
        if description is not None:
            project.description = description
        if budget_limit_usd is not None:
            project.budget_limit_usd = budget_limit_usd
        if allowed_models is not None:
            project.allowed_models = allowed_models
        if routing_strategy is not None:
            project.routing_strategy = routing_strategy
        if status is not None:
            project.status = status
        if charter is not None:
            project.charter = charter
        if roadmap is not None:
            project.roadmap = roadmap
        if decisions is not None:
            project.decisions = decisions

        project.updated_at = time.time()
        self.owner.db.upsert_project(project)
        return project


    def archive_project(self, project_id: str) -> bool:
        """归档项目 (软删除)。"""
        project = self.owner.db.get_project(project_id)
        if not project:
            return False
        self.owner.db.delete_project(project_id)
        logger.info("项目已归档: %s (%s)", project.name, project_id[:8])
        return True


    def get_project_status(self, project_id: str) -> Optional[dict]:
        """返回项目完整状态 (含预算使用率、消费记录摘要)。"""
        project = self.owner.db.get_project(project_id)
        if not project:
            return None

        usage_log = self.owner.db.get_usage_log(project_id, limit=10)
        total_calls = len(usage_log)
        total_input_tokens = sum(r.get("input_tokens", 0) for r in usage_log)
        total_output_tokens = sum(r.get("output_tokens", 0) for r in usage_log)

        budget_ratio = 0.0
        if project.budget_limit_usd > 0:
            budget_ratio = project.budget_used_usd / project.budget_limit_usd

        return {
            **project.to_dict(),
            "budget_ratio": round(budget_ratio, 4),
            "budget_remaining": round(max(0, project.budget_limit_usd - project.budget_used_usd), 6),
            "recent_calls": total_calls,
            "recent_input_tokens": total_input_tokens,
            "recent_output_tokens": total_output_tokens,
            "is_over_budget": project.budget_used_usd >= project.budget_limit_usd if project.budget_limit_usd > 0 else False,
            "should_use_economy": self.owner.should_use_economy_model(project_id),
        }

