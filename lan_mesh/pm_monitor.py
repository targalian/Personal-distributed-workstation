"""
PM 进度监控器 — 进度收集、超时检测、失败接管、质量验证

职责:
1. progress_loop 轮询收集进度
2. 接收子 Agent 进度上报
3. 超时检测 (全局 + 子任务)
4. 失败接管 (三级策略: 同站重试 → 换站重试 → PM 本地接管)
5. 质量验证 (F2.5 生成-验证器模式)
6. 结果聚合 (优化4)
7. 最终失败与后续任务阻塞
"""
import time
import uuid
from typing import Optional

from .agent_prompt import build_aggregation_prompt
from .logger import get_logger
from .pm_state import PMState

logger = get_logger("pm.monitor")


class PMMonitor:
    """进度监控器: 轮询进度 → 超时检测 → 失败接管 → 结果聚合。"""

    def __init__(self, pm_id: str, runtime, secretary_url: str,
                 state: PMState, agent, dispatcher):
        """
        Args:
            pm_id: PM Agent ID
            runtime: Worker 的 AgentRuntime 实例
            secretary_url: Secretary API 地址
            state: 共享状态
            agent: ProjectManagerAgent 协调器引用
            dispatcher: PMDispatcher 实例 (用于重试分发)
        """
        self._pm_id = pm_id
        self._runtime = runtime
        self._secretary_url = secretary_url
        self._state = state
        self._agent = agent
        self._dispatcher = dispatcher
        self._local_takeover_tasks = state.local_takeover_tasks
        self.poll_interval = 10.0

    # ── 进度轮询 ──────────────────────────────────────────────────

    def progress_loop(self):
        """定期收集进度并上报 Secretary (优化4: 全部完成后触发聚合)。"""
        _aggregated = False
        st = self._state

        while self._agent.running:
            if st.stop_event.wait(self.poll_interval) or not self._agent.running:
                break

            # F1.3: 全局超时检测
            if self.is_global_timed_out():
                logger.error("[%s] 全局任务超时 (%.0fs), 停止调度",
                            self._pm_id[:8], st.global_timeout)
                self._fail_task("全局超时", f"全局超时 ({st.global_timeout}s)，在途操作结果待核实")
                break

            if not st.monitoring_ready.is_set():
                continue

            self._dispatcher.poll_remote_results()
            if st.stop_event.is_set():
                break

            # F1.3: 子任务超时检测
            self.check_subtask_timeouts()
            if st.stop_event.is_set():
                break

            with st.lock:
                names = {sub.get("name", "") for sub in st.plan.get("decomposition", [])}
                if not names:
                    continue
                total = len(names)
                completed = sum(name in st.subtask_outputs for name in names)
                has_pending = bool(st.pending_subtasks)

            overall = completed / total if total > 0 else 0.0
            self._agent.report_progress(overall, "in_progress",
                                        f"整体进度: {completed}/{total} 子任务完成")

            # Only verified success can satisfy dependencies and enter aggregation.
            if not _aggregated and not has_pending:
                if completed == total and total > 0:
                    _aggregated = True
                    self.aggregate_results()

    # ── 接收进度上报 ──────────────────────────────────────────────

    def receive_progress_report(self, report: dict):
        """接收子 Agent 主动上报的进度 (通过 Worker API 转发)。

        优化1: 当子任务完成时, 自动检查是否有等待依赖的后续任务可以分发。
        优化5: 当子任务失败时, 触发接管策略。
        """
        st = self._state
        if st.stop_event.is_set():
            return
        reporter_id = report.get("reporter_id", "")
        task_name = report.get("task_name", "")
        status = report.get("status", "in_progress")

        with st.lock:
            if st.stop_event.is_set():
                return
            if task_name in st.subtask_outputs:
                return
            attempt = st.execution_attempts.get(task_name)
            if not attempt or attempt["phase"] != "active" or any(report.get(key) != attempt[key] for key in (
                    "subtask_id", "parent_task_id", "pm_id", "reporter_id")):
                return
            if status in ("completed", "failed", "unknown"):
                # Reserve this terminal result before quality checks or retry.
                attempt["phase"] = "processing"
                attempt["received_result"] = dict(report)
                if not st.persist():
                    return
            # 支持 member_id 或 agent_id 查找 (Station 内嵌模式传 agent_id)
            target = st.subagents.get(reporter_id)
            if not target:
                for m in st.subagents.values():
                    if m.get("agent_id") == reporter_id:
                        target = m
                        break
            if target:
                target["progress"] = report.get("progress", 0.0)
                target["status"] = status
                target["current_task"] = task_name

        # 优化1: 任务完成时存储输出并尝试分发依赖链
        if status == "completed" and task_name:
            output = report.get("output", report.get("message", ""))
            # 优化6: 验证自检结果
            self_check = report.get("self_check", {})
            if not self_check:
                logger.warning("[%s] 子任务 '%s' 完成但未附带自检结果", self._pm_id[:8], task_name)
            elif not self_check.get("passed", False):
                notes = self_check.get("notes", "")
                logger.warning("[%s] 子任务 '%s' 自检未通过: %s", self._pm_id[:8], task_name, notes[:200])
            else:
                logger.info("[%s] 子任务 '%s' 完成, 自检通过: %s",
                           self._pm_id[:8], task_name, self_check.get('notes', '')[:100])
                logger.info("[%s] 子任务 '%s' 完成, 输出已存储", self._pm_id[:8], task_name)

            # F2.5: 质量验证
            quality = self._verify_output_quality(task_name, output)
            if st.stop_event.is_set():
                return
            if quality and not quality.get("accepted", True):
                logger.warning("[%s] 子任务 '%s' 质量未达标 (score=%.1f): %s",
                              self._pm_id[:8], task_name, quality.get("score", 0),
                              quality.get("issues", "")[:150])
                self.handle_subagent_failure(task_name, f"质量验证未通过: {quality.get('issues', '')}")
                return

            with st.lock:
                self._local_takeover_tasks.discard(task_name)
                if st.stop_event.is_set():
                    return
                st.subtask_outputs[task_name] = output
                if attempt:
                    attempt["phase"] = "done"
                st.subtask_start_times.pop(task_name, None)
                if not st.persist():
                    return
            self._dispatcher.try_dispatch_pending()

        if status == "failed" and task_name:
            self.handle_subagent_failure(task_name, report.get("message", "未知错误"))
        elif status == "unknown" and task_name:
            self._fail_task(task_name, report.get("message") or "执行结果待核实，不自动重试")

        # 同步子任务状态到 Secretary
        self._agent.sync_subtasks()

        # 转发到 Secretary
        self._agent.report_progress(
            report.get("progress", 0.0),
            status,
            report.get("message", ""),
            reporter_id=reporter_id,
            reporter_type="member",
            task_name=task_name,
        )

    # ── F1.3: 超时保护 ─────────────────────────────────────────

    def is_global_timed_out(self) -> bool:
        """F1.3: 检查全局任务是否超时。"""
        return (time.time() - self._state.start_time) > self._state.global_timeout

    def check_subtask_timeouts(self):
        """Stop on unknown timeout outcomes instead of duplicating execution."""
        st = self._state
        now = time.time()
        timed_out = []
        with st.lock:
            for task_name, start_t in list(st.subtask_start_times.items()):
                if (now - start_t) > st.subtask_timeout:
                    timed_out.append(task_name)

            for task_name in timed_out:
                del st.subtask_start_times[task_name]
                for agent_id, info in st.subagents.items():
                    if info.get("current_task") == task_name and info.get("status") not in ("completed", "failed"):
                        info["status"] = "failed"
                        break

        for task_name in timed_out:
            # A timeout does not prove the old attempt stopped. Re-executing it
            # could repeat tool side effects on another host.
            self._fail_task(task_name, f"子任务超时 ({st.subtask_timeout}s)，执行结果待核实，不自动重试")
            break

    def _fail_task(self, task_name: str, error_msg: str):
        """Stop the plan and persist why incomplete tasks cannot continue."""
        st = self._state
        with st.lock:
            if st.stop_event.is_set():
                return
            st.terminal_failures[task_name] = error_msg
            st.subtask_outputs.pop(task_name, None)
            for sub in st.plan.get("decomposition", []):
                name = sub.get("name", "")
                if name != task_name and name not in st.subtask_outputs:
                    st.blocked_subtasks[name] = f"任务因 {task_name} 失败而停止：{error_msg}"
            st.pending_subtasks.clear()
            st.subtask_start_times.clear()
            st.stop_reason = "failed"
            st.stop_event.set()
            st.clarification_event.set()
            self._agent.running = False
            st.persist("failed")
        self._agent.sync_subtasks()
        self._agent.report_status("failed")
        self._agent.report_progress(0.0, "failed", f"子任务 {task_name} 失败：{error_msg}", task_name=task_name)

    def _retry_task(self, sub: dict, error_msg: str) -> dict:
        st = self._state
        name = sub["name"]
        with st.lock:
            st.retry_counts[name] = st.retry_counts.get(name, 0) + 1
            st.subtask_outputs.pop(name, None)
            st.subtask_start_times[name] = time.time()
            for member in st.subagents.values():
                if member.get("current_task") == name:
                    member["status"] = "retrying"
            inputs = {**st.task.get("input_data", {}),
                      "_retry_context": {"attempt": st.retry_counts[name], "previous_error": error_msg[:500]},
                      "_dependency_outputs": {dep: st.subtask_outputs[dep] for dep in sub.get("depends_on", [])
                                              if dep in st.subtask_outputs}}
        return {**st.task, "input_data": inputs}

    def _retry_elsewhere(self, task: dict, sub: dict, original_station: dict) -> bool:
        """Attempt one other station; creation failures fall through to local takeover."""
        st = self._state
        name = sub["name"]
        try:
            stations = self._dispatcher.get_available_stations()
            other = next((station for station in stations
                          if station.get("device_id") != original_station.get("device_id")), None)
            if not other or st.stop_event.is_set():
                return False
            agent_id = f"sub-{uuid.uuid4().hex[:10]}"
            prompt = self._dispatcher._build_subagent_prompt_for_sub(task, sub, st.plan, agent_id, name)
            info = self._dispatcher._create_subagent_on_station(
                other, name, [sub.get("skill", "code_generation")], sub.get("description", ""),
                system_prompt=prompt, preferred_agent_id=agent_id)
            if not info or st.stop_event.is_set():
                return False
            with st.lock:
                st.task_station[name] = other
                st.task_agent[name] = info
                for member in st.subagents.values():
                    if member.get("current_task") == name:
                        member.update(agent_id=info.get("agent_id", agent_id), status="retrying")
            self._dispatcher.dispatch_subtask(other, info, task, sub, plan=st.plan)
            return True
        except Exception as exc:
            logger.warning("[%s] 换站重试准备失败: %s", self._pm_id[:8], exc)
            return False

    def handle_subagent_failure(self, task_name: str, error_msg: str):
        """Bounded recovery: same executor, one other station, then one local takeover."""
        st = self._state
        with st.lock:
            if st.stop_event.is_set():
                return
            attempt = st.execution_attempts.get(task_name)
            if attempt:
                attempt["phase"] = "done"
            sub = next((sub for sub in st.plan.get("decomposition", []) if sub.get("name") == task_name), None)
            if task_name in self._local_takeover_tasks:
                self._local_takeover_tasks.discard(task_name)
                action, task = "fail", None
            elif sub is None:
                return
            else:
                station = st.task_station.get(task_name, {})
                info = st.task_agent.get(task_name, {})
                if st.retry_counts.get(task_name, 0) < st.max_retries:
                    action = "retry"
                elif not station:
                    action = "fail"
                elif task_name not in st.cross_station_attempted:
                    st.cross_station_attempted.add(task_name)
                    action = "cross"
                else:
                    self._local_takeover_tasks.add(task_name)
                    action = "local"
                task = self._retry_task(sub, error_msg) if action != "fail" else None
            if not st.persist():
                return
        if action == "fail":
            self._fail_task(task_name, error_msg)
            return
        try:
            if action == "retry":
                if station and info.get("agent_id"):
                    self._dispatcher.dispatch_subtask(station, info, task, sub, plan=st.plan)
                else:
                    self._dispatcher.execute_subtask_locally(task, sub)
                return
            if action == "cross":
                if self._retry_elsewhere(task, sub, station):
                    return
                with st.lock:
                    if st.stop_event.is_set() or task_name in self._local_takeover_tasks:
                        return
                    self._local_takeover_tasks.add(task_name)
                    task = self._retry_task(sub, error_msg)
            self._dispatcher.execute_subtask_locally(task, sub)
        except Exception as exc:
            with st.lock:
                self._local_takeover_tasks.discard(task_name)
            self._fail_task(task_name, f"重试执行异常：{exc}")

    # ── F2.5: 质量验证器 ───────────────────────────────────────

    def _verify_output_quality(self, task_name: str, output) -> Optional[dict]:
        """F2.5: 生成-验证器模式 — 用 LLM 评估子任务输出质量。"""
        if not output or len(str(output)) < 50:
            return None

        sub_desc = ""
        for d in self._state.plan.get("decomposition", []):
            if d.get("name") == task_name:
                sub_desc = d.get("description", "")
                break

        verify_prompt = f"""你是质量验证器。请评估以下子任务输出的质量。

## 子任务要求
{sub_desc or task_name}

## 实际输出 (前2000字)
{str(output)[:2000]}

## 评估标准
1. 完整性: 是否覆盖了任务要求的所有方面
2. 正确性: 内容是否逻辑正确、无明显错误
3. 可用性: 输出是否可直接使用 (代码可运行/文档可阅读)

请严格输出 JSON (不要 markdown):
{{"accepted": true/false, "score": 0-10, "issues": "问题描述(无问题则为空)"}}

注意: score >= 6 则 accepted=true。只有明显缺陷才判定为不通过。"""

        try:
            if not self._agent.execution_allowed():
                return {"accepted": False, "score": 0, "issues": "恢复授权失效"}
            resp = self._runtime._call_llm_with_routing(
                verify_prompt,
                {"_model_preference": "", "_fallback_models": [], "description": "质量验证"}
            )
            content = resp.get("content", "")
            import json as _j
            start = content.find("{")
            end = content.rfind("}") + 1
            if start >= 0 and end > start:
                result = _j.loads(content[start:end])
                return {
                    "accepted": result.get("accepted", True),
                    "score": float(result.get("score", 7)),
                    "issues": result.get("issues", ""),
                }
        except Exception as e:
            logger.debug("[%s] 质量验证异常 (skip): %s", self._pm_id[:8], e)

        return None

    # ── 优化4: 结果聚合 ───────────────────────────────────────────

    def aggregate_results(self):
        """收集所有子任务结果, 调用 LLM 聚合为最终交付物。"""
        st = self._state
        if st.stop_event.is_set() or not st.plan or not st.task:
            return

        decomposition = st.plan.get("decomposition", [])
        missing = [sub.get("name", "") for sub in decomposition
                   if sub.get("name", "") not in st.subtask_outputs]
        if missing:
            self._fail_task(missing[0], "缺少成功的子任务输出，不能生成成功交付")
            return
        task_name = st.task.get("name", "")
        task_desc = st.task.get("description", "")

        subtask_results = []
        for d in decomposition:
            sub_name = d.get("name", "")
            output = st.subtask_outputs.get(sub_name, "")
            status = "completed" if sub_name in st.subtask_outputs else "unknown"
            for m in st.subagents.values():
                if m.get("current_task") == sub_name:
                    status = m.get("status", status)
                    break
            subtask_results.append({
                "name": sub_name,
                "skill": d.get("skill", ""),
                "status": status,
                "output": output,
            })

        logger.info("[%s] 开始聚合 %d 个子任务结果", self._pm_id[:8], len(subtask_results))

        if not self._agent.execution_allowed():
            return
        aggregated = st.subtask_outputs.get("_aggregated")
        if not aggregated:
            agg_prompt = build_aggregation_prompt(task_name, task_desc, subtask_results, st.plan)
            if not st.persist("aggregating"):
                return
            try:
                resp = self._runtime._call_llm_with_routing(
                    agg_prompt,
                    {"_model_preference": "", "_fallback_models": [], "description": "结果聚合"}
                )
                if st.stop_event.is_set():
                    return
                aggregated = resp.get("content", "")
                if (resp.get("_error") or resp.get("error") or resp.get("model") == "none"
                        or not isinstance(aggregated, str) or not aggregated.strip()
                        or aggregated.lstrip().startswith(("[未配置", "[LLM 调用失败", "[模型调用失败"))):
                    raise ValueError(aggregated or "模型未返回有效聚合结果")
            except Exception as e:
                logger.error("[%s] LLM 聚合失败: %s", self._pm_id[:8], e)
                self._agent.report_status("failed")
                self._agent.report_progress(0.0, "failed", f"结果聚合失败: {e}", task_name=task_name)
                self._agent.running = False
                return

            with st.lock:
                st.subtask_outputs["_aggregated"] = aggregated
                if not st.persist("aggregated"):
                    return
            logger.info("[%s] 聚合完成, 结果长度: %d 字符", self._pm_id[:8], len(aggregated))

        # 交付闭环
        if self._agent.deliver_result(task_name, task_desc, aggregated, subtask_results):
            self._agent.report_status("completed")
            self._agent.report_progress(1.0, "completed", f"任务聚合完成: {task_name}", task_name=task_name)
        else:
            self._agent.report_status("failed")
            self._agent.report_progress(0.0, "failed", "交付未获 Secretary 确认，请核实交付记录后处理", task_name=task_name)
        self._agent.running = False
