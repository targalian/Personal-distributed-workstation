"""Yomk PM lifecycle service: one serialized local owner per Station."""

import threading
from functools import wraps
from typing import Any

from ._vendor.yomkserverpy.YomkServerPy import YomkService
from .logger import get_logger
from .service_runtime import get_service_runtime
from .protocol import Task

logger = get_logger("station")


class _LaunchFailure(Exception):
    def __init__(self, agent, cause: Exception):
        super().__init__(str(cause))
        self.agent = agent


def _serialized(method):
    @wraps(method)
    def invoke(self, *args: Any, **kwargs: Any) -> Any:
        with self.lock:
            return method(self, *args, **kwargs)
    return invoke


class PmLifecycleService(YomkService):
    """Own lifecycle transitions; controller fields remain compatibility aliases."""

    def __init__(self, server, controller: object):
        super().__init__(server)
        self.set_name("/PmLifecycleService")
        self.controller = controller
        self.lock = threading.RLock()
        self._retiring = []
        self._transitioning = set()
        self._owner_task_id = ""
        self._owner_pm_id = ""

    def start(self, task_id: str, secretary_url: str, task_data: dict = None) -> dict:
        """Start atomically; failed partial launches are cancelled outside the lock."""
        return self._launch(self._start, task_id, secretary_url, task_data)

    def resume(self, task_id: str) -> dict:
        """Resume through the same slot and failure cleanup as new starts."""
        return self._launch(self._resume, task_id)

    def _launch(self, operation, *args) -> dict:
        try:
            return operation(*args)
        except _LaunchFailure as exc:
            try:
                cancel = getattr(exc.agent, "cancel", None)
                if callable(cancel):
                    cancel()
            except Exception as cleanup_error:
                logger.error("[PM] 启动失败后的清理失败: %s", cleanup_error)
            finally:
                with self.lock:
                    self._transitioning.discard(id(exc.agent))
            print(f"[PM] 本机 PM 启动失败: {exc}")
            return {"ok": False, "message": str(exc)}
        except Exception as exc:
            print(f"[PM] 本机 PM 启动失败: {exc}")
            return {"ok": False, "message": str(exc)}

    def _failed_launch(self, agent, exc: Exception) -> None:
        self.controller._local_pm_agent = None
        self._retiring.append(agent)
        self._transitioning.add(id(agent))
        raise _LaunchFailure(agent, exc)

    def _busy(self) -> bool:
        self._retiring = [pm for pm in self._retiring if self._alive(pm)]
        pm = getattr(self.controller, "_local_pm_agent", None)
        return bool(self._transitioning or self._retiring or (pm and self._alive(pm)))

    @staticmethod
    def _alive(pm) -> bool:
        return bool(getattr(pm, "_running", False) or any(
            getattr(pm, name, None) and getattr(pm, name).is_alive()
            for name in ("_thread", "_progress_thread")))

    def _emit(self, event_type: str, data: dict) -> None:
        sink = getattr(self.controller, "_queue_ws_broadcast", None)
        if callable(sink):
            sink(event_type, data)

    @_serialized
    def _start(self, task_id: str, secretary_url: str, task_data: dict = None) -> dict:
        """在本机 Station 进程内直接启动 PM Agent (无需 Worker)。"""
        current = getattr(self.controller, "_local_pm_agent", None)
        if (current and not self._transitioning and getattr(current, "_running", False)
                and self._owner_task_id == task_id and self._owner_pm_id == current.pm_id):
            return {"ok": True, "pm_id": current.pm_id,
                    "device_id": self.controller.state.device_id}
        if self._busy():
            return {"ok": False, "message": "本机 PM Agent 已在运行"}
        if not task_data:
            return {"ok": False, "message": "缺少任务数据"}
        # iter-55: 让位主机 (网络中已有 Secretary, 本机未激活) 也可执行
        # 远程派发的 PM 任务 — 惰性初始化专用 AgentRuntime, 与激活解耦
        if not self.controller.chat_runtime:
            from .agent_runtime import AgentRuntime
            self.controller.chat_runtime = AgentRuntime(
                agent_id=f"worker-{self.controller.state.device_id[:8]}",
                shared_folder_path=str(self.controller.state.shared_folder.path),
            )
            logger.info("让位主机惰性初始化 Worker AgentRuntime (%s)",
                        self.controller.state.device_name)

        import uuid as _uuid
        from .pm_agent import ProjectManagerAgent
        pm_id = f"pm-{_uuid.uuid4().hex[:12]}"

        self.controller._local_pm_agent = ProjectManagerAgent(
            pm_id=pm_id,
            agent_runtime=self.controller.chat_runtime,
            secretary_url=secretary_url,
            device_id=self.controller.state.device_id,
            device_name=self.controller.state.device_name,
        )
        candidate = self.controller._local_pm_agent
        self._owner_task_id, self._owner_pm_id = task_id, pm_id
        try:
            candidate.start_task(task_data)
        except Exception as exc:
            self._failed_launch(candidate, exc)
        logger.info("本机 PM Agent 已启动: %s, 任务: %s", pm_id, task_id)
        # 方案C: 自动将 PM 绑定到当前活跃对话的线程
        task_name = task_data.get("name", task_id)
        self.controller._auto_attach_pm_thread(pm_id, task_name, f"PM-{pm_id[:8]}")
        return {"ok": True, "pm_id": pm_id, "device_id": self.controller.state.device_id}


    @_serialized
    def register_local(self, pm_id: str, task: Task) -> None:
        """本机 PM 启动后的统一登记 (落库 + 映射 + WS 广播)。

        iter-69 (Bug L 修复): 接力派发与 F3.3 本机接管共用此登记, 避免
        接管路径漏登记导致 PM 查不到、无法取消、二次迁移失联。
        """
        from .protocol import PMAgent
        self.controller.db.upsert_pm_agent(PMAgent(
            pm_id=pm_id,
            agent_name=f"PM-{pm_id[:8]}",
            task_id=task.task_id,
            project_id=getattr(task, 'project_id', '') or '',
            device_id=self.controller.state.device_id,
            hostname=self.controller.state.device_name,
            ip="127.0.0.1",
            api_port=self.controller.state.api_port,
            status="starting",
        ))
        self.controller._pm_worker_map[pm_id] = {
            "ip": "127.0.0.1",
            "api_port": self.controller.state.api_port,
            "device_id": self.controller.state.device_id,
            "local": True,
            "task_id": task.task_id,   # iter-66 (Bug B): F3.3 迁移依赖
        }
        self._emit("pm_registered", {
            "pm_id": pm_id, "task_id": task.task_id,
            "device_id": self.controller.state.device_id,
            "device_name": self.controller.state.device_name,
        })
        self._emit("task_updated", task.to_dict())


    def stop(self, expected_pm_id: str = "") -> dict:
        """Stop the selected owner without cancelling a newer PM instance."""
        return self._transition("cancel", expected_pm_id, clear_subagents=True)


    @_serialized
    def _resume(self, task_id: str) -> dict:
        """iter-53: 从执行态快照恢复中断的 PM Agent (断点续跑)。

        重建 PM Agent 实例并注入快照状态, 保留已完成子任务输出,
        重新分发未完成部分; 任务状态重置为 running。
        """
        if self._busy():
            return {"ok": False, "message": "本机 PM Agent 正在运行, 无法恢复其他任务"}
        # iter-55: 与 _local_start_pm 一致 — 让位主机惰性初始化 runtime
        if not self.controller.chat_runtime:
            from .agent_runtime import AgentRuntime
            self.controller.chat_runtime = AgentRuntime(
                agent_id=f"worker-{self.controller.state.device_id[:8]}",
                shared_folder_path=str(self.controller.state.shared_folder.path),
            )

        snapshot = self.controller.db.get_pm_snapshot_by_task(task_id)
        if not snapshot:
            return {"ok": False, "message": "无执行态快照, 无法恢复"}

        task = self.controller.db.get_task(task_id)
        if not task:
            return {"ok": False, "message": "任务不存在"}

        from .pm_agent import ProjectManagerAgent
        pm_id = snapshot["pm_id"]
        secretary_url = f"http://127.0.0.1:{self.controller.state.api_port}"

        self.controller._local_pm_agent = ProjectManagerAgent(
            pm_id=pm_id,
            agent_runtime=self.controller.chat_runtime,
            secretary_url=secretary_url,
            device_id=self.controller.state.device_id,
            device_name=self.controller.state.device_name,
        )
        candidate = self.controller._local_pm_agent
        try:
            if not candidate.resume_from_snapshot(snapshot):
                self._failed_launch(candidate, ValueError("快照解析失败, 无法恢复"))
        except _LaunchFailure:
            raise
        except Exception as exc:
            self._failed_launch(candidate, exc)
        self._owner_task_id, self._owner_pm_id = task_id, pm_id

        # 任务状态重置为 running (恢复线程内会继续上报进度)
        task.status = "running"
        task.pm_agent_id = pm_id
        self.controller.db.save_task(task)
        self.register_local(pm_id, task)
        logger.info("本机 PM Agent 已从快照恢复: %s, 任务: %s", pm_id, task_id)
        return {"ok": True, "pm_id": pm_id}


    def cancel(self, expected_pm_id: str = "") -> dict:
        """Release compatibility ownership while tracking the retiring threads."""
        return self._transition("cancel", expected_pm_id)


    def pause(self, expected_pm_id: str = "") -> dict:
        """Pause only the requested owner and retain its snapshot."""
        return self._transition("pause", expected_pm_id)


    def _transition(self, action: str, expected_pm_id: str,
                    clear_subagents: bool = False) -> dict:
        # PM.cancel/pause make synchronous HTTP callbacks. Never hold ownership
        # locks across them: callbacks may ask the scheduler to launch the next PM.
        with self.lock:
            agent = getattr(self.controller, "_local_pm_agent", None)
            if agent is None:
                return {"ok": False, "code": "not_running", "message": "PM Agent 未运行"}
            if expected_pm_id and agent.pm_id != expected_pm_id:
                return {"ok": False, "code": "owner_changed", "message": "PM 实例已变更，拒绝控制其他任务"}
            token = id(agent)
            if token in self._transitioning:
                return {"ok": False, "message": "PM 正在处理生命周期操作"}
            self._transitioning.add(token)
            if action == "cancel":
                self._retiring.append(agent)
                self.controller._local_pm_agent = None
        try:
            getattr(agent, action)()
            if clear_subagents:
                with self.lock:
                    self.controller._local_sub_agents.clear()
            print(f"[PM] 本机 PM {action}: {agent.pm_id}")
            return {"ok": True, "pm_id": agent.pm_id}
        except Exception as exc:
            with self.lock:
                if self.controller._local_pm_agent is None:
                    self.controller._local_pm_agent = agent
            print(f"[PM] 本机 PM {action} 失败: {agent.pm_id}: {exc}")
            return {"ok": False, "message": str(exc)}
        finally:
            with self.lock:
                self._transitioning.discard(token)

    @_serialized
    def inject_input(self, input_data: dict, expected_pm_id: str = "") -> dict:
        if not self.controller._local_pm_agent or not self.controller._local_pm_agent._running:
            return {"ok": False, "message": "PM Agent 未运行"}
        if expected_pm_id and self.controller._local_pm_agent.pm_id != expected_pm_id:
            return {"ok": False, "message": "PM 实例已变更，拒绝注入其他任务"}
        self.controller._local_pm_agent.receive_input(input_data)
        return {"ok": True, "pm_id": self.controller._local_pm_agent.pm_id}


    @_serialized
    def status(self) -> dict:
        if not getattr(self.controller, "_local_pm_agent", None):
            return {"running": False}
        return self.controller._local_pm_agent.get_status()



def get_pm_service(controller: object) -> PmLifecycleService:
    """Compose one owner service even for Worker calls before router assembly."""
    runtime = get_service_runtime(controller)
    return runtime.ensure("/PmLifecycleService",
                          lambda server: PmLifecycleService(server, controller),
                          ["start", "resume", "stop", "cancel", "pause", "inject_input", "status", "register_local"])


def call_pm(controller: object, operation: str, **arguments: Any) -> Any:
    """Use the same request boundary from controller compatibility methods."""
    get_pm_service(controller)
    return get_service_runtime(controller).request_sync(
        f"/PmLifecycleService/{operation}", **arguments)
