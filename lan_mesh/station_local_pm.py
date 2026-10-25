"""
内嵌 PM 执行域 — 本机启停/恢复/注入、队列派发、子 Agent、孤儿迁移。

拆分自 station_controller.py (iter-75 Phase 4)。当本机同时充当执行节点时,
PM Agent 在 Secretary 进程内直接运行 (而非派发到远端 Worker)。_local_start_pm
是兼容启动入口，iter-105 起经 Yomk PmLifecycleService 串行化；接力与接管复用 TaskSchedulingService。
"""
import threading
import time

import requests

from .http_retry import auth_headers
from .logger import get_logger

logger = get_logger("station")


class StationLocalPmMixin:
    """本机内嵌 PM 的生命周期与派发接力。

    依赖 StationController 提供的属性: cfg / state / db / runtime /
    pm_agents / _pm_worker_map / local_pm / project_manager 等。
    """

    # ── 内嵌 Worker: 本机 PM Agent 管理 ─────────────────────

    def _local_start_pm(self, task_id: str, secretary_url: str, task_data: dict = None) -> dict:
        """Compatibility entry routed through the Yomk PM lifecycle service."""
        from .pm_lifecycle_service import call_pm
        return call_pm(self, "start", task_id=task_id, secretary_url=secretary_url, task_data=task_data)

    def _dispatch_queued_task(self) -> bool:
        """Compatibility queue relay through the shared scheduling service."""
        from .task_scheduling_service import call_schedule
        return call_schedule(self, "relay")

    def _register_local_pm(self, pm_id: str, task) -> None:
        """Compatibility entry routed through the Yomk PM lifecycle service."""
        from .pm_lifecycle_service import call_pm
        return call_pm(self, "register_local", pm_id=pm_id, task=task)

    def _local_stop_pm(self) -> dict:
        """Compatibility entry routed through the Yomk PM lifecycle service."""
        from .pm_lifecycle_service import call_pm
        return call_pm(self, "stop")

    def _local_resume_pm(self, task_id: str) -> dict:
        """Compatibility entry routed through the Yomk PM lifecycle service."""
        from .pm_lifecycle_service import call_pm
        return call_pm(self, "resume", task_id=task_id)

    def _local_cancel_pm(self) -> dict:
        """Compatibility entry routed through the Yomk PM lifecycle service."""
        from .pm_lifecycle_service import call_pm
        return call_pm(self, "cancel")

    def _local_pause_pm(self) -> dict:
        """Compatibility entry routed through the Yomk PM lifecycle service."""
        from .pm_lifecycle_service import call_pm
        return call_pm(self, "pause")

    def _local_inject_input(self, input_data: dict) -> dict:
        """Compatibility entry routed through the Yomk PM lifecycle service."""
        from .pm_lifecycle_service import call_pm
        return call_pm(self, "inject_input", input_data=input_data)

    def _auto_attach_pm_thread(self, pm_id: str, task_name: str, agent_name: str):
        """方案C: PM 启动后自动绑定到当前活跃对话的线程列表。"""
        ch = self.chat_handler
        if not ch:
            return
        conv_id = ch._active_conv_id
        if not conv_id:
            return
        try:
            ch.attach_pm_thread(conv_id, pm_id, task_name=task_name, agent_name=agent_name)
        except Exception as e:
            logger.debug("方案C 自动绑定 PM 线程失败 (non-critical): %s", e)

    def _local_pm_status(self) -> dict:
        """Compatibility entry routed through the Yomk PM lifecycle service."""
        from .pm_lifecycle_service import call_pm
        return call_pm(self, "status")

    def _local_create_subagent(self, agent_name: str, skills: list,
                               task_description: str = "", system_prompt: str = "",
                               preferred_agent_id: str = "") -> dict:
        """在本机创建子 Agent。"""
        import uuid as _uuid
        from .agent_runtime import AgentRuntime
        agent_id = preferred_agent_id or f"sub-{_uuid.uuid4().hex[:10]}"
        sub_runtime = AgentRuntime(
            agent_id=agent_id,
            shared_folder_path=str(self.state.shared_folder.path),
            custom_system_prompt=system_prompt,
        )
        self._local_sub_agents[agent_id] = {
            "agent_id": agent_id, "agent_name": agent_name,
            "runtime": sub_runtime, "skills": skills,
            "current_task": task_description, "status": "idle",
        }
        logger.info("本机子 Agent 已创建: %s (%s)", agent_id, agent_name)
        return {"agent_id": agent_id, "agent_name": agent_name}

    def _local_cancel_subagent(self, agent_id: str) -> dict:
        """Cancel a local sub-agent without discarding its registration."""
        info = self._local_sub_agents.get(agent_id)
        if not info:
            return {"ok": False, "message": f"子 Agent {agent_id} 不存在"}
        runtime = info.get("runtime")
        cancel_runtime = getattr(runtime, "cancel", None)
        if callable(cancel_runtime):
            cancel_runtime()
        info["status"] = "cancelled"
        logger.info("本机子 Agent 已请求取消: %s", agent_id)
        return {"ok": True, "agent_id": agent_id}

    def _local_forward_progress(self, report: dict) -> dict:
        if not self._local_pm_agent:
            return {"ok": False, "message": "PM Agent 未运行"}
        self._local_pm_agent.receive_progress_report(report)
        return {"ok": True}

    def _local_execute_task(self, payload: dict) -> dict:
        """本机执行 PM 分发的子任务。

        根据 reporter_id 定位子 Agent Runtime，找不到则回退到 chat_runtime。
        执行期间更新子 Agent 状态 (idle → executing → idle)。
        """
        reporter_id = payload.get("reporter_id", "")
        sub_info = self._local_sub_agents.get(reporter_id)

        if sub_info:
            runtime = sub_info["runtime"]
            sub_info["status"] = "executing"
            sub_info["current_task"] = payload.get("name", "")
            logger.info("本机子 Agent 执行任务: %s (%s)", reporter_id, payload.get("name", ""))
        else:
            # 回退: 使用通用 runtime
            runtime = self.chat_runtime
            if not runtime:
                return {"output": {}, "status": "failed", "error": "Agent 运行时未初始化"}
            logger.warning("子 Agent %s 未找到, 回退到通用 Runtime", reporter_id)

        try:
            result = runtime.execute(payload)
        except Exception as e:
            logger.error("本机子任务执行异常: %s", e)
            result = {"output": {}, "status": "failed", "error": str(e)}
        finally:
            if sub_info:
                sub_info["status"] = (
                    "cancelled" if result.get("status") == "cancelled" else "idle"
                )

        # 执行完成后向 PM 上报
        task_name = payload.get("name", "")
        status = result.get("status", "failed")
        output_data = result.get("output", {})
        self._report_subtask_result(payload, task_name, status,
                                    output_data, reporter_id)

        return result

    def _report_subtask_result(self, payload: dict, task_name: str, status: str,
                               output_data, reporter_id: str) -> None:
        """回报子任务结果, 跨站任务回传到原始 PM 所在 Secretary。

        BUG-034: 原实现无条件调用本机 PM 的 receive_subtask_result。当子任务
        由远程 PM 分发到本机执行时, 结果被错误注入本机 PM (或在本机无 PM 时
        直接丢弃), 原始 PM 永远等不到回调, 只能耗到全局超时失败。
        """
        originating_pm_id = str(payload.get("pm_id", "") or "")
        local_pm_id = ""
        if self._local_pm_agent:
            local_pm_id = str(getattr(self._local_pm_agent, "pm_id", "") or "")

        normalized = (output_data if isinstance(output_data, dict)
                      else {"result": str(output_data)})

        # 同一 PM (或无 pm_id 的旧版调用): 直接本地注入
        if not originating_pm_id or originating_pm_id == local_pm_id:
            if self._local_pm_agent and self._local_pm_agent.running:
                self._local_pm_agent.receive_subtask_result(
                    task_name=task_name,
                    status=status,
                    output_data=output_data,
                    agent_id=reporter_id,
                )
            else:
                logger.warning("[Station] 子任务 '%s' 完成但本机无运行中 PM, 结果无处上报",
                               task_name)
            return

        # 跨站: 回传原始 PM 的 Secretary
        secretary_url = str(payload.get("secretary_url", "") or "")
        if not secretary_url:
            logger.error("[Station] 跨站子任务 '%s' 缺少 secretary_url, 结果无法回传 PM %s",
                         task_name, originating_pm_id[:8])
            return

        report = {
            "reporter_id": reporter_id,
            "task_name": task_name,
            "status": status,
            "progress": 1.0 if status == "completed" else 0.0,
            "output": normalized,
            "message": f"跨站子任务 {task_name} {status}",
        }
        try:
            resp = requests.post(
                f"{secretary_url}/pm/progress-report",
                json=report,
                headers=auth_headers(),
                timeout=10,
            )
            if resp.status_code == 200:
                logger.info("[Station] 跨站子任务 '%s' 结果已回传 PM %s (%s)",
                            task_name, originating_pm_id[:8], status)
            else:
                logger.error("[Station] 跨站子任务 '%s' 结果回传失败: HTTP %s",
                             task_name, resp.status_code)
        except Exception as e:
            logger.error("[Station] 跨站子任务 '%s' 结果回传异常: %s", task_name, e)

    def _start_local_pm_for_task(self, task_id: str) -> bool:
        """Failover takeover shares local PM claiming and registration with the queue."""
        from .task_scheduling_service import call_schedule
        task = self.db.get_task(task_id)
        if not task:
            return False
        return call_schedule(self, "dispatch_local", task=task)

    # ── F3.3: PM Agent 故障迁移 ─────────────────────────────────

    def _migrate_orphaned_pms(self, gone_device_ids: list[str]):
        """F3.3: 检测离线主机上的 PM, 将其任务迁移到可用节点或本机接管。

        策略:
        1. 扫描 _pm_worker_map, 找出 device_id 在 gone_ids 中的 PM
        2. 查找该 PM 关联的任务, 重置为 pending
        3. 优先派发到其他空闲在线 Worker, 否则本机接管
        """
        gone_set = set(gone_device_ids)
        orphaned_pms = []

        for pm_id, info in list(self._pm_worker_map.items()):
            if info.get("device_id") in gone_set:
                orphaned_pms.append((pm_id, info))

        if not orphaned_pms:
            return

        logger.warning("[F3.3] 检测到 %d 个 PM 因主机离线而孤立", len(orphaned_pms))

        # 查找可用替代 Worker (在线 + 排除离线 + 排除忙碌, iter-66 修复)
        hosts = self.db.list_hosts()
        available_workers = [
            h for h in hosts
            if getattr(h, 'online', False)
            and getattr(h, 'role', '') == 'worker'
            and getattr(h, 'device_id', '') not in gone_set
            and not self._is_worker_busy(h)
        ]

        for pm_id, info in orphaned_pms:
            # 查找关联任务
            task_id = info.get("task_id", "")
            if task_id:
                task = self.db.get_task(task_id)
                if task and getattr(task, 'status', '') in ('running', 'monitoring'):
                    # 重置任务状态
                    task.status = "pending"
                    task.pm_agent_id = ""
                    # iter-66 修复 (Bug A): Database 无 upsert_task, save_task
                    # 自带 ON CONFLICT DO UPDATE 的 upsert 语义
                    self.db.save_task(task)
                    logger.info("[F3.3] 任务 %s 已重置为 pending", task_id[:8])

                    # 尝试迁移
                    if available_workers:
                        target = available_workers[0]
                        ok = self._dispatch_task_to_worker(task, target)
                        logger.info("[F3.3] 任务已迁移到 %s (派发%s)",
                                   getattr(target, 'device_name', ''),
                                   "成功" if ok else "失败")
                    else:
                        # 本机接管
                        logger.info("[F3.3] 无可用 Worker, 本机接管任务 %s", task_id[:8])
                        if not self._start_local_pm_for_task(task_id):
                            # iter-69 (Bug L): 接管失败任务留在 pending,
                            # 由自动扩容/接力派发下一轮兜底重试
                            logger.warning("[F3.3] 接管失败, 任务 %s 留在 "
                                           "pending 等待下轮派发", task_id[:8])

            # 清理映射
            del self._pm_worker_map[pm_id]

    def _try_periodic_report(self):
        """优化12: 定期汇报 — 当有活跃任务时, 生成简报推送到 Web UI 和 Bot。

        每 _report_interval 秒检查一次, 如果有 running/pending 任务:
        1. 汇总所有 PM 进度
        2. 检测异常 (进度停滞、资源不足)
        3. 生成简报推送到 WebSocket + Bot
        """
        now = time.time()
        if now - self._last_report_time < self._report_interval:
            return
        self._last_report_time = now

        try:
            # 检查是否有活跃任务
            tasks = self.db.list_tasks(limit=20)
            active_tasks = [t for t in tasks if t.status in ("running", "pending")]
            if not active_tasks:
                return  # 无活跃任务, 不汇报

            # 汇总 PM 进度
            pm_lines = []
            pms = self.db.list_pm_agents()
            for pm in pms:
                if pm.status in ("planning", "executing", "monitoring", "awaiting_input", "escalated"):
                    reports = self.db.get_progress_reports(pm.pm_id, limit=1)
                    progress = reports[0]["progress"] if reports else 0.0
                    pm_lines.append(f"{pm.agent_name}: {pm.status} ({progress*100:.0f}%)")

            # 检测异常
            alerts = []
            for pm in pms:
                if pm.status == "escalated":
                    alerts.append(f"🚨 {pm.agent_name} 已升级, 需要决策")
                elif pm.status == "awaiting_input":
                    alerts.append(f"⚠️ {pm.agent_name} 等待您的输入")

            # 生成简报
            report = (
                f"📊 工作站定期汇报\n"
                f"活跃任务: {len(active_tasks)} 个\n"
                f"PM Agent: {', '.join(pm_lines) if pm_lines else '无活跃 PM'}\n"
            )
            if alerts:
                report += f"⚠️ 注意: {'; '.join(alerts)}\n"

            # Bot 推送 (仅当有异常或活跃任务时)
            self.bot_gateway.notify("periodic_report", {
                "active_tasks": len(active_tasks),
                "summary": report[:300],
            })

            logger.info("定期汇报: %d 个活跃任务", len(active_tasks))

        except Exception as e:
            logger.error("定期汇报异常: %s", e)
