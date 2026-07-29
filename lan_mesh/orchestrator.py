"""
任务编排引擎 — 借鉴 LangGraph Supervisor 模式 (增强版: 显式状态机 + Checkpoint)

职责:
1. 接收用户任务,分解为子任务 (规则/LLM 驱动)
2. 构建子任务 DAG (依赖图)
3. 查找匹配的空闲 Agent
4. 通过 HTTP 分发子任务到 Worker
5. 收集结果并聚合
6. 管理任务生命周期 (pending → running → completed/failed)
7. 显式状态机驱动: decompose → route → dispatch → monitor → aggregate → deliver
8. 自动 Checkpoint: 每次状态转换时持久化图状态, 支持断点恢复
"""
import json
import time
import uuid
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Optional, Callable

import requests

from .database import Database
from .protocol import Task, SubTask, AgentCard
from .task import TaskDAG
from .logger import get_logger

logger = get_logger("orchestrator")

# ── 子任务执行配置 ─────────────────────────────────────────────
SUBTASK_TIMEOUT_SECS = 300        # 单个子任务最大执行时间
SUBTASK_MAX_RETRIES = 2           # 最大重试次数
SUBTASK_RETRY_BASE_DELAY = 3      # 重试基础延迟 (指数退避)
EXECUTOR_MAX_WORKERS = 8          # 线程池最大并发数


# ── 显式状态机定义 ─────────────────────────────────────────────

@dataclass
class GraphState:
    """图执行状态 — 状态机的核心数据结构。

    每次状态转换时自动保存 checkpoint,
    支持从任意 phase 断点恢复。
    """
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


# ── 预置任务分解模板 ────────────────────────────────────────────

DECOMPOSITION_TEMPLATES = {
    "code_task": [
        {"name": "分析需求", "skill": "code_review", "description": "分析需求描述,提取关键信息"},
        {"name": "生成代码", "skill": "code_generation", "description": "根据需求生成代码", "depends_on_idx": [0]},
        {"name": "代码审查", "skill": "code_review", "description": "审查生成的代码", "depends_on_idx": [1]},
    ],
    "code_task_cli": [
        {"name": "CLI Agent 自主编码", "skill": "cli_agent", "description": "调用 CLI Agent 自主完成编码任务 (探索/编辑/测试/修复)"},
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
    """任务编排器 (状态机驱动)。

    在 Secretary 端运行,负责任务分解、Agent 匹配、子任务分发与结果聚合。
    增强: 显式状态机 + 自动 Checkpoint + 断点恢复 + 超时重试 + 线程池。
    """

    def __init__(self, db: Database, project_manager=None, model_router=None,
                 skill_registry=None, on_event: Callable = None):
        self.db = db
        self.project_manager = project_manager
        self.model_router = model_router
        self.skill_registry = skill_registry
        self.on_event = on_event  # 事件回调: on_event(event_type: str, data: dict)
        self._lock = threading.RLock()  # 保护 _active_dags / _active_states 的并发访问
        self._active_dags: dict[str, TaskDAG] = {}  # task_id → DAG
        self._active_states: dict[str, GraphState] = {}  # task_id → GraphState
        self._executor = ThreadPoolExecutor(
            max_workers=EXECUTOR_MAX_WORKERS, thread_name_prefix="orch"
        )
        self._task_events: dict[str, threading.Event] = {}  # task_id → 完成通知

    # ── 线程安全的状态访问辅助方法 ─────────────────────────────

    def _get_state(self, task_id: str) -> Optional[GraphState]:
        """线程安全获取 GraphState。"""
        with self._lock:
            return self._active_states.get(task_id)

    def _set_state(self, task_id: str, state: GraphState):
        """线程安全设置 GraphState。"""
        with self._lock:
            self._active_states[task_id] = state

    def _remove_state(self, task_id: str):
        """线程安全移除活跃状态。"""
        with self._lock:
            self._active_states.pop(task_id, None)
            self._active_dags.pop(task_id, None)
            self._task_events.pop(task_id, None)

    def _get_dag(self, task_id: str) -> Optional[TaskDAG]:
        """线程安全获取 DAG。"""
        with self._lock:
            return self._active_dags.get(task_id)

    def _set_dag(self, task_id: str, dag: TaskDAG):
        """线程安全设置 DAG。"""
        with self._lock:
            self._active_dags[task_id] = dag

    def _has_active_state(self, task_id: str) -> bool:
        """线程安全检查任务是否活跃。"""
        with self._lock:
            return task_id in self._active_states

    # ── 状态机核心 ─────────────────────────────────────────────

    def transition(self, state: GraphState, to_phase: str, reason: str = "") -> bool:
        """状态转换 (校验合法性 + 记录 history + 触发 checkpoint)。

        Args:
            state: 当前图状态
            to_phase: 目标 phase
            reason: 转换原因 (记录到 history)

        Returns:
            True 转换成功, False 转换非法
        """
        allowed = PHASE_TRANSITIONS.get(state.phase, [])
        if to_phase not in allowed:
            logger.warning("非法状态转换: %s → %s (允许: %s)", state.phase, to_phase, allowed)
            return False

        from_phase = state.phase
        state.phase = to_phase
        state.history.append((time.time(), from_phase, to_phase, reason))

        # 自动保存 checkpoint
        self._save_checkpoint(state)

        logger.info("状态转换: %s → %s (%s)", from_phase, to_phase, reason)
        if self.on_event:
            self.on_event("phase_transition", {
                "task_id": state.task_id,
                "from_phase": from_phase,
                "to_phase": to_phase,
                "reason": reason,
            })
        return True

    def _save_checkpoint(self, state: GraphState):
        """保存当前图状态到数据库 checkpoint。"""
        checkpoint_id = str(uuid.uuid4())
        state.checkpoint_id = checkpoint_id

        dag_json = "{}"
        if state.dag:
            dag_json = json.dumps(state.dag.to_graph_json(), ensure_ascii=False)

        context_json = json.dumps(state.context, ensure_ascii=False, default=str)
        history_json = json.dumps(state.history, ensure_ascii=False, default=str)

        self.db.save_checkpoint(
            checkpoint_id=checkpoint_id,
            task_id=state.task_id,
            phase=state.phase,
            dag_json=dag_json,
            context_json=context_json,
            history_json=history_json,
        )

    def resume_task(self, task_id: str) -> bool:
        """从 checkpoint 恢复任务执行。

        流程:
        1. 从 DB 加载最新 checkpoint
        2. 重建 TaskDAG (from_graph_json)
        3. 恢复 GraphState (phase + context + history)
        4. 重新启动状态机循环

        Args:
            task_id: 要恢复的任务 ID

        Returns:
            True 恢复成功, False 无 checkpoint 或恢复失败
        """
        ckpt = self.db.get_latest_checkpoint(task_id)
        if not ckpt:
            logger.warning("任务 %s 无 checkpoint, 无法恢复", task_id)
            return False

        try:
            dag_data = json.loads(ckpt.get("dag_json", "{}"))
            context = json.loads(ckpt.get("context_json", "{}"))
            history = json.loads(ckpt.get("history_json", "[]"))
        except (json.JSONDecodeError, TypeError):
            logger.error("checkpoint 数据解析失败")
            return False

        # 重建 DAG
        dag = TaskDAG.from_graph_json(dag_data) if dag_data.get("nodes") else None

        # 恢复 GraphState
        state = GraphState(
            task_id=task_id,
            phase=ckpt.get("phase", "dispatch"),
            dag=dag,
            context=context,
            history=history,
            checkpoint_id=ckpt.get("checkpoint_id", ""),
        )

        # 线程安全注册到活跃状态
        with self._lock:
            self._active_states[task_id] = state
            if dag:
                self._active_dags[task_id] = dag

        # 更新任务状态
        task = self.db.get_task(task_id)
        if task:
            task.status = "running"
            self.db.save_task(task)

        logger.info("任务已从 checkpoint 恢复: %s (phase=%s)", task_id, state.phase)

        # 启动状态机循环
        threading.Thread(
            target=self._state_machine_loop, args=(task_id,), daemon=True
        ).start()
        return True

    def submit_task(self, name: str, description: str, input_data: dict = None,
                    created_by: str = "user", project_id: str = "") -> Task:
        """提交新任务,自动分解并开始调度 (状态机驱动)。"""
        task = Task(
            task_id=str(uuid.uuid4()),
            name=name,
            description=description,
            input_data=input_data or {},
            status="pending",
            created_by=created_by,
            project_id=project_id,
        )

        # 初始化 GraphState
        state = GraphState(task_id=task.task_id, phase="decompose", context={
            "task_name": name,
            "task_description": description,
            "input_data": input_data or {},
            "project_id": project_id,
        })

        # 分解为子任务
        subtasks = self._decompose(task)
        task.subtasks = [st.to_dict() for st in subtasks]

        # 构建 DAG
        dag = TaskDAG(subtasks)
        if dag.has_cycle():
            task.status = "failed"
            task.output_data = {"error": "子任务存在循环依赖"}
            state.phase = "failed"
        else:
            task.status = "running"
            state.dag = dag

        # 线程安全注册活跃状态
        with self._lock:
            self._active_states[task.task_id] = state
            if task.status == "running" and state.dag:
                self._active_dags[task.task_id] = state.dag

        self.db.save_task(task)
        logger.info("任务已提交: %s (%s) → %d 个子任务", task.task_id, name, len(subtasks))

        # 启动状态机循环
        if task.status == "running":
            self.transition(state, "route", "分解完成")
            threading.Thread(
                target=self._state_machine_loop, args=(task.task_id,), daemon=True
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

    # ── 状态机循环 (替代原 _schedule_loop) ─────────────────────

    def _get_task_event(self, task_id: str) -> threading.Event:
        """获取或创建任务完成通知事件。"""
        with self._lock:
            if task_id not in self._task_events:
                self._task_events[task_id] = threading.Event()
            return self._task_events[task_id]

    def _notify_task_event(self, task_id: str):
        """通知状态机有子任务状态变更。"""
        with self._lock:
            evt = self._task_events.get(task_id)
        if evt:
            evt.set()

    def _state_machine_loop(self, task_id: str):
        """状态机驱动循环: 根据当前 phase 执行对应处理函数。"""
        while self._has_active_state(task_id):
            state = self._get_state(task_id)
            if not state:
                break

            if state.phase == "route":
                self._phase_route(state)
            elif state.phase == "dispatch":
                self._phase_dispatch(state)
            elif state.phase == "monitor":
                self._phase_monitor(state)
            elif state.phase == "aggregate":
                self._phase_aggregate(state)
            elif state.phase == "deliver":
                self._phase_deliver(state)
            elif state.phase in ("completed", "failed"):
                break
            else:
                time.sleep(1)

    def _phase_route(self, state: GraphState):
        """路由阶段: 为各子任务确定模型路由 (当前简化为直接进入 dispatch)。"""
        # 未来可在此处为每个子任务预计算模型路由
        self.transition(state, "dispatch", "路由决策完成")

    def _phase_dispatch(self, state: GraphState):
        """分发阶段: 分发所有就绪的子任务到 Agent。"""
        dag = state.dag
        if not dag:
            self.transition(state, "failed", "DAG 不存在")
            return

        ready = dag.get_ready_subtasks(context=state.context)
        if not ready:
            # 无就绪子任务, 检查是否全部完成
            if dag.is_all_completed():
                self.transition(state, "aggregate", "所有子任务已完成")
                return
            if dag.has_failed():
                self.transition(state, "failed", "部分子任务失败")
                self._fail_task(state.task_id, "部分子任务失败")
                return
            # 等待正在执行的子任务
            time.sleep(2)
            self.transition(state, "monitor", "等待执行中子任务")
            return

        # 分发每个就绪的子任务
        for st in ready:
            agent = self.db.find_idle_agent_with_skill(st.required_skill)
            if agent:
                self._dispatch_subtask(state.task_id, st, agent, dag)

        self.transition(state, "monitor", f"已分发 {len(ready)} 个子任务")

    def _phase_monitor(self, state: GraphState):
        """监控阶段: 事件驱动等待子任务完成, 根据结果决定下一步。"""
        dag = state.dag
        if not dag:
            self.transition(state, "failed", "DAG 不存在")
            return

        # 事件驱动: 等待子任务完成通知 (替代 time.sleep 轮询)
        evt = self._get_task_event(state.task_id)
        evt.clear()
        evt.wait(timeout=5)  # 最多等 5s, 兼顾超时检测

        # 超时检测: 标记超时子任务
        self._check_subtask_timeouts(dag)

        # 收集已完成的子任务输出到 context
        for st in dag.to_subtask_list():
            if st.status == "completed" and st.output_data:
                state.context[f"output_{st.name}"] = st.output_data

        # 条件路由: 决定下一步
        if dag.is_all_completed():
            self.transition(state, "aggregate", "所有子任务完成, 进入聚合")
        elif dag.has_failed():
            # 尝试重试失败的子任务
            if self._retry_failed_subtasks(state, dag):
                self.transition(state, "dispatch", "重试失败子任务")
            else:
                self.transition(state, "failed", "子任务失败 (重试耗尽)")
                self._fail_task(state.task_id, "部分子任务失败")
        elif dag.get_ready_subtasks(context=state.context):
            # 还有就绪子任务 (动态路由: 回到 dispatch)
            self.transition(state, "dispatch", "有新的就绪子任务 (动态路由)")
        # else: 仍在执行中, 下次循环继续等待事件

    def _phase_aggregate(self, state: GraphState):
        """聚合阶段: 收集所有子任务结果。"""
        dag = state.dag
        if dag:
            state.context["aggregated_outputs"] = {
                st.name: st.output_data
                for st in dag.to_subtask_list()
            }
        self.transition(state, "deliver", "聚合完成")

    def _phase_deliver(self, state: GraphState):
        """交付阶段: 标记任务完成。"""
        self._complete_task(state.task_id, state.dag)
        self.transition(state, "completed", "任务交付完成")
        # 线程安全清理活跃状态
        self._remove_state(state.task_id)

    # ── 兼容旧接口 (保留 _schedule_loop 作为别名) ─────────────

    def _schedule_loop(self, task_id: str):
        """兼容旧接口: 转发到状态机循环。"""
        self._state_machine_loop(task_id)

    def _dispatch_subtask(self, task_id: str, subtask: SubTask, agent: AgentCard, dag: TaskDAG):
        """分发子任务到目标 Agent (HTTP 调用, 线程池执行, 含超时控制)。

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
            logger.info(
                "[Router] %s → %s (难度=%s, 评分=%.3f, 策略=%s)",
                subtask.name, routing.selected_model,
                routing.difficulty, routing.score, routing.strategy,
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
                resp = requests.post(url, json=payload, timeout=SUBTASK_TIMEOUT_SECS)
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
                    logger.info("子任务完成: %s → %s", subtask.name, agent.agent_name)
                else:
                    dag.update_subtask(
                        subtask.subtask_id,
                        status="failed",
                        error=f"HTTP {resp.status_code}: {resp.text[:200]}",
                        completed_at=time.time(),
                    )
                    logger.error("子任务失败: %s → HTTP %d", subtask.name, resp.status_code)
            except requests.Timeout:
                dag.update_subtask(
                    subtask.subtask_id,
                    status="failed",
                    error=f"执行超时 ({SUBTASK_TIMEOUT_SECS}s)",
                    completed_at=time.time(),
                )
                logger.error("子任务超时: %s (%ds)", subtask.name, SUBTASK_TIMEOUT_SECS)
            except requests.RequestException as e:
                dag.update_subtask(
                    subtask.subtask_id,
                    status="failed",
                    error=str(e)[:200],
                    completed_at=time.time(),
                )
                logger.error("子任务异常: %s → %s", subtask.name, e)
            finally:
                # 恢复 Agent 状态
                current = self.db.get_agent(agent.agent_id)
                new_count = max(0, (current.current_task_count - 1) if current else 0)
                new_status = "idle" if new_count < agent.max_concurrent_tasks else "busy"
                self.db.update_agent_status(agent.agent_id, new_status, new_count)
                self._sync_dag_to_db(task_id, dag)
                # 事件通知: 唤醒状态机循环
                self._notify_task_event(task_id)

        # 使用线程池替代裸线程
        self._executor.submit(_execute_and_collect)

    def _check_subtask_timeouts(self, dag: TaskDAG):
        """检测并标记超时的子任务 (assigned/running 状态超过 SUBTASK_TIMEOUT_SECS)。"""
        now = time.time()
        for st in dag.to_subtask_list():
            if st.status in ("assigned", "running"):
                # 使用 assigned_at 或 created_at 作为起始时间
                start = getattr(st, "assigned_at", None) or getattr(st, "created_at", None) or 0
                if start and (now - start) > SUBTASK_TIMEOUT_SECS:
                    dag.update_subtask(
                        st.subtask_id,
                        status="failed",
                        error=f"监控超时 ({SUBTASK_TIMEOUT_SECS}s 无响应)",
                        completed_at=now,
                    )
                    logger.warning("子任务监控超时: %s", st.name)

    def _retry_failed_subtasks(self, state: GraphState, dag: TaskDAG) -> bool:
        """尝试重试失败的子任务 (指数退避)。

        Returns:
            True 如果有子任务被重置为 pending 等待重新分发, False 重试耗尽。
        """
        retried = False
        for st in dag.to_subtask_list():
            if st.status != "failed":
                continue
            retry_count = getattr(st, "retry_count", 0)
            if retry_count >= SUBTASK_MAX_RETRIES:
                continue  # 重试次数耗尽
            # 指数退避延迟检查
            last_failed_at = getattr(st, "completed_at", 0) or 0
            delay = SUBTASK_RETRY_BASE_DELAY * (2 ** retry_count)
            if time.time() - last_failed_at < delay:
                continue  # 还没到重试时间
            # 重置为 pending, 等待重新分发
            dag.update_subtask(
                st.subtask_id,
                status="pending",
                error="",
                output_data=None,
                completed_at=None,
            )
            # 记录重试次数 (通过 input_data 携带)
            st.retry_count = retry_count + 1
            logger.info("子任务重试 (%d/%d): %s", retry_count + 1, SUBTASK_MAX_RETRIES, st.name)
            retried = True
        return retried

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
            } if dag else {}
            self.db.save_task(task)
            self._remove_state(task_id)
            logger.info("任务完成: %s", task_id)
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
            self._remove_state(task_id)
            logger.error("任务失败: %s (%s)", task_id, reason)
            if self.on_event:
                self.on_event("task_failed", {
                    "task_id": task_id,
                    "name": task.name,
                    "reason": reason,
                })

    def get_task_status(self, task_id: str) -> Optional[Task]:
        """查询任务状态。"""
        return self.db.get_task(task_id)

    def get_graph_state(self, task_id: str) -> Optional[dict]:
        """获取任务的当前图状态 (供 API 层查询)。

        Returns:
            {phase, history, checkpoint_id, dag_json} 或 None
        """
        state = self._get_state(task_id)
        if state:
            return {
                "task_id": task_id,
                "phase": state.phase,
                "history": state.history,
                "checkpoint_id": state.checkpoint_id,
                "dag": state.dag.to_graph_json() if state.dag else None,
            }
        # 尝试从 checkpoint 加载
        ckpt = self.db.get_latest_checkpoint(task_id)
        if ckpt:
            return {
                "task_id": task_id,
                "phase": ckpt.get("phase", ""),
                "history": json.loads(ckpt.get("history_json", "[]")),
                "checkpoint_id": ckpt.get("checkpoint_id", ""),
                "dag": json.loads(ckpt.get("dag_json", "{}")),
            }
        return None

    def get_task_graph(self, task_id: str) -> Optional[dict]:
        """获取任务的 DAG 图结构 JSON (供前端渲染)。

        优先从活跃状态获取, 否则从 DB 中的 task.subtasks 重建。
        """
        # 优先活跃 DAG
        dag = self._get_dag(task_id)
        if dag:
            return dag.to_graph_json()

        # 从 checkpoint 获取
        ckpt = self.db.get_latest_checkpoint(task_id)
        if ckpt:
            try:
                return json.loads(ckpt.get("dag_json", "{}"))
            except (json.JSONDecodeError, TypeError):
                pass

        # 从 task.subtasks 重建
        task = self.db.get_task(task_id)
        if task and task.subtasks:
            subtasks = [SubTask.from_dict(st) for st in task.subtasks]
            dag = TaskDAG(subtasks)
            return dag.to_graph_json()

        return None

    def update_task_graph(self, task_id: str, graph_data: dict) -> dict:
        """更新任务的 DAG 图结构 (前端编辑保存)。

        Args:
            task_id: 任务 ID
            graph_data: {"nodes": [...], "edges": [...]}

        Returns:
            {"ok": bool, "message": str}
        """
        # 重建 DAG 并验证
        new_dag = TaskDAG.from_graph_json(graph_data)
        if new_dag.has_cycle():
            return {"ok": False, "message": "图结构存在循环依赖, 无法保存"}

        # 线程安全更新活跃 DAG
        with self._lock:
            if task_id in self._active_dags:
                self._active_dags[task_id] = new_dag
            if task_id in self._active_states:
                self._active_states[task_id].dag = new_dag

        # 同步到 DB
        task = self.db.get_task(task_id)
        if task:
            task.subtasks = [st.to_dict() for st in new_dag.to_subtask_list()]
            self.db.save_task(task)

        return {"ok": True, "message": "DAG 图结构已更新"}

    def shutdown(self, wait: bool = True, timeout: float = 10):
        """优雅关闭编排器: 等待线程池中正在执行的子任务完成。"""
        logger.info("编排器关闭中 (wait=%s)...", wait)
        self._executor.shutdown(wait=wait)
        logger.info("编排器线程池已关闭")
