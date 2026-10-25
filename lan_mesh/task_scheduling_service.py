"""Task scheduling service shared by HTTP, chat, federation, queues and autoscaling."""

import threading
import uuid as _uuid
from typing import Any

from ._vendor.yomkserverpy.YomkServerPy import YomkService
from .logger import get_logger
from .protocol import Task
from .http_retry import auth_headers
from .service_runtime import ServiceError, get_service_runtime

logger = get_logger("station")


class TaskSchedulingService(YomkService):
    """Own task creation and dispatch decisions; adapters share this implementation."""

    def __init__(self, server, controller: object):
        super().__init__(server)
        self.set_name("/TaskSchedulingService")
        self.controller = controller
        self._relay_lock = threading.RLock()
        self._claims_lock = threading.RLock()
        self._claims = set()
        self._stopped = threading.Event()

    def deinit(self) -> None:
        """Stop queue waiters when this application runtime is closed."""
        self._stopped.set()

    def _emit(self, state: object, event_type: str, data: dict) -> None:
        sink = getattr(self.controller, "_queue_ws_broadcast", None)
        if callable(sink):
            sink(event_type, data)

    def submit_task(self, payload: dict, check_secretary: bool = False) -> dict:
        """提交新任务 — 创建 Task 记录并分配 PM Agent。

        新流程 (PM Agent 架构):
        1. 创建 Task 记录 (status=pending, pm_agent_id="")
        2. 选择合适 work_station (按评级/在线状态)
        3. POST 到目标 Worker /role/start-pm (携带 task_id, secretary_url)
        4. 更新 Task.pm_agent_id, status=running
        5. WebSocket 广播 task_submitted + pm_registered
        """
        if check_secretary and not self.controller.secretary_active:
            raise ServiceError(503, "Secretary 未激活,请在 Station 面板中点击「启动秘书」")
        task = self._create_task(payload)
        self._estimate_cost(task)
        self._trace_submission(task)
        hosts = self._select_hosts()
        if not hosts:
            task.status = "failed"
            task.output_data = {"error": "无可用 work_station"}
            self.controller.db.save_task(task)
            print(f"[Task] 任务失败: {task.task_id}")
            return task.to_dict()
        local_result = self._try_local(task, hosts)
        if local_result is not None:
            return local_result
        target = self._pick_host(hosts)
        if getattr(target, "source", "lan") == "fed":
            if (task.input_data or {}).get("_federation_relay"):
                task.status = "failed"
                task.output_data = {"error": "联邦委托已达跳数上限 (防环), 本侧暂无法执行"}
                self.controller.db.save_task(task)
                return task.to_dict()
            self.controller._federation_forward_task(task, target)
            return task.to_dict()
        return self._start_remote(task, target)


    def _create_task(self, payload: dict) -> Task:
        controller = self.controller
        db = controller.db
        state = controller.state
        project_manager = controller.project_manager
        project_id = payload.get("project_id", "")
        if project_id and project_manager:
            if not project_manager.check_budget(project_id):
                raise ServiceError(status_code=402, detail="项目预算已起支或已暂停,无法提交任务")

        # 1. 创建 Task 记录
        task = Task(
            task_id=f"task-{_uuid.uuid4().hex[:12]}",
            name=payload.get("name", ""),
            description=payload.get("description", ""),
            input_data=payload.get("input_data", {}),
            created_by=payload.get("created_by", "user"),
            project_id=project_id,
            status="pending",
        )
        task.input_data = dict(task.input_data or {})
        if "priority" in payload:
            task.input_data.setdefault("_priority", payload["priority"])
        if payload.get("fed_relay"):
            task.input_data["_federation_relay"] = True
        db.save_task(task)
        print(f"[Task] 任务已创建: {task.task_id}")
        self._emit(state, "task_submitted", task.to_dict())
        controller.bot_gateway.notify("task_submitted", {
            "name": task.name, "task_id": task.task_id[:8],
        })
        return task


    def _estimate_cost(self, task: Task) -> None:
        controller = self.controller
        db = controller.db
        state = controller.state
        project_id = task.project_id
        project_manager = controller.project_manager
        # F4.4 (iter-52): 成本感知调度 — 预算预估 + 适配检查 (异常静默, 不阻断)
        try:
            from .budget_advisor import build_task_cost_estimate
            cost_est = build_task_cost_estimate(
                task.name, task.description, db,
                project_id=project_id,
                project_manager=project_manager)
            task.input_data = task.input_data or {}
            task.input_data["_cost_estimate"] = cost_est
            db.save_task(task)
            fit = cost_est.get("budget_fit", {})
            if fit.get("status") in ("tight", "insufficient"):
                controller.bot_gateway.notify("cost_budget_warning", {
                    "name": task.name, "task_id": task.task_id[:8],
                    "estimated": cost_est["estimated_tokens"],
                    "status": fit.get("label", fit.get("status", "")),
                    "advice": fit.get("advice", ""),
                })
                self._emit(state, "cost_budget_warning", {
                    "task_id": task.task_id, "name": task.name,
                    "estimated_tokens": cost_est["estimated_tokens"],
                    "status": fit.get("status", "unknown"),
                    "advice": fit.get("advice", ""),
                })
        except Exception as exc:
            logger.warning("[Task] 预算预估失败 (任务继续): %s", exc)


    def _trace_submission(self, task: Task) -> None:
        # P3: 任务流追踪 — 提交阶段点 (异常静默)
        try:
            from . import runtime_trace
            runtime_trace.trace_task_event(
                task.task_id, "submitted",
                detail=f"{task.name} (created_by={task.created_by})")
        except Exception as exc:
            logger.warning("[Task] 提交追踪失败 (任务继续): %s", exc)


    def _select_hosts(self) -> list:
        controller = self.controller
        db = controller.db
        state = controller.state
        # 2. 选择合适的 work_station (按评级排序, 取在线最高评级)
        hosts = db.list_hosts()
        online_hosts = [h for h in hosts if h.online and h.device_id != state.device_id]
        # 如果没有在线 Worker, 使用本机
        if not online_hosts:
            online_hosts = [h for h in hosts if h.online]

        tier_order = {"S": 5, "A": 4, "B": 3, "C": 2, "D": 1, "": 0}
        return sorted(online_hosts, key=lambda h: tier_order.get(h.rating_tier, 0), reverse=True)


    def _try_local(self, task: Task, online_hosts: list) -> dict | None:
        controller = self.controller
        if controller.secretary_active and controller.chat_runtime:
            if self.dispatch_local(task):
                return task.to_dict()
            if not any(h.device_id != controller.state.device_id for h in online_hosts):
                self._emit(controller.state, "task_updated", task.to_dict())
                return {**task.to_dict(), "queued": True,
                        "message": "本机 PM 忙, 任务已排队等待派发"}
        return None


    def _start_remote(self, task: Task, target_host: object) -> dict:
        result = self._dispatch_remote(task, target_host)
        if result.get("busy"):
            return {**task.to_dict(), "queued": True, "message": "任务正在派发"}
        if not result["ok"] and task.status not in ("running", "cancelled"):
            task.status = "failed"
            task.output_data = {"error": result["error"]}
            self.controller.db.save_task(task)
            print(f"[Task] 任务失败: {task.task_id}")
        self._emit(self.controller.state, "task_updated", task.to_dict())
        return task.to_dict()


    def _register_remote(self, task: Task, target_host: object, target_ip: str, pm_id: str) -> None:
        controller = self.controller
        db = controller.db
        state = controller.state
        from .protocol import PMAgent
        project_id = task.project_id
        task.pm_agent_id = pm_id
        latest = db.get_task(task.task_id)
        task.status = "cancelled" if latest and latest.status == "cancelled" else "running"
        db.save_task(task)

        pm_agent = PMAgent(
            pm_id=pm_id,
            agent_name=f"PM-{pm_id[:8]}",
            task_id=task.task_id,
            project_id=project_id,
            device_id=target_host.device_id,
            hostname=getattr(target_host, "hostname", "") or target_host.device_name,
            ip=target_ip,
            api_port=target_host.api_port,
            status="starting",
        )
        db.upsert_pm_agent(pm_agent)
        controller._pm_worker_map[pm_id] = {
            "ip": target_ip,
            "api_port": target_host.api_port,
            "device_id": target_host.device_id,
            # iter-66 (Bug B 补): F3.3 迁移依赖 task_id 定位任务
            "task_id": task.task_id,
        }

        self._emit(state, "pm_registered", {
            "pm_id": pm_id,
            "task_id": task.task_id,
            "device_id": target_host.device_id,
            "device_name": target_host.device_name,
        })
        bot = getattr(controller, "bot_gateway", None)
        if bot:
            bot.notify("pm_registered", {
                "pm_id": pm_id[:12], "task": task.name,
                "station": target_host.device_name or getattr(target_host, "hostname", ""),
            })
        if task.status == "cancelled":
            from .pm_control_service import call_pm_control
            call_pm_control(controller, "cancel_task", task_id=task.task_id)


    def _pick_host(self, hosts: list) -> object:
        selector = getattr(self.controller, "_pick_task_host", None)
        if callable(selector):
            return selector(hosts)
        from .station_scheduler import StationSchedulerMixin
        return StationSchedulerMixin._pick_task_host(self.controller, hosts)

    def relay(self) -> bool:
        """iter-57 (补强#5): PM 空闲后接力派发队列中最早 pending 任务。

        并发提交时本机 PM 忙 → 任务排队 (pending); 本机 PM 完成后调用
        本方法接力派发。若 PM 仍在收尾 (completed 上报时线程尚未退出),
        后台线程等待其结束后再派发, 避免请求线程内自调用阻塞
        (压测发现 20 并发提交时 19 个任务瞬时 failed + ReadTimeout)。
        """
        if self._stopped.is_set():
            return False
        with self._relay_lock:
            return self._relay_locked()

    def _relay_locked(self) -> bool:
        import threading
        import time
        from .pm_lifecycle_service import get_pm_service
        owner = get_pm_service(self.controller)
        with owner.lock:
            busy = owner._busy()
        if busy:
            if not getattr(self.controller, '_queued_dispatch_waiting', False):
                self.controller._queued_dispatch_waiting = True

                def wait_then_dispatch() -> None:
                    try:
                        deadline = time.monotonic() + 120
                        while time.monotonic() < deadline:
                            if self._stopped.wait(2):
                                return
                            with owner.lock:
                                idle = not owner._busy()
                            if idle:
                                self.relay()
                                return
                    finally:
                        self.controller._queued_dispatch_waiting = False

                threading.Thread(target=wait_then_dispatch,
                                 daemon=True, name="queued-dispatch").start()
            return False
        pending = [t for t in self.controller.db.list_tasks() if t.status == "pending"]
        if not pending:
            return False
        task = min(pending, key=lambda t: getattr(t, 'created_at', 0) or 0)
        return self.dispatch_local(task)


    def dispatch_to_worker(self, task: Task, worker_host: object) -> bool:
        """Shared remote dispatch for submissions, autoscaling and orphan migration."""
        return self._dispatch_remote(task, worker_host)["ok"]


    def dispatch_local(self, task: Task) -> bool:
        """Serialize claiming, PM creation and registration across every local caller."""
        with self._claims_lock:
            if task.task_id in self._claims:
                return False
            self._claims.add(task.task_id)
        try:
            return self._dispatch_local_claimed(task)
        except Exception as exc:
            logger.warning("[Station] 本机派发失败: %s", exc)
            return False
        finally:
            with self._claims_lock:
                self._claims.discard(task.task_id)

    def _dispatch_local_claimed(self, task: Task) -> bool:
        from .pm_lifecycle_service import call_pm, get_pm_service
        owner = get_pm_service(self.controller)
        with self._relay_lock, owner.lock:
            latest = self.controller.db.get_task(task.task_id)
            if latest and latest.status not in ("pending", "queued"):
                return False
            result = self.controller._local_start_pm(
                task.task_id, f"http://127.0.0.1:{self.controller.state.api_port}", task.to_dict())
            if not result.get("ok"):
                return False
            task.pm_agent_id, task.status = result["pm_id"], "running"
            latest = self.controller.db.get_task(task.task_id)
            if latest and latest.status == "cancelled":
                task.status = "cancelled"
            else:
                self.controller.db.save_task(task)
                call_pm(self.controller, "register_local", pm_id=result["pm_id"], task=task)
            bot = getattr(self.controller, "bot_gateway", None)
            if bot:
                bot.notify("pm_registered", {"pm_id": result["pm_id"][:12],
                           "task": task.name, "station": self.controller.state.device_name})
        if task.status == "cancelled":
            call_pm(self.controller, "cancel", expected_pm_id=result["pm_id"])
        return True

    def _dispatch_remote(self, task: Task, host: object) -> dict:
        from .station_scheduler import http_post
        from .host_info import pick_reachable_ip
        ip, port = getattr(host, "ip", ""), getattr(host, "api_port", 0)
        if not ip or not port:
            return {"ok": False, "error": "Worker 地址信息不完整"}
        with self._claims_lock:
            if task.task_id in self._claims:
                return {"ok": False, "busy": True, "error": "任务正在派发"}
            latest = self.controller.db.get_task(task.task_id)
            if latest and latest.status not in ("pending", "queued"):
                task.status, task.pm_agent_id = latest.status, latest.pm_agent_id
                return {"ok": False, "error": "任务已离开等待队列"}
            self._claims.add(task.task_id)
        try:
            ip = "127.0.0.1" if host.device_id == self.controller.state.device_id else ip
            callback_ip = pick_reachable_ip(ip) or "127.0.0.1"
            response = http_post(
                f"http://{ip}:{port}/role/start-pm", headers=auth_headers(),
                json={"task_id": task.task_id, "task_data": task.to_dict(),
                      "secretary_url": f"http://{callback_ip}:{self.controller.state.api_port}"},
                timeout=10,
            )
            if response.status_code != 200:
                return {"ok": False, "error": f"PM 启动失败: {response.text[:200]}"}
            pm_id = response.json().get("pm_id", "")
            if not pm_id:
                return {"ok": False, "error": "Worker 未返回 pm_id"}
            self._register_remote(task, host, ip, pm_id)
            attach = getattr(self.controller, "_auto_attach_pm_thread", None)
            if callable(attach):
                attach(pm_id, task.name, f"PM-{pm_id[:8]}")
            return {"ok": True}
        except Exception as exc:
            logger.warning("[Station] 派发失败: %s", exc)
            return {"ok": False, "error": f"PM 启动异常: {exc}"}
        finally:
            with self._claims_lock:
                self._claims.discard(task.task_id)


def get_scheduling_service(controller: object) -> TaskSchedulingService:
    """Compose once for synchronous callers and FastAPI adapters alike."""
    runtime = get_service_runtime(controller)
    return runtime.ensure("/TaskSchedulingService",
                          lambda server: TaskSchedulingService(server, controller),
                          ["submit_task", "relay", "dispatch_to_worker", "dispatch_local"])


def call_schedule(controller: object, operation: str, **arguments: Any) -> dict | bool:
    """Route existing controller entry points through Yomk."""
    get_scheduling_service(controller)
    return get_service_runtime(controller).request_sync(
        f"/TaskSchedulingService/{operation}", **arguments)
