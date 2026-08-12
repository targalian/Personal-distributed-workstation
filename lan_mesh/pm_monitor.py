"""
PM 进度监控器 — 进度收集、超时检测、失败接管、质量验证

职责:
1. progress_loop 轮询收集进度
2. 接收子 Agent 进度上报
3. 超时检测 (全局 + 子任务)
4. 失败接管 (三级策略: 同站重试 → 换站重试 → PM 本地接管)
5. 质量验证 (F2.5 生成-验证器模式)
6. 结果聚合 (优化4)
7. 升级上报 (优化10)
"""
import time
import uuid
from typing import Optional

import requests

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
        self._local_takeover_tasks = set()  # 防重入: PM本地接管中的子任务

    # ── 进度轮询 ──────────────────────────────────────────────────

    def progress_loop(self):
        """定期收集进度并上报 Secretary (优化4: 全部完成后触发聚合)。"""
        _aggregated = False
        st = self._state

        while self._agent.running:
            time.sleep(10)

            # F1.3: 全局超时检测
            if self.is_global_timed_out():
                logger.error("[%s] 全局任务超时 (%.0fs), 强制终止",
                            self._pm_id[:8], st.global_timeout)
                self._agent.report_progress(0.0, "failed", f"全局超时 ({st.global_timeout}s)")
                self._agent.running = False
                break

            # F1.3: 子任务超时检测
            self.check_subtask_timeouts()

            with st.lock:
                if not st.subagents:
                    continue
                total = len(st.subagents)
                completed = sum(1 for m in st.subagents.values() if m.get("status") == "completed")
                failed = sum(1 for m in st.subagents.values() if m.get("status") == "failed")
                has_pending = bool(st.pending_subtasks)

            overall = completed / total if total > 0 else 0.0
            self._agent.report_progress(overall, "in_progress" if completed < total else "completed",
                                        f"整体进度: {completed}/{total} 子任务完成")

            # 优化4: 全部子任务完成 (含 failed) 且无 pending, 触发结果聚合
            if not _aggregated and not has_pending:
                finished = completed + failed
                if finished >= total and total > 0:
                    _aggregated = True
                    self.aggregate_results()

    # ── 接收进度上报 ──────────────────────────────────────────────

    def receive_progress_report(self, report: dict):
        """接收子 Agent 主动上报的进度 (通过 Worker API 转发)。

        优化1: 当子任务完成时, 自动检查是否有等待依赖的后续任务可以分发。
        优化5: 当子任务失败时, 触发接管策略。
        """
        st = self._state
        reporter_id = report.get("reporter_id", "")
        task_name = report.get("task_name", "")
        status = report.get("status", "in_progress")

        with st.lock:
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
            # 本地接管成功的任务, 移出集合, 防止同名子任务后续失败被误判为已接管
            self._local_takeover_tasks.discard(task_name)
            with st.lock:
                st.subtask_outputs[task_name] = output

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
            if quality and not quality.get("accepted", True):
                logger.warning("[%s] 子任务 '%s' 质量未达标 (score=%.1f): %s",
                              self._pm_id[:8], task_name, quality.get("score", 0),
                              quality.get("issues", "")[:150])
                with st.lock:
                    retry_count = st.retry_counts.get(task_name, 0)
                if retry_count < st.max_retries:
                    with st.lock:
                        st.subtask_outputs.pop(task_name, None)
                    self.handle_subagent_failure(task_name, f"质量验证未通过: {quality.get('issues', '')}")
                    return

            self._dispatcher.try_dispatch_pending()

        # 优化5: 任务失败时触发接管策略 (跳过已在本地接管中的任务, 防止无限递归)
        if status == "failed" and task_name:
            if task_name in self._local_takeover_tasks:
                logger.error("[%s] 子任务 '%s' 本地接管后仍失败, 放弃重试",
                            self._pm_id[:8], task_name)
                self._local_takeover_tasks.discard(task_name)
            else:
                error_msg = report.get("message", "未知错误")
                logger.error("[%s] 子任务 '%s' 失败: %s", self._pm_id[:8], task_name, error_msg[:200])
                self.handle_subagent_failure(task_name, error_msg)

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
        """F1.3: 检测子任务超时, 触发失败接管。"""
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
            logger.warning("[%s] 子任务 '%s' 超时 (%.0fs), 触发接管",
                          self._pm_id[:8], task_name, st.subtask_timeout)
            self.handle_subagent_failure(task_name, f"子任务超时 ({st.subtask_timeout}s)")

    # ── 优化5: 失败接管 ───────────────────────────────────────────

    def handle_subagent_failure(self, task_name: str, error_msg: str):
        """三级策略: 同站重试 → 换站重试 → PM 本地接管。"""
        st = self._state
        with st.lock:
            retry_count = st.retry_counts.get(task_name, 0)

        decomposition = st.plan.get("decomposition", []) if st.plan else []
        sub = None
        for d in decomposition:
            if d.get("name") == task_name:
                sub = d
                break
        if not sub:
            logger.warning("[%s] 失败子任务 '%s' 未在 plan 中找到, 跳过接管", self._pm_id[:8], task_name)
            return

        original_station = st.task_station.get(task_name, {})
        original_agent = st.task_agent.get(task_name, {})

        # 策略1: 同站重试
        if retry_count < st.max_retries:
            with st.lock:
                st.retry_counts[task_name] = retry_count + 1
            logger.info("[%s] 同站重试 '%s' (第 %d 次)", self._pm_id[:8], task_name, retry_count + 1)
            agent_id = original_agent.get("agent_id", "")
            if agent_id and original_station:
                task = dict(st.task)
                input_data = dict(task.get("input_data", {}))
                input_data["_retry_context"] = {
                    "attempt": retry_count + 1,
                    "previous_error": error_msg[:500],
                }
                task["input_data"] = input_data
                self._dispatcher.dispatch_subtask(original_station, original_agent, task, sub, plan=st.plan)
            return

        # 策略2: 换站重试
        stations = self._dispatcher.get_available_stations()
        other_stations = [s for s in stations
                          if s.get("device_id") != original_station.get("device_id")]
        if other_stations:
            new_station = other_stations[0]
            logger.info("[%s] 换站重试 '%s' → %s",
                       self._pm_id[:8], task_name,
                       new_station.get('device_name', new_station.get('ip', '')))
            with st.lock:
                st.retry_counts[task_name] = retry_count + 1

            new_agent_id = f"sub-{uuid.uuid4().hex[:10]}"
            agent_name = sub.get("name", "worker")
            skill = sub.get("skill", "code_generation")
            system_prompt = self._dispatcher._build_subagent_prompt_for_sub(
                st.task, sub, st.plan, new_agent_id, agent_name
            )
            new_agent_info = self._dispatcher._create_subagent_on_station(
                new_station, agent_name, [skill], sub.get("description", ""),
                system_prompt=system_prompt,
                preferred_agent_id=new_agent_id,
            )
            if new_agent_info:
                st.task_station[task_name] = new_station
                st.task_agent[task_name] = new_agent_info
                self._dispatcher.dispatch_subtask(new_station, new_agent_info, st.task, sub, plan=st.plan)
            return

        # 策略3: PM 本地接管 (最终回退, 失败后不再递归)
        logger.info("[%s] PM 本地接管子任务 '%s'", self._pm_id[:8], task_name)
        self._local_takeover_tasks.add(task_name)
        self._dispatcher.execute_subtask_locally(st.task, sub)
        self._report_escalation(task_name, error_msg, sub)

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
        if not st.plan or not st.task:
            return

        decomposition = st.plan.get("decomposition", [])
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

        agg_prompt = build_aggregation_prompt(task_name, task_desc, subtask_results, st.plan)
        try:
            resp = self._runtime._call_llm_with_routing(
                agg_prompt,
                {"_model_preference": "", "_fallback_models": [], "description": "结果聚合"}
            )
            aggregated = resp.get("content", "")
        except Exception as e:
            logger.error("[%s] LLM 聚合失败: %s", self._pm_id[:8], e)
            aggregated = f"[聚合失败] {e}"

        self._agent.report_status("completed")
        self._agent.report_progress(1.0, "completed", f"任务聚合完成: {task_name}", task_name=task_name)

        st.subtask_outputs["_aggregated"] = aggregated
        logger.info("[%s] 聚合完成, 结果长度: %d 字符", self._pm_id[:8], len(aggregated))

        # 交付闭环
        self._agent.deliver_result(task_name, task_desc, aggregated, subtask_results)

    # ── 优化10: 升级上报 ──────────────────────────────────────────

    def _report_escalation(self, task_name: str, error_msg: str, sub: dict):
        """三级接管全失败后, 上报 escalated 状态到 Secretary。"""
        st = self._state
        escalation = {
            "pm_id": self._pm_id,
            "task_id": st.task.get("task_id", ""),
            "task_name": st.task.get("name", ""),
            "failed_subtask": task_name,
            "error": error_msg[:500],
            "retry_count": st.retry_counts.get(task_name, 0),
            "options": [
                "忽略此子任务, 继续执行其他部分",
                "降低质量要求, 用简化方案重试",
                "手动指定一台主机重试",
                "放弃整个任务",
            ],
            "escalated_at": time.time(),
        }

        self._agent.report_status("escalated")
        self._agent.report_progress(
            -2.0, "escalated",
            f"子任务 '{task_name}' 三级接管全失败: {error_msg[:200]}",
            reporter_type="pm_escalation",
            task_name=task_name,
        )

        try:
            requests.post(
                f"{self._secretary_url}/api/pm/{self._pm_id}/status",
                json={"status": "escalated", "escalation": escalation},
                timeout=5,
            )
        except Exception:
            pass

        logger.warning("[%s] 子任务 '%s' 已上报 escalated, 等待 Boss 决策", self._pm_id[:8], task_name)
