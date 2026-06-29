"""
任务编排引擎 — 借鉴 LangGraph Supervisor 模式

职责:
1. 接收用户任务,分解为子任务 (规则/LLM 驱动)
2. 构建子任务 DAG (依赖图)
3. 查找匹配的空闲 Agent
4. 通过 HTTP 分发子任务到 Worker
5. 收集结果并聚合
6. 管理任务生命周期 (pending → running → completed/failed)
"""
import time
import uuid
import threading
from typing import Optional, Callable

import requests

from .database import Database
from .protocol import Task, SubTask, AgentCard
from .task import TaskDAG


# ── 预置任务分解模板 ────────────────────────────────────────────

DECOMPOSITION_TEMPLATES = {
    "code_task": [
        {"name": "分析需求", "skill": "code_review", "description": "分析需求描述,提取关键信息"},
        {"name": "生成代码", "skill": "code_generation", "description": "根据需求生成代码", "depends_on_idx": [0]},
        {"name": "代码审查", "skill": "code_review", "description": "审查生成的代码", "depends_on_idx": [1]},
    ],
    "document_task": [
        {"name": "检索资料", "skill": "rag_search", "description": "从知识库检索相关资料"},
        {"name": "生成摘要", "skill": "document_summary", "description": "基于检索结果生成摘要", "depends_on_idx": [0]},
    ],
    "system_task": [
        {"name": "检查状态", "skill": "monitoring", "description": "检查系统资源状态"},
        {"name": "执行操作", "skill": "shell_exec", "description": "执行必要的系统操作", "depends_on_idx": [0]},
    ],
    "simple_task": [
        {"name": "执行", "skill": "code_generation", "description": "直接执行任务"},
    ],
}


def _classify_task(task: Task) -> str:
    """根据任务描述关键词分类 (规则驱动,未来可替换为 LLM)。"""
    desc = (task.name + " " + task.description).lower()
    if any(k in desc for k in ["代码", "code", "函数", "function", "bug", "重构"]):
        return "code_task"
    if any(k in desc for k in ["文档", "document", "摘要", "summary", "报告"]):
        return "document_task"
    if any(k in desc for k in ["系统", "system", "监控", "monitor", "shell", "命令"]):
        return "system_task"
    return "simple_task"


class Orchestrator:
    """任务编排器。

    在 Secretary 端运行,负责任务分解、Agent 匹配、子任务分发与结果聚合。
    """

    def __init__(self, db: Database, project_manager=None, model_router=None,
                 skill_registry=None, on_event: Callable = None):
        self.db = db
        self.project_manager = project_manager
        self.model_router = model_router
        self.skill_registry = skill_registry
        self.on_event = on_event  # 事件回调: on_event(event_type: str, data: dict)
        self._lock = threading.Lock()
        self._active_dags: dict[str, TaskDAG] = {}  # task_id → DAG

    def submit_task(self, name: str, description: str, input_data: dict = None,
                    created_by: str = "user", project_id: str = "") -> Task:
        """提交新任务,自动分解并开始调度。

        如果指定了 project_id,任务将与项目关联,执行过程中记录消费。
        """
        task = Task(
            task_id=str(uuid.uuid4()),
            name=name,
            description=description,
            input_data=input_data or {},
            status="pending",
            created_by=created_by,
            project_id=project_id,
        )

        # 分解为子任务
        subtasks = self._decompose(task)
        task.subtasks = [st.to_dict() for st in subtasks]

        # 构建 DAG
        dag = TaskDAG(subtasks)
        if dag.has_cycle():
            task.status = "failed"
            task.output_data = {"error": "子任务存在循环依赖"}
        else:
            task.status = "running"
            self._active_dags[task.task_id] = dag

        self.db.save_task(task)
        print(f"[Orchestrator] 任务已提交: {task.task_id} ({name}) → {len(subtasks)} 个子任务")

        # 异步调度
        if task.status == "running":
            threading.Thread(
                target=self._schedule_loop, args=(task.task_id,), daemon=True
            ).start()

        return task

    def _decompose(self, task: Task) -> list[SubTask]:
        """将顶层任务分解为子任务。"""
        template_key = _classify_task(task)
        template = DECOMPOSITION_TEMPLATES.get(template_key, DECOMPOSITION_TEMPLATES["simple_task"])

        subtasks = []
        subtask_ids = []
        for item in template:
            sid = str(uuid.uuid4())
            subtask_ids.append(sid)
            depends_on = [subtask_ids[i] for i in item.get("depends_on_idx", [])]
            subtasks.append(SubTask(
                subtask_id=sid,
                parent_task_id=task.task_id,
                name=item["name"],
                description=item["description"],
                required_skill=item["skill"],
                input_data=task.input_data,
                depends_on=depends_on,
            ))
        return subtasks

    def _schedule_loop(self, task_id: str):
        """调度循环: 持续分发就绪的子任务直到全部完成。"""
        while task_id in self._active_dags:
            dag = self._active_dags[task_id]
            ready = dag.get_ready_subtasks()

            if not ready:
                if dag.is_all_completed():
                    self._complete_task(task_id, dag)
                    break
                if dag.has_failed():
                    self._fail_task(task_id, "部分子任务失败")
                    break
                # 等待正在执行的子任务完成
                time.sleep(2)
                continue

            # 分发每个就绪的子任务
            for st in ready:
                agent = self.db.find_idle_agent_with_skill(st.required_skill)
                if agent:
                    self._dispatch_subtask(task_id, st, agent, dag)
                # 如果没有空闲 Agent,等待重试
            time.sleep(2)

    def _dispatch_subtask(self, task_id: str, subtask: SubTask, agent: AgentCard, dag: TaskDAG):
        """分发子任务到目标 Agent (HTTP 调用)。

        在分发前调用 ModelRouter 确定推荐模型和降级链,
        将 model_preference 和 fallback_models 写入 payload。
        """
        dag.update_subtask(subtask.subtask_id, status="assigned", assigned_agent_id=agent.agent_id)
        self._sync_dag_to_db(task_id, dag)

        # 标记 Agent 为忙碌
        self.db.update_agent_status(agent.agent_id, "busy", agent.current_task_count + 1)

        # 模型路由决策
        model_preference = ""
        fallback_models = []
        if self.model_router:
            parent_task = self.db.get_task(task_id)
            project_id = parent_task.project_id if parent_task else ""
            routing = self.model_router.route(
                text=subtask.description,
                skill=subtask.required_skill,
                project_id=project_id,
            )
            model_preference = routing.selected_model
            fallback_models = routing.fallback_chain
            print(
                f"[Router] {subtask.name} → {routing.selected_model} "
                f"(难度={routing.difficulty}, 评分={routing.score:.3f}, 策略={routing.strategy})"
            )

        # HTTP 调用 Worker 执行
        url = f"http://{agent.ip}:{agent.api_port}/tasks/execute"
        payload = {
            "subtask_id": subtask.subtask_id,
            "parent_task_id": task_id,
            "name": subtask.name,
            "description": subtask.description,
            "required_skill": subtask.required_skill,
            "input_data": subtask.input_data,
            "model_preference": model_preference,
            "fallback_models": fallback_models,
        }

        def _execute_and_collect():
            try:
                resp = requests.post(url, json=payload, timeout=300)
                if resp.status_code == 200:
                    result = resp.json()
                    dag.update_subtask(
                        subtask.subtask_id,
                        status="completed",
                        output_data=result.get("output", {}),
                        completed_at=time.time(),
                    )
                    # 记录模型调用消费 (如果关联了项目)
                    if self.project_manager:
                        task_obj = self.db.get_task(task_id)
                        if task_obj and task_obj.project_id:
                            usage = result.get("usage", {})
                            if usage:
                                self.project_manager.record_usage(
                                    project_id=task_obj.project_id,
                                    task_id=task_id,
                                    subtask_id=subtask.subtask_id,
                                    model=usage.get("model", ""),
                                    input_tokens=usage.get("input_tokens", 0),
                                    output_tokens=usage.get("output_tokens", 0),
                                )
                    print(f"[Orchestrator] 子任务完成: {subtask.name} → {agent.agent_name}")
                else:
                    dag.update_subtask(
                        subtask.subtask_id,
                        status="failed",
                        error=f"HTTP {resp.status_code}: {resp.text}",
                        completed_at=time.time(),
                    )
                    print(f"[Orchestrator] 子任务失败: {subtask.name} → HTTP {resp.status_code}")
            except requests.RequestException as e:
                dag.update_subtask(
                    subtask.subtask_id,
                    status="failed",
                    error=str(e),
                    completed_at=time.time(),
                )
                print(f"[Orchestrator] 子任务异常: {subtask.name} → {e}")
            finally:
                # 恢复 Agent 状态
                current = self.db.get_agent(agent.agent_id)
                new_count = max(0, (current.current_task_count - 1) if current else 0)
                new_status = "idle" if new_count < agent.max_concurrent_tasks else "busy"
                self.db.update_agent_status(agent.agent_id, new_status, new_count)
                self._sync_dag_to_db(task_id, dag)

        threading.Thread(target=_execute_and_collect, daemon=True).start()

    def _sync_dag_to_db(self, task_id: str, dag: TaskDAG):
        """将 DAG 状态同步到数据库。"""
        task = self.db.get_task(task_id)
        if task:
            task.subtasks = [st.to_dict() for st in dag.to_subtask_list()]
            self.db.save_task(task)

    def _complete_task(self, task_id: str, dag: TaskDAG):
        """标记任务完成并聚合结果。"""
        task = self.db.get_task(task_id)
        if task:
            task.status = "completed"
            task.completed_at = time.time()
            task.output_data = {
                st.name: st.output_data
                for st in dag.to_subtask_list()
            }
            self.db.save_task(task)
            self._active_dags.pop(task_id, None)
            print(f"[Orchestrator] 任务完成: {task_id}")
            if self.on_event:
                self.on_event("task_completed", {
                    "task_id": task_id,
                    "name": task.name,
                })

    def _fail_task(self, task_id: str, reason: str):
        """标记任务失败。"""
        task = self.db.get_task(task_id)
        if task:
            task.status = "failed"
            task.output_data = {"error": reason}
            self.db.save_task(task)
            self._active_dags.pop(task_id, None)
            print(f"[Orchestrator] 任务失败: {task_id} ({reason})")
            if self.on_event:
                self.on_event("task_failed", {
                    "task_id": task_id,
                    "name": task.name,
                    "reason": reason,
                })

    def get_task_status(self, task_id: str) -> Optional[Task]:
        """查询任务状态。"""
        return self.db.get_task(task_id)
