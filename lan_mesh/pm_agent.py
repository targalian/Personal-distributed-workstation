"""PM Agent 协调器/门面 — 持有 Planner/Dispatcher/Monitor, 对外暴露统一接口。

子模块: pm_planner / pm_dispatcher / pm_monitor / pm_state
"""
import threading
import time
import uuid
from typing import Optional

from .http_retry import http_post, http_get
from .logger import get_logger
from .pm_activity import activity, register
from .pm_state import PMState
from .pm_planner import PMPlanner
from .pm_dispatcher import PMDispatcher
from .pm_monitor import PMMonitor

logger = get_logger("pm")


class ProjectManagerAgent:
    """项目经理 Agent — 运行在 Worker 进程内, 协调规划/分发/监控子组件。"""

    def __init__(self, pm_id: str, agent_runtime, secretary_url: str,
                 device_id: str, device_name: str = ""):
        self.pm_id = pm_id
        self.runtime = agent_runtime
        self.secretary_url = secretary_url.rstrip("/")
        self.device_id = device_id
        self.device_name = device_name

        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._progress_thread: Optional[threading.Thread] = None
        self._started = False

        # ── 共享状态 ──
        self._state = PMState()
        self._checkpoint_store = None
        self._checkpoint_revision = 0
        self._checkpoint_error = False
        self._resume_authorization = None
        self._state.persist = self.checkpoint
        register(self)

        # ── 子组件 ──
        self._planner = PMPlanner(pm_id, agent_runtime, self._state, self)
        self._dispatcher = PMDispatcher(
            pm_id, agent_runtime, self.secretary_url, device_id, self._state, self
        )
        self._monitor = PMMonitor(
            pm_id, agent_runtime, self.secretary_url, self._state, self, self._dispatcher
        )

    @property
    def running(self) -> bool:
        return self._running

    @running.setter
    def running(self, value: bool):
        self._running = value

    def checkpoint(self, phase: str = "") -> bool:
        """Stop future scheduling if durable evidence cannot be written."""
        st = self._state
        with st.lock:
            if self._checkpoint_error or self._retired:
                return False
            if st.stop_event.is_set() and phase and phase != st.stop_reason:
                return False
            if phase:
                st.phase = phase
            if self._checkpoint_store is None:
                return True  # Directly constructed test/legacy instances have no store.
            try:
                snapshot = {**st.snapshot(), "secretary_url": self.secretary_url, "device_name": self.device_name}
                self._checkpoint_revision = self._checkpoint_store.save(
                    self.pm_id, st.task.get("task_id", ""), self.device_id, snapshot, self._checkpoint_revision)
                return True
            except Exception as exc:
                self._checkpoint_error = True
                st.stop_reason = "checkpoint_failed"
                st.stop_event.set()
                st.clarification_event.set()
                self._running = False
                logger.error("[%s] 检查点写入失败，停止后续调度，原执行需核实: %s", self.pm_id[:8], exc)
                return False

    def start_task(self, task: dict):
        """接管任务，开始规划与执行 (异步)。"""
        with self._state.lock:
            if self._started or self._state.stop_event.is_set():
                raise RuntimeError("PM 实例已接管过任务，请创建新实例")
            self._started = True
            self._state.task = task
            self._running = True
            self._state.start_time = time.time()
            if not self.checkpoint("starting"):
                raise RuntimeError("PM 检查点不可写，未启动工作线程")

        # 加载 multi-agent-architect skill
        self._planner.load_skill()

        # 启动工作线程
        self._thread = threading.Thread(
            target=self._run_task, args=(task,), daemon=True, name=f"pm-{self.pm_id[:8]}"
        )
        self._thread.start()

        # 启动进度收集线程
        self._progress_thread = threading.Thread(
            target=self._monitor.progress_loop, daemon=True, name=f"pm-progress-{self.pm_id[:8]}"
        )
        self._progress_thread.start()

    def stop(self):
        self._signal_stop("stopped")

    def _signal_stop(self, reason: str):
        with self._state.lock:
            if reason == "stopped" and self._state.stop_reason in ("paused", "cancelled", "checkpoint_failed"):
                return  # Service shutdown must not erase a persisted business stop.
            if reason == "paused":
                from .pm_pause import begin_pause
                begin_pause(self._state)
            self._state.stop_reason = reason
            self._state.stop_event.set()
            self._running = False
            self._state.clarification_event.set()
            self.checkpoint(reason if self._state.phase not in ("completed", "failed") else "")

    def get_status(self) -> dict:
        st = self._state
        return {
            "pm_id": self.pm_id,
            "task_id": st.task.get("task_id", ""),
            "device_id": self.device_id,
            "running": self._running,
            "teams": len(st.teams),
            "subagents": len(st.subagents),
            "control_receipt": dict(st.control_receipt),
            "checkpoint_error": self._checkpoint_error,
        }

    @activity
    def _run_task(self, task: dict):
        st = self._state
        keep_monitoring = False
        try:
            if st.stop_event.is_set():
                return
            if self._monitor.is_global_timed_out():
                raise TimeoutError(f"全局任务超时 ({st.global_timeout}s)")

            # F2.3: 多轮任务细化
            if not self.checkpoint("refining"):
                return
            task = self._planner.refine_requirements(task)
            if st.stop_event.is_set():
                return
            if self._monitor.is_global_timed_out():
                self._monitor._fail_task("全局超时", "澄清结束时原任务全局时限已耗尽")
                return

            # 阶段 1: 规划
            self.report_status("planning")
            if not self.checkpoint("planning"):
                return
            plan = st.pause.get("returned", {}).get("planning")
            if plan is None:
                plan = self._planner.analyze_with_skill(task)
            if st.stop_event.is_set():
                from .pm_pause import retain_paused_result
                retain_paused_result(self, "planning", plan)
                return
            logger.info("[%s] 规划完成: 模式=%s, 子任务=%d",
                       self.pm_id[:8], plan.get('pattern', 'single'),
                       len(plan.get('decomposition', [])))

            with st.lock:
                if st.stop_event.is_set():
                    from .pm_pause import retain_paused_result
                    retain_paused_result(self, "planning", plan)
                    return
                st.plan = plan
                st.task = task
                if not self.checkpoint("planned"):
                    return

            # 阶段 2: 执行
            pattern = plan.get("pattern", "single")
            if pattern == "single":
                self._execute_single(task, plan)
            else:
                self.report_status("executing", collaboration_mode=pattern,
                                   task_list=plan.get("decomposition", []))
                self.sync_subtasks()
                self._dispatcher.create_team_and_dispatch(task, plan)
                if st.stop_event.is_set():
                    return
                self.report_status("monitoring")
                keep_monitoring = True
                with st.lock:
                    st.monitoring_ready.set()
                    self.checkpoint("monitoring")

        except Exception as e:
            logger.error("[%s] 任务执行失败: %s", self.pm_id[:8], e)
            self.report_status("failed")
            self.report_progress(0.0, "failed", str(e))

        finally:
            if not keep_monitoring:
                self._running = False

    def execution_allowed(self) -> bool:
        """Recovered instances recheck live authorization before new effects."""
        if self._state.stop_event.is_set():
            return False
        if self._resume_authorization is None:
            return True
        try:
            if self._monitor.is_global_timed_out():
                raise ValueError("原任务时限已耗尽")
            self._resume_authorization()
            return True
        except Exception as exc:
            logger.warning("[%s] 恢复授权失效，停止后续执行: %s", self.pm_id[:8], exc)
            self._signal_stop("recovery_blocked")
            return False

    def _execute_single(self, task: dict, plan: dict):
        st = self._state
        self.report_status("executing", collaboration_mode="single")
        if not plan.get("decomposition"):
            plan["decomposition"] = [{
                "name": task.get("name", "执行"),
                "skill": "code_generation",
                "depends_on": [],
                "description": task.get("description", ""),
            }]
        self.sync_subtasks()
        if not self.execution_allowed():
            return
        existing = st.direct_execution if st.recovery else {}
        names = [sub["name"] for sub in plan["decomposition"]]
        if st.recovery and all(name in st.subtask_outputs for name in names):
            result = st.subtask_outputs[names[0]]
        elif existing.get("state") == "returned":
            result = existing["result"]
        elif existing:
            raise ValueError("本地直接执行结果未知，禁止重复执行")
        else:
            with st.lock:
                st.direct_execution = {"execution_id": str(uuid.uuid4()), "state": "active"}
                if not self.checkpoint("executing_single"):
                    return
            execution_id = st.direct_execution["execution_id"]
            result = self._planner.execute_directly(task)
            with st.lock:
                if st.stop_event.is_set():
                    from .pm_pause import retain_paused_result
                    retain_paused_result(self, "direct", result, execution_id=execution_id)
                    return
                st.direct_execution.update(state="returned", result=result)
                if not self.checkpoint():
                    return

        # 校验: 如果执行结果是错误信息, 不能当作完成交付
        summary = result.get("summary", "")
        if result.get("status") == "failed" or summary.startswith(("[未配置", "[LLM 调用失败", "[模型调用失败")):
            error_msg = summary or "执行失败"
            logger.error("[%s] 任务执行失败 (LLM 错误): %s", self.pm_id[:8], error_msg)
            self.report_status("failed")
            self.report_progress(0.0, "failed", error_msg)
            return

        with st.lock:
            if st.stop_event.is_set():
                return
            for sub in plan.get("decomposition", []):
                st.subtask_outputs[sub.get("name", "")] = result
            st.direct_execution["state"] = "completed"
            if not self.checkpoint("executed_single"):
                return
        self.sync_subtasks()

        # 构建交付物并上报 (修复: single 模式也必须创建交付记录)
        task_name = task.get("name", "")
        task_desc = task.get("description", "")
        deliverable = result.get("summary", "")
        # 尝试从 runtime 结果中提取完整代码
        full_output = st.subtask_outputs.get(plan["decomposition"][0].get("name", ""), {})
        if isinstance(full_output, dict) and full_output.get("summary"):
            deliverable = full_output["summary"]
        subtask_results = [{"name": s.get("name", ""), "status": "completed", "output": result}
                           for s in plan.get("decomposition", [])]
        if not self.deliver_result(task_name, task_desc, deliverable, subtask_results):
            self.report_status("failed")
            self.report_progress(0.0, "failed", "交付未获 Secretary 确认，请核实交付记录后处理")
            return

        self.report_status("completed", task_list=[{"name": task_name, "status": "completed"}])
        self.report_progress(1.0, "completed", f"任务完成: {result.get('summary', '')[:100]}")

    def receive_progress_report(self, report: dict):
        self._monitor.receive_progress_report(report)

    def receive_subtask_result(self, task_name: str, status: str,
                               output_data: dict, agent_id: str = "", error: str = "",
                               execution_id: str = ""):
        """接收本机子 Agent 执行结果，转化为进度报告注入 Monitor。

        Station 内嵌模式下，子任务执行完成后由 station_controller 调用此方法，
        让 PM Monitor 感知子任务完成/失败，触发依赖链分发或失败接管。
        """
        report = {
            "reporter_id": agent_id,
            "subtask_id": execution_id,
            "pm_id": self.pm_id,
            "parent_task_id": self._state.task.get("task_id", ""),
            "task_name": task_name,
            "status": status,
            "progress": 1.0 if status == "completed" else 0.0,
            "output": output_data,
            "message": error or f"子任务 {task_name} {status}",
        }
        self._monitor.receive_progress_report(report)

    @activity
    def receive_input(self, input_data: dict) -> dict:
        from .pm_clarification import receive_answer
        return receive_answer(self, input_data)

    def request_clarification(self, question: str, options: list = None,
                              timeout: float = None, continuation: dict = None) -> dict:
        from .pm_clarification import wait_for_answer
        return wait_for_answer(self, question, options, timeout, continuation)

    def cancel(self):
        self._signal_stop("cancelled")
        self.report_status("cancelled")
        self.report_progress(0.0, "cancelled", "任务已被 Boss 取消")
        logger.info("[%s] 任务已取消", self.pm_id[:8])

    def pause(self):
        self._signal_stop("paused")
        self.report_status("paused")
        self.report_progress(0.0, "paused", "任务已被 Boss 暂停")
        logger.info("[%s] 任务已暂停", self.pm_id[:8])

    def report_status(self, status: str, team_structure: dict = None,
                      task_list: list = None, collaboration_mode: str = None, clarification: dict = None):
        if self._state.stop_event.is_set() and status != self._state.stop_reason:
            return
        if status in ("completed", "failed") and not self.checkpoint(status):
            return
        try:
            payload = {"status": status, "generation": self._state.generation}
            if clarification is not None:
                payload.update(clarification_question=clarification["question"],
                               clarification_options=clarification["options"],
                               clarification_id=clarification["question_id"])
            if team_structure is not None:
                payload["team_structure"] = team_structure
            if task_list is not None:
                payload["task_list"] = task_list
            if collaboration_mode is not None:
                payload["collaboration_mode"] = collaboration_mode
            http_post(
                f"{self.secretary_url}/api/pm/{self.pm_id}/status",
                json=payload, timeout=5, retries=2,
            )
        except Exception as e:
            logger.warning("[%s] 上报状态失败: %s", self.pm_id[:8], e)

    def report_team(self, team: dict):
        if self._state.stop_event.is_set():
            return
        try:
            http_post(
                f"{self.secretary_url}/api/pm/{self.pm_id}/teams",
                json=team, timeout=5, retries=2,
            )
        except Exception as e:
            logger.warning("[%s] 上报团队信息失败: %s", self.pm_id[:8], e)

    def report_progress(self, progress: float, status: str, message: str,
                        reporter_id: str = "", reporter_type: str = "pm",
                        task_name: str = ""):
        if self._state.stop_event.is_set() and status != self._state.stop_reason:
            return
        try:
            http_post(
                f"{self.secretary_url}/api/pm/{self.pm_id}/progress",
                json={
                    "pm_id": self.pm_id,
                    "generation": self._state.generation,
                    "reporter_id": reporter_id or self.pm_id,
                    "reporter_type": reporter_type,
                    "task_name": task_name,
                    "progress": progress,
                    "status": status,
                    "message": message,
                    "timestamp": time.time(),
                },
                timeout=5, retries=2,
            )
        except Exception as e:
            logger.warning("[%s] 上报进度失败: %s", self.pm_id[:8], e)

    def sync_subtasks(self):
        if self._state.stop_event.is_set() and self._state.stop_reason != "failed":
            return
        try:
            subtasks = self._build_subtask_status()
            if not subtasks:
                return
            http_post(
                f"{self.secretary_url}/api/pm/{self.pm_id}/subtasks",
                json={
                    "task_id": (self._state.task or {}).get("task_id", ""),
                    "subtasks": subtasks,
                    "generation": self._state.generation,
                },
                timeout=5, retries=2,
            )
        except Exception as e:
            logger.warning("[%s] 同步子任务失败: %s", self.pm_id[:8], e)

    def _build_subtask_status(self) -> list:
        st = self._state
        decomposition = (st.plan or {}).get("decomposition", [])
        result = []
        with st.lock:
            for sub in decomposition:
                name = sub.get("name", "")
                if name in st.terminal_failures:
                    status = "failed"
                elif name in st.blocked_subtasks:
                    status = "blocked"
                elif name in st.subtask_outputs:
                    status = "completed"
                else:
                    status = "pending"
                    for m in st.subagents.values():
                        if m.get("current_task") == name:
                            raw = m.get("status", "pending")
                            if raw in ("busy", "executing", "working"):
                                status = "running"
                            elif raw in ("completed", "failed"):
                                status = raw
                            else:
                                status = "assigned"
                            break
                result.append({
                    "name": name,
                    "description": sub.get("description", ""),
                    "required_skill": sub.get("skill", ""),
                    "depends_on": sub.get("depends_on", []),
                    "status": status,
                    "error": st.terminal_failures.get(name, st.blocked_subtasks.get(name, "")),
                })
        return result

    def deliver_result(self, task_name: str, task_desc: str,
                       aggregated: str, subtask_results: list) -> bool:
        """Return true only after Secretary acknowledges the matching task delivery."""
        if self._state.stop_event.is_set():
            return False
        summary = aggregated[:500] if len(aggregated) > 500 else aggregated
        completed_count = sum(1 for r in subtask_results if r.get("status") == "completed")
        total_count = len(subtask_results)

        delivery = {
            "pm_id": self.pm_id,
            "task_id": self._state.task.get("task_id", ""),
            "task_name": task_name,
            "task_description": task_desc,
            "deliverable": aggregated,
            "summary": summary,
            "subtask_stats": {
                "total": total_count,
                "completed": completed_count,
                "failed": total_count - completed_count,
            },
            "delivered_at": time.time(),
        }

        if not self.execution_allowed():
            return False
        if self._state.delivery.get("payload"):
            delivery = self._state.delivery["payload"]
        with self._state.lock:
            self._state.delivery = {"state": "prepared", "payload": delivery}
            if not self.checkpoint("delivering"):
                return False

        # Attempt to save the local artifact before the delivery request. An HTTP
        # timeout is an unknown outcome; do not record it as successful delivery.
        self._distribute_artifacts(task_name, aggregated)
        if self._state.stop_event.is_set():
            return False
        with self._state.lock:
            self._state.delivery["state"] = "sending"
            if not self.checkpoint():
                return False
        try:
            resp = http_post(
                f"{self.secretary_url}/api/pm/{self.pm_id}/deliver",
                json={**delivery, "generation": self._state.generation},
                timeout=10,
            )
            ack = resp.json() if resp.status_code == 200 else {}
            if ack.get("ok") is True and ack.get("task_id") == delivery["task_id"]:
                with self._state.lock:
                    self._state.delivery["state"] = "acknowledged"
                    if not self.checkpoint():
                        return False
                logger.info("[%s] 交付物已上报 Secretary", self.pm_id[:8])
            else:
                logger.error("[%s] 交付物上报失败: HTTP %d", self.pm_id[:8], resp.status_code)
                return False
        except Exception as e:
            logger.error("[%s] 交付物上报异常: %s", self.pm_id[:8], e)
            return False

        if self._state.stop_event.is_set():
            return False
        self._record_task_memory(task_name, task_desc, subtask_results)
        return True

    def _distribute_artifacts(self, task_name: str, content: str):
        """F3.2: 产物写入共享目录。"""
        try:
            from .shared_folder import SharedFolder
            from .config import load_config
            import os
            cfg = load_config()
            sf = SharedFolder(cfg)

            deliverables_dir = os.path.join(sf.path, "deliverables")
            os.makedirs(deliverables_dir, exist_ok=True)

            task_id = self._state.task.get("task_id", "unknown")[:8]
            safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in task_name)[:40]
            filename = f"{task_id}_{safe_name}.md"
            filepath = os.path.join(deliverables_dir, filename)

            header = f"# {task_name}\n\n> PM: {self.pm_id[:8]} | 时间: {time.strftime('%Y-%m-%d %H:%M')}\n\n"
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(header + content)

            logger.info("[%s] F3.2 产物已分发: %s", self.pm_id[:8], filename)
        except Exception as e:
            logger.debug("[%s] 产物分发失败 (non-critical): %s", self.pm_id[:8], e)

    def _record_task_memory(self, task_name: str, task_desc: str, subtask_results: list):
        """优化14: 记录任务模式到 Secretary 任务记忆表。"""
        st = self._state
        keywords = [w for w in f"{task_name} {task_desc[:100]}".replace(":", " ").split() if len(w) >= 2][:10]
        task_type = PMPlanner.infer_task_type(task_name, task_desc)
        duration = time.time() - getattr(st, "start_time", time.time())
        completed_count = sum(1 for r in subtask_results if r.get("status") == "completed")
        success = completed_count >= len(subtask_results) * 0.5 if subtask_results else True
        error_pattern = next((r["error"][:100] for r in subtask_results
                             if r.get("status") != "completed" and r.get("error")), "")
        memory_data = {
            "pm_id": self.pm_id, "task_name": task_name, "task_keywords": keywords,
            "task_type": task_type,
            # 修复: plan 中的协作模式字段是 pattern (非 collaboration_mode)
            "collaboration_mode": (st.plan or {}).get("pattern", ""),
            "team_size": len(st.subagents) + len(st.teams),
            "duration_secs": duration, "success": success,
            "error_pattern": error_pattern, "device_id": self.device_id,
        }
        try:
            resp = http_post(f"{self.secretary_url}/api/pm/{self.pm_id}/task-memory",
                                 json=memory_data, timeout=10)
            if resp.status_code == 200:
                logger.info("[%s] 任务记忆已记录 (type=%s, success=%s)", self.pm_id[:8], task_type, success)
            else:
                logger.error("[%s] 任务记忆记录失败: HTTP %d", self.pm_id[:8], resp.status_code)
        except Exception as e:
            logger.error("[%s] 任务记忆记录异常: %s", self.pm_id[:8], e)

    @staticmethod
    def _infer_task_type(task_name: str, task_desc: str) -> str:
        """向后兼容, 委托给 PMPlanner。"""
        return PMPlanner.infer_task_type(task_name, task_desc)
