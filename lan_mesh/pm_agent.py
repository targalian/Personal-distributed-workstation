"""PM Agent 协调器/门面 — 持有 Planner/Dispatcher/Monitor, 对外暴露统一接口。

子模块: pm_planner / pm_dispatcher / pm_monitor / pm_state
"""
import threading
import time
from typing import Optional

import requests as _requests

from .http_retry import http_post, http_get, auth_headers
from .logger import get_logger
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
        self._task_id = ""  # P3: 当前任务 ID, 供任务流追踪归因

        # ── 共享状态 ──
        self._state = PMState()

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

    def start_task(self, task: dict):
        """接管任务，开始规划与执行 (异步)。"""
        self._running = True

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
        self._running = False

    def get_status(self) -> dict:
        st = self._state
        return {
            "pm_id": self.pm_id,
            "device_id": self.device_id,
            "running": self._running,
            "teams": len(st.teams),
            "subagents": len(st.subagents),
        }

    # ── iter-53: 执行态快照持久化与断点恢复 ─────────────────────

    def _persist_snapshot(self, phase: str):
        """iter-53: 序列化当前状态并上报 Secretary 落库 (异常静默)。

        快照通过 Secretary HTTP API 持久化, PM 无论在哪个节点运行都有效;
        落库失败仅降级为「少一个恢复点」, 不影响任务执行。
        """
        try:
            snapshot = self._state.to_snapshot()
            http_post(
                f"{self.secretary_url}/api/pm/{self.pm_id}/snapshot",
                json={
                    "pm_id": self.pm_id,
                    "task_id": (self._state.task or {}).get("task_id", self._task_id),
                    "phase": phase,
                    "state": snapshot,
                },
                timeout=5, retries=1,
            )
        except Exception as e:
            logger.debug("[%s] 快照持久化失败 (non-critical): %s", self.pm_id[:8], e)
            try:
                from .error_tracker import error_tracker
                error_tracker.capture("pm", e, context={
                    "point": "snapshot", "pm_id": self.pm_id[:8], "phase": phase})
            except Exception:
                pass

    def _clear_snapshot(self):
        """iter-53: 任务终结 (完成/失败/取消) 时清除执行态快照。"""
        try:
            _requests.delete(
                f"{self.secretary_url}/api/pm/{self.pm_id}/snapshot",
                headers=auth_headers(), timeout=5)
        except Exception:
            pass

    def resume_from_snapshot(self, snapshot: dict) -> bool:
        """iter-53: 从快照恢复执行 — 保留已完成子任务输出, 重跑未完成部分。

        Args:
            snapshot: db.get_pm_snapshot* 返回的行 (含 state_json/phase 字段)。

        Returns:
            是否成功启动恢复线程。
        """
        import json as _json
        try:
            raw = snapshot.get("state_json", "")
            state_data = _json.loads(raw) if isinstance(raw, str) else dict(raw or {})
            if isinstance(state_data, str):
                state_data = _json.loads(state_data)
        except Exception as e:
            logger.error("[%s] 快照解析失败, 无法恢复: %s", self.pm_id[:8], e)
            return False

        # 就地恢复: planner/dispatcher/monitor 共享同一 state 引用, 只重写字段
        self._state.restore_from(state_data)
        self._resume_phase = snapshot.get("phase", "monitoring")
        self._running = True
        self._task_id = (self._state.task or {}).get("task_id", "")

        self._thread = threading.Thread(
            target=self._run_resumed, daemon=True, name=f"pm-resume-{self.pm_id[:8]}")
        self._thread.start()
        self._progress_thread = threading.Thread(
            target=self._monitor.progress_loop, daemon=True,
            name=f"pm-progress-{self.pm_id[:8]}")
        self._progress_thread.start()
        return True

    def _run_resumed(self):
        """iter-53: 断点续跑 — 按快照阶段恢复执行流。"""
        st = self._state
        try:
            self.report_status("resumed")
            self.report_progress(0.0, "resumed", "任务从断点恢复, 重新分发未完成子任务")
            self.sync_subtasks()

            # 场景1: 上次在等待 Boss 澄清 → 重新发起, 回复后继续走重分发
            if st.clarification_question:
                question = st.clarification_question
                st.clarification_question = ""
                resp = self.request_clarification(question)
                if resp.get("cancelled") or resp.get("paused"):
                    return

            # 场景2: 无任务分解 (快照残缺) → 标记失败
            decomposition = (st.plan or {}).get("decomposition", [])
            if not decomposition:
                self.report_status("failed")
                self.report_progress(0.0, "failed", "断点恢复失败: 快照无任务分解")
                self._running = False
                self._clear_snapshot()
                return

            # 场景3: 全部子任务已有输出 → 直接聚合交付
            todo = [s for s in decomposition if s.get("name", "") not in st.subtask_outputs]
            if not todo:
                logger.info("[%s] 断点恢复: 全部子任务已完成, 直接聚合", self.pm_id[:8])
                self._monitor.aggregate_results()
                return

            # 场景4: 重新分发未完成子任务 (已完成输出保留不重跑)
            logger.info("[%s] 断点恢复: %d/%d 子任务未完成, 重新分发",
                        self.pm_id[:8], len(todo), len(decomposition))
            for sub in todo:
                sub_name = sub.get("name", "")
                deps = sub.get("depends_on", [])
                if not all(d in st.subtask_outputs for d in deps):
                    # 依赖仍未满足 → 挂回 pending, 由依赖完成时触发分发
                    station = st.task_station.get(sub_name)
                    agent_info = st.task_agent.get(sub_name)
                    if station and agent_info:
                        with st.lock:
                            st.pending_subtasks[sub_name] = {
                                "sub": sub, "station": station, "agent_info": agent_info}
                    continue
                station = st.task_station.get(sub_name)
                agent_info = st.task_agent.get(sub_name)
                if station and agent_info:
                    self._dispatcher._record_subtask_start(sub_name)
                    try:
                        self._dispatcher.dispatch_subtask(
                            station, agent_info, dict(st.task), sub, plan=st.plan)
                    except Exception as e:
                        logger.warning("[%s] 恢复分发 '%s' 异常, 回退本地: %s",
                                       self.pm_id[:8], sub_name, e)
                        self._dispatcher.execute_subtask_locally(dict(st.task), sub)
                else:
                    self._dispatcher.execute_subtask_locally(dict(st.task), sub)

            self._dispatcher.try_dispatch_pending()
            # 保持 running, 由 progress_loop 在全部完成后触发聚合
            self._persist_snapshot("monitoring")
        except Exception as e:
            logger.error("[%s] 断点恢复执行失败: %s", self.pm_id[:8], e)
            try:
                from .error_tracker import error_tracker
                error_tracker.capture("pm", e, context={
                    "point": "resume", "pm_id": self.pm_id[:8]})
            except Exception:
                pass
            self.report_status("failed")
            self.report_progress(0.0, "failed", f"断点恢复失败: {e}")
            self._running = False
            self._clear_snapshot()

    def _run_task(self, task: dict):
        st = self._state
        st.start_time = time.time()
        self._task_id = (task or {}).get("task_id", "")
        # iter-53: multi 模式下 _run_task 结束后由 progress_loop 聚合收尾
        # (修复: 原先 finally 无条件停 running, 多子任务模式聚合永不触发)
        _multi_monitoring = False
        try:
            if self._monitor.is_global_timed_out():
                raise TimeoutError(f"全局任务超时 ({st.global_timeout}s)")

            # F2.3: 多轮任务细化
            task = self._planner.refine_requirements(task)

            # 阶段 1: 规划
            self.report_status("planning")
            plan = self._planner.analyze_with_skill(task)
            logger.info("[%s] 规划完成: 模式=%s, 子任务=%d",
                       self.pm_id[:8], plan.get('pattern', 'single'),
                       len(plan.get('decomposition', [])))

            st.plan = plan
            st.task = task
            # iter-53: 规划完成快照 (断点1)
            self._persist_snapshot("planning_done")

            # 阶段 2: 执行
            pattern = plan.get("pattern", "single")
            if pattern == "single":
                self.report_status("executing", collaboration_mode="single")
                if not plan.get("decomposition"):
                    plan["decomposition"] = [{
                        "name": task.get("name", "执行"),
                        "skill": "code_generation",
                        "depends_on": [],
                        "description": task.get("description", ""),
                    }]
                self.sync_subtasks()
                result = self._planner.execute_directly(task)

                # 校验: 如果执行结果是错误信息, 不能当作完成交付
                summary = result.get("summary", "")
                if result.get("status") == "failed" or summary.startswith(("[未配置", "[LLM 调用失败", "[模型调用失败")):
                    error_msg = summary or "执行失败"
                    logger.error("[%s] 任务执行失败 (LLM 错误): %s", self.pm_id[:8], error_msg)
                    self.report_status("failed")
                    self.report_progress(0.0, "failed", error_msg)
                    return

                for sub in plan.get("decomposition", []):
                    st.subtask_outputs[sub.get("name", "")] = result
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
                self.deliver_result(task_name, task_desc, deliverable, subtask_results)

                self.report_status("completed", task_list=[{"name": task_name, "status": "completed"}])
                self.report_progress(1.0, "completed", f"任务完成: {result.get('summary', '')[:100]}")
            else:
                self.report_status("executing", collaboration_mode=pattern,
                                   task_list=plan.get("decomposition", []))
                self.sync_subtasks()
                self._dispatcher.create_team_and_dispatch(task, plan)
                self.report_status("monitoring")
                # iter-53: 分发完成进入监测, 快照 (断点2)
                _multi_monitoring = True
                self._persist_snapshot("monitoring")

        except Exception as e:
            logger.error("[%s] 任务执行失败: %s", self.pm_id[:8], e)
            # iter-45: 错误追踪埋点 — 任务级失败 (最高价值信号)
            try:
                from .error_tracker import error_tracker
                error_tracker.capture("pm", e, context={
                    "task_id": (task or {}).get("task_id", ""),
                    "pm_id": self.pm_id[:8]})
            except Exception:
                pass
            self.report_status("failed")
            self.report_progress(0.0, "failed", str(e))

        finally:
            # iter-53: multi 正常路径保持 running 等待聚合收尾; 其余路径停止并清快照
            if not _multi_monitoring:
                self._running = False
                self._clear_snapshot()

    def receive_progress_report(self, report: dict):
        self._monitor.receive_progress_report(report)

    def receive_subtask_result(self, task_name: str, status: str,
                               output_data: dict, agent_id: str = ""):
        """接收本机子 Agent 执行结果，转化为进度报告注入 Monitor。

        Station 内嵌模式下，子任务执行完成后由 station_controller 调用此方法，
        让 PM Monitor 感知子任务完成/失败，触发依赖链分发或失败接管。
        """
        report = {
            "reporter_id": agent_id,
            "task_name": task_name,
            "status": status,
            "progress": 1.0 if status == "completed" else 0.0,
            "output": output_data,
            "message": f"子任务 {task_name} {status}",
        }
        # P3: 任务流追踪 — 子任务结果阶段点 (异常静默)
        try:
            from . import runtime_trace
            runtime_trace.trace_task_event(
                self._task_id, "subtask_result",
                detail=f"{task_name} → {status}", pm_id=self.pm_id)
        except Exception:
            pass
        self._monitor.receive_progress_report(report)
        # iter-53: 子任务结果注入后快照 (断点3, 高频但轻量)
        self._persist_snapshot("executing")

    def receive_input(self, input_data: dict):
        st = self._state
        st.clarification_response = input_data
        st.clarification_event.set()
        logger.info("[%s] 收到 Boss 回复: %s", self.pm_id[:8], str(input_data)[:200])

    def request_clarification(self, question: str, options: list = None,
                              timeout: float = None) -> dict:
        """向 Boss 发起澄清请求, 阻塞等待回复。"""
        st = self._state
        st.clarification_event.clear()
        st.clarification_response = {}
        st.clarification_question = question
        self.report_status("awaiting_input",
                           task_list=[{"name": "请求决策", "status": "awaiting_input", "description": question}])
        self.report_progress(-1.0, "awaiting_input", question,
                             reporter_type="pm_clarification", task_name=question[:100])
        # iter-53: 澄清等待快照 (断点4, 重启后重新发起澄清)
        self._persist_snapshot("awaiting_input")
        if options:
            try:
                http_post(f"{self.secretary_url}/api/pm/{self.pm_id}/status",
                              json={"status": "awaiting_input", "clarification_question": question,
                                    "clarification_options": options}, timeout=5)
            except Exception:
                pass
        actual_timeout = timeout if timeout is not None else st.clarification_timeout
        logger.info("[%s] 等待 Boss 决策 (超时=%ds): %s", self.pm_id[:8], actual_timeout, question[:120])
        if not st.clarification_event.wait(timeout=actual_timeout):
            logger.warning("[%s] 等待 Boss 决策超时, 使用默认策略继续", self.pm_id[:8])
            return {"response": "", "choice": "", "timed_out": True}
        self.report_status("executing")
        return st.clarification_response

    def cancel(self):
        st = self._state
        self._running = False
        st.clarification_event.set()
        st.clarification_response = {"response": "", "choice": "", "cancelled": True}
        self.report_status("cancelled")
        self.report_progress(0.0, "cancelled", "任务已被 Boss 取消")
        # iter-53: 取消即终结, 清理快照
        self._clear_snapshot()
        logger.info("[%s] 任务已取消", self.pm_id[:8])

    def pause(self):
        st = self._state
        self._running = False
        st.clarification_event.set()
        st.clarification_response = {"response": "", "choice": "", "paused": True}
        self.report_status("paused")
        self.report_progress(0.0, "paused", "任务已被 Boss 暂停")
        # iter-53: 暂停保留快照 (resume 可继续)
        self._persist_snapshot("paused")
        logger.info("[%s] 任务已暂停", self.pm_id[:8])

    def report_status(self, status: str, team_structure: dict = None,
                      task_list: list = None, collaboration_mode: str = None):
        # P3: 任务流追踪 — 所有生命周期状态必经此出口 (异常静默)
        try:
            from . import runtime_trace
            detail_parts = []
            if collaboration_mode:
                detail_parts.append(f"模式={collaboration_mode}")
            if task_list:
                detail_parts.append(f"子任务={len(task_list)}")
            runtime_trace.trace_task_event(
                self._task_id, f"pm:{status}",
                detail=", ".join(detail_parts), pm_id=self.pm_id)
        except Exception:
            pass
        try:
            payload = {"status": status}
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
        try:
            http_post(
                f"{self.secretary_url}/api/pm/{self.pm_id}/progress",
                json={
                    "pm_id": self.pm_id,
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
        try:
            subtasks = self._build_subtask_status()
            if not subtasks:
                return
            http_post(
                f"{self.secretary_url}/api/pm/{self.pm_id}/subtasks",
                json={
                    "task_id": (self._state.task or {}).get("task_id", ""),
                    "subtasks": subtasks,
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
                if name in st.subtask_outputs:
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
                })
        return result

    def deliver_result(self, task_name: str, task_desc: str,
                       aggregated: str, subtask_results: list):
        """优化9: 交付闭环 — 上报 Secretary + 产物分发 + 任务记忆。"""
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

        # P3: 任务流追踪 — 交付阶段点 (异常静默)
        try:
            from . import runtime_trace
            runtime_trace.trace_task_event(
                self._task_id, "delivered",
                detail=f"{completed_count}/{total_count} 子任务完成", pm_id=self.pm_id)
        except Exception:
            pass

        try:
            resp = http_post(
                f"{self.secretary_url}/api/pm/{self.pm_id}/deliver",
                json=delivery,
                timeout=10,
            )
            if resp.status_code == 200:
                logger.info("[%s] 交付物已上报 Secretary", self.pm_id[:8])
            else:
                logger.error("[%s] 交付物上报失败: HTTP %d", self.pm_id[:8], resp.status_code)
        except Exception as e:
            logger.error("[%s] 交付物上报异常: %s", self.pm_id[:8], e)
            # iter-45: 错误追踪埋点 — 交付链异常 (交付丢失风险)
            try:
                from .error_tracker import error_tracker
                error_tracker.capture("pm", e, context={
                    "point": "deliver", "pm_id": self.pm_id[:8]})
            except Exception:
                pass

        self._distribute_artifacts(task_name, aggregated)
        self._record_task_memory(task_name, task_desc, subtask_results)

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
            # iter-45: 错误追踪埋点 — 记忆沉淀链异常 (经验丢失风险)
            try:
                from .error_tracker import error_tracker
                error_tracker.capture("pm", e, context={
                    "point": "task_memory", "pm_id": self.pm_id[:8]})
            except Exception:
                pass

    @staticmethod
    def _infer_task_type(task_name: str, task_desc: str) -> str:
        """向后兼容, 委托给 PMPlanner。"""
        return PMPlanner.infer_task_type(task_name, task_desc)
