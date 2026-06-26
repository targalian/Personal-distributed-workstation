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

    def create_project(
        self,
        name: str,
        description: str = "",
        budget_limit_usd: float = 10.0,
        allowed_models: list = None,
        routing_strategy: str = "balanced",
        workspace_base: str = "",
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
            created_at=now,
            updated_at=now,
        )
        self.db.upsert_project(project)
        print(f"[ProjectManager] 项目已创建: {name} ({project_id[:8]}) 预算=${budget_limit_usd}")
        return project

    def get_project(self, project_id: str) -> Optional[Project]:
        """查询项目详情。"""
        return self.db.get_project(project_id)

    def list_projects(self, status: str = None) -> list[Project]:
        """列出所有项目,可按状态过滤。"""
        return self.db.list_projects(status=status)

    def update_project(
        self,
        project_id: str,
        name: str = None,
        description: str = None,
        budget_limit_usd: float = None,
        allowed_models: list = None,
        routing_strategy: str = None,
        status: str = None,
    ) -> Optional[Project]:
        """更新项目字段 (仅更新非 None 的字段)。"""
        project = self.db.get_project(project_id)
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

        project.updated_at = time.time()
        self.db.upsert_project(project)
        return project

    def archive_project(self, project_id: str) -> bool:
        """归档项目 (软删除)。"""
        project = self.db.get_project(project_id)
        if not project:
            return False
        self.db.delete_project(project_id)
        print(f"[ProjectManager] 项目已归档: {project.name} ({project_id[:8]})")
        return True

    # ── 预算控制 ─────────────────────────────────────────────────

    def check_budget(self, project_id: str) -> bool:
        """检查项目预算是否充足 (未超支且状态为 active)。

        Returns:
            True 表示可以继续提交任务
        """
        project = self.db.get_project(project_id)
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
        project = self.db.get_project(project_id)
        if not project or project.budget_limit_usd <= 0:
            return 0.0
        return project.budget_used_usd / project.budget_limit_usd

    def should_use_economy_model(self, project_id: str) -> bool:
        """预算使用率超过警告阈值时,建议切换经济模型。"""
        return self.get_budget_ratio(project_id) >= self.BUDGET_WARN_RATIO

    def get_recommended_model(self, project_id: str, default_model: str = "deepseek-chat") -> str:
        """根据项目预算状况和路由策略推荐模型。

        - cost_first: 始终用经济模型
        - quality_first: 始终用 default_model
        - balanced: 预算充足用 default_model, 超过 80% 切经济模型
        """
        project = self.db.get_project(project_id)
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
        if self.should_use_economy_model(project_id):
            # 预算紧张, 切经济模型
            allowed = project.allowed_models
            if not allowed or ECONOMY_MODEL in allowed:
                return ECONOMY_MODEL
            return allowed[0]
        return default_model

    # ── 消费记录 ─────────────────────────────────────────────────

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
        self.db.record_usage(
            project_id=project_id,
            task_id=task_id,
            subtask_id=subtask_id,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost,
        )

        # 更新项目已消费金额
        project = self.db.get_project(project_id)
        if project:
            project.budget_used_usd = round(project.budget_used_usd + cost, 6)
            project.updated_at = time.time()
            self.db.upsert_project(project)

            # 检查是否超支
            self.suspend_if_over_budget(project_id)

        return cost

    def suspend_if_over_budget(self, project_id: str) -> bool:
        """如果项目超支,自动暂停。

        Returns:
            True 表示已触发暂停
        """
        project = self.db.get_project(project_id)
        if not project or project.status != ProjectStatus.ACTIVE:
            return False
        if project.budget_limit_usd <= 0:
            return False
        if project.budget_used_usd >= project.budget_limit_usd:
            self.db.update_project_status(project_id, ProjectStatus.SUSPENDED)
            print(
                f"[ProjectManager] 项目超支已暂停: {project.name} "
                f"(已用 ${project.budget_used_usd:.4f} / 预算 ${project.budget_limit_usd:.2f})"
            )
            return True
        return False

    # ── 状态查询 ─────────────────────────────────────────────────

    def get_project_status(self, project_id: str) -> Optional[dict]:
        """返回项目完整状态 (含预算使用率、消费记录摘要)。"""
        project = self.db.get_project(project_id)
        if not project:
            return None

        usage_log = self.db.get_usage_log(project_id, limit=10)
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
            "should_use_economy": self.should_use_economy_model(project_id),
        }
