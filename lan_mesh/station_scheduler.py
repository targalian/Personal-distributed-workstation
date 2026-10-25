"""
任务提交与调度域 — 负载选站、联邦转发轮询、自动扩容派发。

拆分自 station_controller.py (iter-75 Phase 3)。任务从 Boss/秘书进入系统后的
去向决策都在这里: 先按负载与技能选站 (本机/远端 Worker/跨网段联邦), 再由
F3.1 自动扩缩容守护持续把积压队列派发给空闲 Worker (单轮批量清空)。
iter-105：提交与实际派发委托 TaskSchedulingService；此处保留选站、联邦传输与守护兼容入口。
"""
import threading
from .application_lifecycle import wait_for_stop
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
                              fed_relay: bool = False,
                              input_data: dict | None = None,
                              project_id: str = "") -> dict:
        """从对话或联邦兼容入口，经 Yomk 统一提交任务并保留项目与优先级。"""
        from .task_scheduling_service import call_schedule
        return call_schedule(self, "submit_task", payload={
            "name": name, "description": description, "created_by": created_by,
            "priority": priority, "fed_relay": fed_relay,
            "input_data": input_data or {}, "project_id": project_id,
        })

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
                if wait_for_stop(self, max(1, int(cfg.interval))):
                    return
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
            if wait_for_stop(self, 30):
                return  # 每 30 秒检测一次
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
        """Compatibility entry for autoscaling and orphan migration dispatch."""
        from .task_scheduling_service import call_schedule
        return call_schedule(self, "dispatch_to_worker", task=task, worker_host=worker_host)

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
