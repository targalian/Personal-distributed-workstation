"""
内嵌 PM 执行域 — 本机启停/恢复/注入、队列派发、子 Agent、孤儿迁移。

拆分自 station_controller.py (iter-75 Phase 4)。当本机同时充当执行节点时,
PM Agent 在 Secretary 进程内直接运行 (而非派发到远端 Worker)。_local_start_pm
是唯一启动入口 (iter-69 修复), 接力派发、故障接管与孤儿迁移均复用它。
"""
import threading
import time

from .logger import get_logger

logger = get_logger("station")


class StationLocalPmMixin:
    """本机内嵌 PM 的生命周期与派发接力。

    依赖 StationController 提供的属性: cfg / state / db / runtime /
    pm_agents / _pm_worker_map / local_pm / project_manager 等。
    """

    # ── 内嵌 Worker: 本机 PM Agent 管理 ─────────────────────

    def _local_start_pm(self, task_id: str, secretary_url: str, task_data: dict = None) -> dict:
        """在本机 Station 进程内直接启动 PM Agent (无需 Worker)。"""
        if self._local_pm_agent and getattr(self._local_pm_agent, '_running', False):
            return {"ok": False, "message": "本机 PM Agent 已在运行"}
        # iter-55: 让位主机 (网络中已有 Secretary, 本机未激活) 也可执行
        # 远程派发的 PM 任务 — 惰性初始化专用 AgentRuntime, 与激活解耦
        if not self.chat_runtime:
            from .agent_runtime import AgentRuntime
            self.chat_runtime = AgentRuntime(
                agent_id=f"worker-{self.state.device_id[:8]}",
                shared_folder_path=str(self.state.shared_folder.path),
            )
            logger.info("让位主机惰性初始化 Worker AgentRuntime (%s)",
                        self.state.device_name)

        import uuid as _uuid
        from .pm_agent import ProjectManagerAgent
        pm_id = f"pm-{_uuid.uuid4().hex[:12]}"

        if not task_data:
            return {"ok": False, "message": "缺少任务数据"}

        self._local_pm_agent = ProjectManagerAgent(
            pm_id=pm_id,
            agent_runtime=self.chat_runtime,
            secretary_url=secretary_url,
            device_id=self.state.device_id,
            device_name=self.state.device_name,
        )
        self._local_pm_agent.start_task(task_data)
        logger.info("本机 PM Agent 已启动: %s, 任务: %s", pm_id, task_id)
        # 方案C: 自动将 PM 绑定到当前活跃对话的线程
        task_name = task_data.get("name", task_id)
        self._auto_attach_pm_thread(pm_id, task_name, f"PM-{pm_id[:8]}")
        return {"ok": True, "pm_id": pm_id, "device_id": self.state.device_id}

    def _dispatch_queued_task(self) -> bool:
        """iter-57 (补强#5): PM 空闲后接力派发队列中最早 pending 任务。

        并发提交时本机 PM 忙 → 任务排队 (pending); 本机 PM 完成后调用
        本方法接力派发。若 PM 仍在收尾 (completed 上报时线程尚未退出),
        后台线程等待其结束后再派发, 避免请求线程内自调用阻塞
        (压测发现 20 并发提交时 19 个任务瞬时 failed + ReadTimeout)。
        """
        pm = self._local_pm_agent
        if pm and getattr(pm, '_running', False):
            # PM 仍在收尾: 后台等待空闲后接力 (最多 120s), 防重入
            if not getattr(self, '_queued_dispatch_waiting', False):
                self._queued_dispatch_waiting = True

                def _wait_then_dispatch():
                    deadline = time.time() + 120
                    while time.time() < deadline:
                        time.sleep(2)
                        cur = self._local_pm_agent
                        if not cur or not getattr(cur, '_running', False):
                            break
                    self._queued_dispatch_waiting = False
                    self._dispatch_queued_task()

                threading.Thread(target=_wait_then_dispatch,
                                 daemon=True, name="queued-dispatch").start()
            return False

        tasks = self.db.list_tasks()
        pending = [t for t in tasks if getattr(t, 'status', '') == 'pending']
        if not pending:
            return False
        # iter-66 (Bug D 修复): FIFO — 取最早提交的任务 (list_tasks 为 DESC)
        task = min(pending, key=lambda t: getattr(t, 'created_at', 0) or 0)
        secretary_url = f"http://127.0.0.1:{self.state.api_port}"
        result = self._local_start_pm(task.task_id, secretary_url, task.to_dict())
        if not result.get("ok"):
            logger.warning("排队任务接力派发失败: %s", result.get("message"))
            return False

        pm_id = result["pm_id"]
        task.pm_agent_id = pm_id
        task.status = "running"
        self.db.save_task(task)
        self._register_local_pm(pm_id, task)
        logger.info("排队任务已接力派发: %s (%s) → PM %s",
                    task.task_id[:8], task.name, pm_id[:8])
        return True

    def _register_local_pm(self, pm_id: str, task) -> None:
        """本机 PM 启动后的统一登记 (落库 + 映射 + WS 广播)。

        iter-69 (Bug L 修复): 接力派发与 F3.3 本机接管共用此登记, 避免
        接管路径漏登记导致 PM 查不到、无法取消、二次迁移失联。
        """
        from .protocol import PMAgent
        self.db.upsert_pm_agent(PMAgent(
            pm_id=pm_id,
            agent_name=f"PM-{pm_id[:8]}",
            task_id=task.task_id,
            project_id=getattr(task, 'project_id', '') or '',
            device_id=self.state.device_id,
            hostname=self.state.device_name,
            ip="127.0.0.1",
            api_port=self.state.api_port,
            status="starting",
        ))
        self._pm_worker_map[pm_id] = {
            "ip": "127.0.0.1",
            "api_port": self.state.api_port,
            "device_id": self.state.device_id,
            "local": True,
            "task_id": task.task_id,   # iter-66 (Bug B): F3.3 迁移依赖
        }
        self._queue_ws_broadcast("pm_registered", {
            "pm_id": pm_id, "task_id": task.task_id,
            "device_id": self.state.device_id,
            "device_name": self.state.device_name,
        })
        self._queue_ws_broadcast("task_updated", task.to_dict())

    def _local_stop_pm(self) -> dict:
        """停止本机 PM Agent。"""
        if not self._local_pm_agent:
            return {"ok": False, "message": "PM Agent 未运行"}
        self._local_pm_agent.cancel()
        pm_id = self._local_pm_agent.pm_id
        self._local_pm_agent = None
        self._local_sub_agents.clear()
        logger.info("本机 PM Agent 已停止: %s", pm_id)
        return {"ok": True, "pm_id": pm_id}

    def _local_resume_pm(self, task_id: str) -> dict:
        """iter-53: 从执行态快照恢复中断的 PM Agent (断点续跑)。

        重建 PM Agent 实例并注入快照状态, 保留已完成子任务输出,
        重新分发未完成部分; 任务状态重置为 running。
        """
        if self._local_pm_agent and getattr(self._local_pm_agent, '_running', False):
            return {"ok": False, "message": "本机 PM Agent 正在运行, 无法恢复其他任务"}
        # iter-55: 与 _local_start_pm 一致 — 让位主机惰性初始化 runtime
        if not self.chat_runtime:
            from .agent_runtime import AgentRuntime
            self.chat_runtime = AgentRuntime(
                agent_id=f"worker-{self.state.device_id[:8]}",
                shared_folder_path=str(self.state.shared_folder.path),
            )

        snapshot = self.db.get_pm_snapshot_by_task(task_id)
        if not snapshot:
            return {"ok": False, "message": "无执行态快照, 无法恢复"}

        task = self.db.get_task(task_id)
        if not task:
            return {"ok": False, "message": "任务不存在"}

        from .pm_agent import ProjectManagerAgent
        pm_id = snapshot["pm_id"]
        secretary_url = f"http://127.0.0.1:{self.state.api_port}"

        self._local_pm_agent = ProjectManagerAgent(
            pm_id=pm_id,
            agent_runtime=self.chat_runtime,
            secretary_url=secretary_url,
            device_id=self.state.device_id,
            device_name=self.state.device_name,
        )
        if not self._local_pm_agent.resume_from_snapshot(snapshot):
            self._local_pm_agent = None
            return {"ok": False, "message": "快照解析失败, 无法恢复"}

        # 任务状态重置为 running (恢复线程内会继续上报进度)
        task.status = "running"
        task.pm_agent_id = pm_id
        self.db.save_task(task)
        logger.info("本机 PM Agent 已从快照恢复: %s, 任务: %s", pm_id, task_id)
        return {"ok": True, "pm_id": pm_id}

    def _local_cancel_pm(self) -> dict:
        if not self._local_pm_agent:
            return {"ok": False, "message": "PM Agent 未运行"}
        self._local_pm_agent.cancel()
        return {"ok": True, "pm_id": self._local_pm_agent.pm_id}

    def _local_pause_pm(self) -> dict:
        if not self._local_pm_agent:
            return {"ok": False, "message": "PM Agent 未运行"}
        self._local_pm_agent.pause()
        return {"ok": True, "pm_id": self._local_pm_agent.pm_id}

    def _local_inject_input(self, input_data: dict) -> dict:
        if not self._local_pm_agent or not self._local_pm_agent._running:
            return {"ok": False, "message": "PM Agent 未运行"}
        self._local_pm_agent.receive_input(input_data)
        return {"ok": True, "pm_id": self._local_pm_agent.pm_id}

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
        if not self._local_pm_agent:
            return {"running": False}
        return self._local_pm_agent.get_status()

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
                sub_info["status"] = "idle"

        # 执行完成后向 PM 上报
        if self._local_pm_agent and self._local_pm_agent.running:
            task_name = payload.get("name", "")
            status = result.get("status", "failed")
            output_data = result.get("output", {})
            self._local_pm_agent.receive_subtask_result(
                task_name=task_name,
                status=status,
                output_data=output_data,
                agent_id=reporter_id,
            )

        return result

    def _start_local_pm_for_task(self, task_id: str) -> bool:
        """F3.3: 本机启动 PM Agent 接管指定任务。

        iter-69 (Bug L 修复): 原实现自行构造 ProjectManagerAgent 且用的是
        早期签名 (task=/runtime=), 真机接管必然 TypeError —— 七节点实压日志
        "[F3.3] 本机接管失败: ProjectManagerAgent.__init__() got an
        unexpected keyword argument 'task'", 任务停在 pending 无人接管。
        改为复用唯一入口 `_local_start_pm` (与接力派发同路径, 带 runtime
        懒初始化 + start_task 真正跑起来), 并补齐 PM 落库 + 映射登记。
        """
        task = self.db.get_task(task_id)
        if not task:
            logger.warning("[F3.3] 本机接管失败: 任务不存在 %s", task_id[:8])
            return False

        secretary_url = f"http://127.0.0.1:{self.state.api_port}"
        try:
            result = self._local_start_pm(task_id, secretary_url,
                                          task.to_dict())
        except Exception as e:
            logger.error("[F3.3] 本机接管失败: %s", e)
            return False
        if not result.get("ok"):
            logger.error("[F3.3] 本机接管失败: %s", result.get("message"))
            return False

        pm_id = result["pm_id"]
        task.pm_agent_id = pm_id
        task.status = "running"
        self.db.save_task(task)
        self._register_local_pm(pm_id, task)
        logger.info("[F3.3] 本机 PM 已接管任务 %s → PM %s",
                    task_id[:8], pm_id[:8])
        return True

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
