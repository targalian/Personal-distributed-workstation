"""
任务提交与调度域 — 负载选站、联邦转发轮询、自动扩容派发。

拆分自 station_controller.py (iter-75 Phase 3)。任务从 Boss/秘书进入系统后的
去向决策都在这里: 先按负载与技能选站 (本机/远端 Worker/跨网段联邦), 再由
F3.1 自动扩缩容守护持续把积压队列派发给空闲 Worker (单轮批量清空)。
"""
import threading
import time
import uuid

from .http_retry import http_post
from .logger import get_logger

logger = get_logger("station")


class StationSchedulerMixin:
    """任务提交、选站、联邦转发与自动扩容。

    依赖 StationController 提供的属性: cfg / db / state / director /
    _running / pm_agents / _pm_worker_map / project_manager 等。
    """

    def submit_task_from_chat(self, name: str, description: str, created_by: str = "secretary",
                              priority: str = "normal",
                              fed_relay: bool = False) -> dict:
        """从秘书对话直接提交任务并分配 PM Agent。

        与 station_api.submit_task() 逻辑一致, 但同步执行。
        优化13: 支持优先级 + 负载感知选站。

        Args:
            priority: 优先级 (low / normal / high / urgent)
            fed_relay: iter-65 联邦防环 — 本任务已是跨网段委托任务
                (对端转来), 本侧选站再命中联邦主机时不再回传,
                直接失败终止 (防 A↔B 互相委托死循环)
        """
        from .protocol import Task, PMAgent

        task = Task(
            task_id=f"task-{uuid.uuid4().hex[:12]}",
            name=name,
            description=description,
            created_by=created_by,
            status="pending",
        )
        # 优化13: 记录优先级到 input_data
        task.input_data = task.input_data or {}
        task.input_data["_priority"] = priority
        # iter-65: 联邦防环标记落盘 (审计可见)
        if fed_relay:
            task.input_data["_federation_relay"] = True
        # F4.4 (iter-52): 成本感知调度 — 预算预估 + 适配检查 (异常静默, 不阻断)
        try:
            from .budget_advisor import build_task_cost_estimate
            cost_est = build_task_cost_estimate(
                name, description, self.db, project_id="",
                project_manager=self.project_manager)
            task.input_data["_cost_estimate"] = cost_est
            fit = cost_est.get("budget_fit", {})
            if fit.get("status") in ("tight", "insufficient"):
                self.bot_gateway.notify("cost_budget_warning", {
                    "name": name, "task_id": task.task_id[:8],
                    "estimated": cost_est["estimated_tokens"],
                    "status": fit.get("label", fit.get("status", "")),
                    "advice": fit.get("advice", ""),
                })
                self._queue_ws_broadcast("cost_budget_warning", {
                    "task_id": task.task_id, "name": name,
                    "estimated_tokens": cost_est["estimated_tokens"],
                    "status": fit.get("status", "unknown"),
                    "advice": fit.get("advice", ""),
                })
        except Exception:
            pass  # 预估失败不影响任务提交
        self.db.save_task(task)
        logger.info("对话提交任务: %s (%s) 优先级=%s", task.task_id, name, priority)
        # WS 广播: 通知前端任务面板刷新
        self._queue_ws_broadcast("task_submitted", task.to_dict())
        # P3: 任务流追踪 — 提交阶段点 (异常静默)
        try:
            from . import runtime_trace
            runtime_trace.trace_task_event(
                task.task_id, "submitted",
                detail=f"{name} (created_by={created_by}, 优先级={priority})")
        except Exception:
            pass

        # 选择在线 work_station (优化13: 评级 + 负载感知; iter-65: lan 优先 fed 兜底)
        hosts = self.db.list_hosts()
        online_hosts = [h for h in hosts if h.online and h.device_id != self.state.device_id]
        if not online_hosts:
            online_hosts = [h for h in hosts if h.online]

        if not online_hosts:
            task.status = "failed"
            task.output_data = {"error": "无可用 work_station"}
            self.db.save_task(task)
            return task.to_dict()

        # iter-65 (F3.4 联邦任务转发): 选站分层 — 本网段优先, 联邦兜底
        target_host = self._pick_task_host(online_hosts)

        # 构造 Secretary URL: 本机派发用 localhost; 远程派发在回退分支重建可达地址
        secretary_url = f"http://127.0.0.1:{self.state.api_port}"

        # 优先本机派发 (内嵌 Worker, 无需单独 Worker 进程)
        if self.secretary_active and self.chat_runtime:
            result = self._local_start_pm(task.task_id, secretary_url, task.to_dict())
            if result.get("ok"):
                pm_id = result["pm_id"]
                task.pm_agent_id = pm_id
                task.status = "running"
                self.db.save_task(task)
                pm_agent = PMAgent(
                    pm_id=pm_id,
                    agent_name=f"PM-{pm_id[:8]}",
                    task_id=task.task_id,
                    device_id=self.state.device_id,
                    hostname=self.state.device_name,
                    ip="127.0.0.1",
                    api_port=self.state.api_port,
                    status="starting",
                )
                self.db.upsert_pm_agent(pm_agent)
                self._pm_worker_map[pm_id] = {
                    "ip": "127.0.0.1",
                    "api_port": self.state.api_port,
                    "device_id": self.state.device_id,
                    "local": True,
                    "task_id": task.task_id,   # iter-66 (Bug B): F3.3 迁移依赖
                }
                logger.info("PM Agent 本机启动: %s", pm_id[:12])
                # WS 广播: 通知前端 PM 面板 + 任务面板刷新
                self._queue_ws_broadcast("pm_registered", {
                    "pm_id": pm_id, "agent_name": f"PM-{pm_id[:8]}",
                    "task_id": task.task_id, "status": "starting",
                    "device_id": self.state.device_id,
                })
                self._queue_ws_broadcast("task_updated", task.to_dict())
                self.bot_gateway.notify("pm_registered", {
                    "pm_id": pm_id[:12], "task": task.name, "station": self.state.device_name,
                })
                return task.to_dict()
            else:
                logger.warning("本机 PM 启动失败: %s, 尝试远程派发", result.get('message'))

        # iter-65 (F3.4 联邦任务转发): 目标为联邦主机时经对端 Secretary 委托执行
        # (网段内主机 IP 跨网段不可达; 对端全权接管, 本侧标记 forwarded)
        if target_host and getattr(target_host, "source", "lan") == "fed":
            if fed_relay:
                # 防环: 委托任务不再回传 (跳数上限 1), 本侧无法执行直接失败
                task.status = "failed"
                task.output_data = {"error": "联邦委托已达跳数上限 (防环), 本侧暂无法执行"}
                self.db.save_task(task)
                logger.warning("联邦委托防环终止: %s (本侧仅联邦主机可达且本机 PM 忙)",
                               task.task_id)
                return task.to_dict()
            self._federation_forward_task(task, target_host)
            return task.to_dict()

        # 回退: POST 到远程 Worker 启动 PM Agent
        # 修复: 如果目标就是本机 (Tailscale IP 不可达), 直接用 127.0.0.1
        target_ip = target_host.ip
        if target_host.device_id == self.state.device_id:
            target_ip = "127.0.0.1"
        # 修复 (任务③): 远程 PM 必须回报到本机 Secretary, 不能用 127.0.0.1
        from .host_info import pick_reachable_ip
        reach_ip = pick_reachable_ip(target_ip)
        remote_secretary_url = (
            f"http://{reach_ip}:{self.state.api_port}" if reach_ip else secretary_url
        )
        try:
            resp = http_post(
                f"http://{target_ip}:{target_host.api_port}/role/start-pm",
                json={
                    "task_id": task.task_id,
                    "secretary_url": remote_secretary_url,
                    "task_data": task.to_dict(),
                },
                timeout=15,
            )
            if resp.status_code == 200:
                pm_data = resp.json()
                pm_id = pm_data.get("pm_id", "")
                task.pm_agent_id = pm_id
                task.status = "running"
                self.db.save_task(task)

                pm_agent = PMAgent(
                    pm_id=pm_id,
                    agent_name=f"PM-{pm_id[:8]}",
                    task_id=task.task_id,
                    device_id=target_host.device_id,
                    hostname=target_host.hostname or target_host.device_name,
                    ip=target_host.ip,
                    api_port=target_host.api_port,
                    status="starting",
                )
                self.db.upsert_pm_agent(pm_agent)
                # 优化7: 记录 PM→Worker 映射
                self._pm_worker_map[pm_id] = {
                    "ip": target_host.ip,
                    "api_port": target_host.api_port,
                    "device_id": target_host.device_id,
                    "task_id": task.task_id,   # iter-66 (Bug B): F3.3 迁移依赖
                }
                logger.info("PM Agent 已启动: %s → %s", pm_id[:12], target_host.device_name)
                self.bot_gateway.notify("pm_registered", {
                    "pm_id": pm_id[:12], "task": task.name,
                    "station": target_host.device_name or target_host.hostname,
                })
                # 方案C: 自动将 PM 绑定到当前活跃对话的线程
                self._auto_attach_pm_thread(pm_id, task.name, f"PM-{pm_id[:8]}")
            else:
                task.status = "failed"
                task.output_data = {"error": f"PM 启动失败: {resp.text}"}
                self.db.save_task(task)
        except Exception as e:
            task.status = "failed"
            task.output_data = {"error": f"PM 启动异常: {e}"}
            self.db.save_task(task)

        return task.to_dict()

    def _pick_task_host(self, online_hosts: list):
        """iter-65 (F3.4 联邦任务转发): 任务目标选站 — 本网段优先, 联邦兜底。

        本网段主机 (source=lan) 按评级+负载排序优先; 无本网段主机时
        退回联邦主机, 且仅限 role=secretary 的对端 (跨网段场景下只有
        peer.host 可达, 对端网段内主机 IP 不可达)。返回选中的 HostRecord。
        """
        lan_hosts = [h for h in online_hosts if getattr(h, "source", "lan") != "fed"]
        fed_hosts = [h for h in online_hosts if getattr(h, "source", "lan") == "fed"
                     and getattr(h, "role", "") == "secretary"]
        if not fed_hosts:  # 无对端 Secretary 时退化为任意 fed 主机 (尽力而为)
            fed_hosts = [h for h in online_hosts if getattr(h, "source", "lan") == "fed"]

        # 优化13: 负载感知排序 — 评级优先, 同评级选 PM 数量少的
        tier_order = {"S": 5, "A": 4, "B": 3, "C": 2, "D": 1, "": 0}
        pm_agents = self.db.list_pm_agents()
        # 统计每台主机上的活跃 PM 数量
        host_pm_count: dict[str, int] = {}
        for pm in pm_agents:
            if pm.status in ("planning", "executing", "monitoring", "awaiting_input"):
                host_pm_count[pm.device_id] = host_pm_count.get(pm.device_id, 0) + 1

        def _host_sort_key(h):
            tier = tier_order.get(h.rating_tier, 0)
            load = host_pm_count.get(h.device_id, 0)
            # 先按评级降序, 再按负载升序
            return (-tier, load)

        lan_hosts.sort(key=_host_sort_key)
        fed_hosts.sort(key=_host_sort_key)
        return (lan_hosts or fed_hosts)[0]

    def _federation_forward_task(self, task, target_host) -> bool:
        """iter-65 (F3.4 联邦任务转发): 将任务委托给对端 Secretary 网段执行。

        对端调用其本网段选站流程, 全权接管任务生命周期; 本侧标记
        forwarded (output_data 记录委托目标)。成功返回 True。
        """
        try:
            # iter-65 防环: 转发的任务数据带 _federation_relay 标记 (跳数上限 1)
            fwd_data = task.to_dict()
            fwd_input = fwd_data.setdefault("input_data", {}) or {}
            fwd_input["_federation_relay"] = True
            fwd_data["input_data"] = fwd_input
            resp = http_post(
                f"http://{target_host.ip}:{target_host.api_port}/api/federation/tasks/forward",
                json={
                    "task_data": fwd_data,
                    "forwarded_from": self.state.device_id,
                    "federation": getattr(target_host, "federation", ""),
                },
                timeout=15,
            )
            if resp.status_code == 200:
                task.status = "forwarded"
                task.output_data = {
                    "forwarded_to": target_host.device_id,
                    "federation": getattr(target_host, "federation", ""),
                }
                self.db.save_task(task)
                self._queue_ws_broadcast("task_updated", task.to_dict())
                self.bot_gateway.notify("task_forwarded", {
                    "task": task.name, "task_id": task.task_id[:8],
                    "target": target_host.device_name or target_host.hostname,
                    "federation": getattr(target_host, "federation", ""),
                })
                logger.info("联邦委托任务: %s → %s (%s)",
                            task.task_id, target_host.device_id[:8],
                            getattr(target_host, "federation", ""))
                return True
            task.status = "failed"
            task.output_data = {"error": f"联邦委托失败: {resp.text[:200]}"}
        except Exception as e:
            task.status = "failed"
            task.output_data = {"error": f"联邦委托异常: {e}"}
        self.db.save_task(task)
        return False

    # ── F3.4 (iter-64): 跨网段联邦 ────────────────────────────

    def _federation_sync_peer(self, peer) -> int:
        """同步单个联邦对端: 拉取对方与网段主机写入 DB (source=fed)。

        返回 1 成功 / 0 失败。对端自身与其网段全部主机均标记
        source=fed + federation=peer.name, 与 UDP 发现的本网段主机
        (source=lan) 完全隔离 (选举/仲裁互不干扰)。
        """
        import requests as _requests
        from .protocol import HostRecord

        port = int(peer.port or self.state.api_port)
        url = f"http://{peer.host}:{port}/api/federation/info"
        headers = {}
        if getattr(self, "_mesh_auth_enabled", False) and getattr(self, "_mesh_token", ""):
            headers["Authorization"] = f"Bearer {self._mesh_token}"
        try:
            resp = _requests.get(url, headers=headers, timeout=8)
            if resp.status_code != 200:
                return 0
            info = resp.json() or {}
        except Exception as e:
            logger.debug("[F3.4] 联邦对端 %s 拉取失败: %s", peer.name, e)
            return 0

        now = time.time()
        records: list[HostRecord] = []
        # 对端自身
        records.append(HostRecord(
            device_id=info.get("device_id", ""),
            device_name=info.get("device_name", "") or peer.name,
            role=info.get("role", "station"),
            ip=peer.host,
            api_port=port,
            online=True,
            registered_at=now,
            last_seen=now,
            code_version=info.get("code_version", ""),
            version_ts=info.get("version_ts", 0.0),
            source="fed",
            federation=peer.name,
        ))
        # 对端网段主机 (转播其本地视图, 含其自身联邦转发)
        for hd in (info.get("hosts") or []):
            if hd.get("device_id") == self.state.device_id:
                continue  # 不回写自己
            if hd.get("device_id") == info.get("device_id"):
                continue  # iter-65 修复: 对端自身已按 peer.host 入库, 防被报告 ip 覆盖
            try:
                records.append(HostRecord(
                    device_id=hd.get("device_id", ""),
                    device_name=hd.get("device_name", ""),
                    role=hd.get("role", "station"),
                    hostname=hd.get("hostname", ""),
                    platform=hd.get("platform", ""),
                    ip=hd.get("ip", ""),
                    api_port=hd.get("api_port", 0),
                    cpu_count=hd.get("cpu_count", 0),
                    memory_total_mb=hd.get("memory_total_mb", 0),
                    disk_total_gb=hd.get("disk_total_gb", 0),
                    cpu_percent=hd.get("cpu_percent", 0.0),
                    memory_percent=hd.get("memory_percent", 0.0),
                    disk_percent=hd.get("disk_percent", 0.0),
                    shared_folder=hd.get("shared_folder", ""),
                    online=bool(hd.get("online", True)),
                    registered_at=now,
                    last_seen=now,
                    rating_tier=hd.get("rating_tier", ""),
                    rating_score=hd.get("rating_score", 0),
                    rating_summary=hd.get("rating_summary", ""),
                    code_version=hd.get("code_version", ""),
                    version_ts=hd.get("version_ts", 0.0),
                    source="fed",
                    federation=peer.name,
                ))
            except Exception:
                continue
        for rec in records:
            if not rec.device_id:
                continue
            try:
                self.db.upsert_host(rec)
            except Exception as e:
                logger.warning("[F3.4] 联邦主机入库失败 %s: %s", rec.device_id, e)
        return 1

    def _federation_loop(self):
        """F3.4 (iter-64): 跨网段联邦轮询线程。

        周期拉取全部配置对端; 连续 offline_after 次失败将该联邦
        主机集合置离线。配置变更需重启生效 (静态联邦 v1)。
        """
        cfg = self.cfg.federation
        failures: dict[str, int] = {}
        logger.info("[F3.4] 联邦轮询启动: %d 个对端, 间隔 %ds",
                    len(cfg.peers), cfg.interval)
        while self._running:
            for peer in cfg.peers:
                ok = self._federation_sync_peer(peer)
                failures[peer.name] = 0 if ok else failures.get(peer.name, 0) + 1
                if failures[peer.name] >= cfg.offline_after:
                    try:
                        for h in self.db.list_hosts(source="fed"):
                            if h.federation == peer.name and h.online:
                                self.db.set_offline(h.device_id)
                    except Exception:
                        pass
                    logger.warning("[F3.4] 联邦对端 %s 连续 %d 次不可达, 已标记离线",
                                   peer.name, failures[peer.name])
            try:
                time.sleep(max(1, int(cfg.interval)))
            except Exception:
                return

    # ── F3.1: Worker 自动扩缩容 ─────────────────────────────────

    def _start_autoscaler(self):
        """F3.1: 启动自动扩缩容监控线程。"""
        t = threading.Thread(target=self._autoscaler_loop, daemon=True, name="autoscaler")
        t.start()
        logger.info("自动扩缩容监控已启动 (interval=30s, scale_up=%d, scale_down=%d)",
                   self._autoscale_up_threshold, self._autoscale_down_threshold)

    def _autoscaler_loop(self):
        """F3.1: 定期检测任务队列深度, 触发扩缩容决策。"""
        while self._running:
            time.sleep(30)  # 每 30 秒检测一次
            try:
                self._autoscale_check()
            except Exception as e:
                logger.debug("自动扩缩容检测异常: %s", e)

    def _autoscale_check(self):
        """F3.1: 扩缩容决策逻辑。

        策略:
        - 队列深度 > up_threshold → 尝试激活空闲 Worker
        - 队列深度 < down_threshold 且无活跃 PM → 允许 Worker 进入低功耗
        """
        # 统计任务队列
        all_tasks = self.db.list_tasks()
        pending = [t for t in all_tasks if getattr(t, 'status', '') in ('pending', 'queued')]
        running = [t for t in all_tasks if getattr(t, 'status', '') in ('running', 'monitoring')]
        queue_depth = len(pending)
        active_count = len(running)

        # 获取在线 Worker 列表
        hosts = self.db.list_hosts()
        online_workers = [h for h in hosts
                         if getattr(h, 'online', False)
                         and getattr(h, 'role', '') == 'worker']
        idle_workers = [h for h in online_workers
                       if not self._is_worker_busy(h)]

        # 扩容: 队列积压且有可用 Worker (iter-67 Bug J: 门槛 >= 1)
        # iter-68: 同轮连续派发清空积压 — 原 30s/轮 × N 积压滞后严重
        # (五节点实测 4 积压需 120s); 每次派发后重查队列与空闲 Worker,
        # 派发失败立即停止本轮防死循环 (30s 后下轮再试)
        while queue_depth >= 1 and idle_workers:
            target = idle_workers[0]
            logger.info("[自动扩容] 队列=%d, 激活 Worker: %s",
                       queue_depth, getattr(target, 'device_name', ''))
            ok = self._dispatch_next_task_to_worker(target)
            new_depth = len([t for t in self.db.list_tasks()
                             if getattr(t, 'status', '')
                             in ('pending', 'queued')])
            if not ok or new_depth >= queue_depth:
                # 派发失败或队列未减: 停止本轮 (防死循环)
                break
            queue_depth = new_depth
            hosts = self.db.list_hosts()
            online_workers = [h for h in hosts
                              if getattr(h, 'online', False)
                              and getattr(h, 'role', '') == 'worker']
            idle_workers = [h for h in online_workers
                            if not self._is_worker_busy(h)]

        # 缩容日志 (仅记录, 不主动关闭 Worker)
        if queue_depth == 0 and active_count == 0 and online_workers:
            logger.debug("[缩容观察] 无活跃任务, %d 台 Worker 空闲", len(online_workers))

    def _is_worker_busy(self, host) -> bool:
        """F3.1: 检查 Worker 是否正在执行任务。"""
        device_id = getattr(host, 'device_id', '')
        # 检查是否有活跃 PM 在该 Worker 上
        for pm_info in self._pm_worker_map.values():
            if pm_info.get("device_id") == device_id:
                return True
        return False

    def _dispatch_task_to_worker(self, task, worker_host) -> bool:
        """iter-66 (F3.1/F3.3 修复): 将指定任务派发到指定 Worker 并更新任务状态。

        成功: 置 running + 落库 + 记录 PM→Worker 映射 (含 task_id, F3.3 迁移依赖);
        失败: 返回 False (任务状态由调用方决定, 避免误置 failed 影响迁移重试)。
        """
        task_id = getattr(task, 'task_id', '')
        ip = getattr(worker_host, 'ip', '')
        port = getattr(worker_host, 'api_port', 0)

        if not ip or not port:
            return False

        # 修复 (任务③): 远程 PM 回报地址用本机对目标可达的 IP, 非 127.0.0.1
        from .host_info import pick_reachable_ip
        reach_ip = pick_reachable_ip(ip)
        sec_url = (
            f"http://{reach_ip}:{self.state.api_port}"
            if reach_ip else f"http://127.0.0.1:{self.state.api_port}"
        )
        try:
            # iter-66 (Bug E): 节点间认证启用时 Worker 端 /role/start-pm
            # 要求 Bearer mesh_token, 不带认证头会 401 静默失败
            from .http_retry import auth_headers
            resp = http_post(
                f"http://{ip}:{port}/role/start-pm",
                json={"task_id": task_id, "secretary_url": sec_url,
                      # iter-66 (Bug F): 任务仅存于 Secretary DB, Worker 端
                      # 无法从本地 DB 获取 → 缺 task_data 时 409「无法获取任务详情」
                      "task_data": task.to_dict()},
                headers=auth_headers(),
                timeout=10,
            )
            if resp.status_code == 200:
                pm_data = resp.json()
                pm_id = pm_data.get("pm_id", "")
                task.pm_agent_id = pm_id
                task.status = "running"
                self.db.save_task(task)
                if pm_id:
                    # iter-66 修复 (Bug B): 补 task_id 键, F3.3 迁移依赖
                    self._pm_worker_map[pm_id] = {
                        "ip": ip,
                        "api_port": port,
                        "device_id": getattr(worker_host, 'device_id', ''),
                        "task_id": task_id,
                    }
                    # iter-67 (Bug K): 与其余 5 处派发路径对齐, 落 pm_agents 表
                    # (运维查询任务承载 + 脚本 victim 定位依赖该表)
                    from .protocol import PMAgent
                    self.db.upsert_pm_agent(PMAgent(
                        pm_id=pm_id,
                        agent_name=f"PM-{pm_id[:8]}",
                        task_id=task_id,
                        project_id=getattr(task, 'project_id', '') or '',
                        device_id=getattr(worker_host, 'device_id', ''),
                        hostname=getattr(worker_host, 'device_name', ''),
                        ip=ip,
                        api_port=port,
                        status="starting",
                    ))
                logger.info("[自动扩容] 任务 %s 已派发到 %s", task_id[:8], ip)
                return True
            else:
                # iter-66: 非 200 明确记录状态码, 便于定位 401/404/500
                logger.warning("[自动扩容] 派发被拒: %s → %s:%s/role/start-pm "
                               "HTTP %d (%s)", task_id[:8], ip, port,
                               resp.status_code, resp.text[:120])
        except Exception as e:
            logger.warning("[自动扩容] 派发失败: %s", e)
        return False

    def _next_pending_task(self):
        """iter-66 (Bug D 修复): 取队列中最早提交的 pending 任务 (FIFO)。

        list_tasks 默认 ORDER BY created_at DESC, 直接取 [0] 是 LIFO,
        持续有新任务提交时旧任务会饥饿; 此处显式重排取最早任务。
        """
        all_tasks = self.db.list_tasks()
        pending = [t for t in all_tasks if getattr(t, 'status', '') == 'pending']
        if not pending:
            return None
        return min(pending, key=lambda t: getattr(t, 'created_at', 0) or 0)

    def _dispatch_next_task_to_worker(self, worker_host) -> bool:
        """F3.1: 将队列中最早 pending 任务派发到指定 Worker。

        Returns:
            True = 派发成功 (iter-68 起供扩容批量清空循环判定)
        """
        task = self._next_pending_task()
        if not task:
            return False
        return self._dispatch_task_to_worker(task, worker_host)
