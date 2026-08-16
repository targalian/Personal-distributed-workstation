"""
任务编排引擎 — 已废弃, 降级为工具库 (iter-30 收敛裁定)

历史职责 (已由 PM Agent 四件套接管, 见 03-task-orchestration 文档):
1. 接收用户任务, 分解为子任务 (规则/LLM 驱动)
2. 构建子任务 DAG, 显式状态机驱动 + Checkpoint 断点恢复
3. HTTP 分发子任务到 Worker 并聚合结果

现状:
- 任务拆解/组队/分发/监控/聚合全部由 pm_agent/pm_planner/
  pm_dispatcher/pm_monitor 承担; DAG 数据结构在 task.py,
  任务模板在 task_templates.py
- 本文件仅保留仍被引用的工具函数 (_classify_task 任务分类)
  与状态机数据定义 (GraphState/PHASE_TRANSITIONS, 供考古与文档参考)
- Orchestrator 类为兼容 stub: 实例化即抛 RuntimeError,
  待 secretary.py 历史入口清理后可整体删除本类
"""
from dataclasses import dataclass, field
from typing import Optional

from .protocol import Task
from .task import TaskDAG
from .logger import get_logger

logger = get_logger("orchestrator")


# ── 显式状态机定义 (考古资产, 无运行时引用) ──────────────────────

@dataclass
class GraphState:
    """图执行状态 — 早期状态机的核心数据结构 (已随 Orchestrator 废弃)。"""
    task_id: str = ""
    phase: str = "decompose"   # decompose|route|dispatch|monitor|aggregate|deliver|completed|failed
    dag: Optional[TaskDAG] = None
    context: dict = field(default_factory=dict)   # 累积上下文 (子任务输出、路由决策等)
    history: list = field(default_factory=list)    # [(timestamp, from_phase, to_phase, reason)]
    checkpoint_id: str = ""                        # 最近一次 checkpoint ID


# 合法状态转换表
PHASE_TRANSITIONS: dict[str, list[str]] = {
    "decompose": ["route", "failed"],
    "route": ["dispatch", "failed"],
    "dispatch": ["monitor", "failed"],
    "monitor": ["dispatch", "aggregate", "failed"],  # monitor 可回到 dispatch (动态路由)
    "aggregate": ["deliver", "failed"],
    "deliver": ["completed"],
}


def _classify_task(task: Task) -> str:
    """根据任务描述关键词分类 (规则驱动,未来可替换为 LLM)。

    复杂代码任务 (多文件/工程级) 优先路由到 CLI Agent 模板。
    """
    desc = (task.name + " " + task.description).lower()

    # 复杂代码任务 → CLI Agent (如果可用)
    _CLI_KEYWORDS = ["工程", "项目", "多文件", "重构", "迁移", "开发一个", "实现一个",
                     "project", "refactor", "migrate", "implement", "build"]
    is_code = any(k in desc for k in ["代码", "code", "函数", "function", "bug", "重构"])
    is_complex = any(k in desc for k in _CLI_KEYWORDS) or len(task.description) > 300

    if is_code and is_complex:
        # 检测 CLI Agent 是否可用
        try:
            from .agent_runtime import get_preferred_cli_agent
            if get_preferred_cli_agent():
                return "code_task_cli"
        except ImportError:
            pass
        return "code_task"

    if is_code:
        return "code_task"
    if any(k in desc for k in ["文档", "document", "摘要", "summary", "报告"]):
        return "document_task"
    if any(k in desc for k in ["系统", "system", "监控", "monitor", "shell", "命令"]):
        return "system_task"
    return "simple_task"


class Orchestrator:
    """已废弃的编排引擎兼容 stub (能力由 PM Agent 四件套接管)。

    保留类定义仅为 secretary.py 历史入口的 import 兼容;
    任何实例化都会明确报错, 指引迁移方向。
    """

    def __init__(self, *args, **kwargs):
        raise RuntimeError(
            "Orchestrator 已废弃: 任务编排由 PM Agent 四件套承担 "
            "(pm_agent/pm_planner/pm_dispatcher/pm_monitor), "
            "请勿再实例化; 本 stub 待 secretary.py 历史入口清理后删除"
        )
