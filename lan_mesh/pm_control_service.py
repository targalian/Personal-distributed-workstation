"""Yomk PM control service for local identity-safe and remote task operations."""

from typing import Any

from ._vendor.yomkserverpy.YomkServerPy import YomkService
from .logger import get_logger
from .service_runtime import get_service_runtime
from .pm_lifecycle_service import call_pm
from .http_retry import auth_headers

logger = get_logger("station")


class PmControlService(YomkService):
    """Resolve PM location and control only the intended local instance."""

    def __init__(self, server, controller: object):
        super().__init__(server)
        self.set_name("/PmControlService")
        self.controller = controller

    def pause_task(self, task_id: str) -> dict:
        """暂停指定任务及对应的 PM Agent。

        Args:
            task_id: 任务 ID

        Returns:
            {ok: bool, message: str}
        """
        from .station_pm_control import http_post
        task = self.controller.db.get_task(task_id)
        if not task:
            return {"ok": False, "message": f"任务 {task_id} 不存在"}

        pm_id = task.pm_agent_id
        if not pm_id:
            return {"ok": False, "message": "任务尚未分配 PM Agent"}

        worker = self.controller._pm_worker_map.get(pm_id)
        if not worker:
            return {"ok": False, "message": "PM Agent 的 Worker 信息未找到"}

        # 本机 PM 直接暂停
        if worker.get("local"):
            result = call_pm(self.controller, "pause", expected_pm_id=pm_id)
            if not result.get("ok"):
                return result
            self.controller.db.update_task_status(task_id, "paused")
            self.controller.db.update_pm_status(pm_id, "paused")
            logger.info("本机任务已暂停: %s", task_id)
            self.controller.bot_gateway.notify("task_paused", {"task_id": task_id, "name": task.name})
            return {"ok": True, "message": "任务已暂停"}

        ip = worker.get("ip", "")
        port = worker.get("api_port", 0)
        if not ip or not port:
            return {"ok": False, "message": "Worker 地址信息不完整"}

        try:
            # iter-66 (Bug G 同类): 认证启用时远程 pause 端点同样要求 Bearer
            from .http_retry import auth_headers
            resp = http_post(
                f"http://{ip}:{port}/role/pause-pm",
                json={"pm_id": pm_id},
                headers=auth_headers(),
                retries=1,
                timeout=10,
            )
            if resp.status_code == 200:
                self.controller.db.update_task_status(task_id, "paused")
                self.controller.db.update_pm_status(pm_id, "paused")
                logger.info("任务已暂停: %s", task_id)
                self.controller.bot_gateway.notify("task_paused", {
                    "task_id": task_id, "name": task.name,
                })
                return {"ok": True, "message": "任务已暂停"}
            else:
                return {"ok": False, "message": f"Worker 返回错误: {resp.text[:200]}"}
        except Exception as e:
            return {"ok": False, "message": f"暂停失败: {e}"}


    def cancel_task(self, task_id: str) -> dict:
        """取消指定任务及对应的 PM Agent。

        Args:
            task_id: 任务 ID

        Returns:
            {ok: bool, message: str}
        """
        from .station_pm_control import http_post
        task = self.controller.db.get_task(task_id)
        if not task:
            return {"ok": False, "message": f"任务 {task_id} 不存在"}

        pm_id = task.pm_agent_id
        if not pm_id:
            # 任务尚未分配 PM, 直接取消
            self.controller.db.update_task_status(task_id, "cancelled")
            return {"ok": True, "message": "任务已取消 (未分配 PM)"}

        worker = self.controller._pm_worker_map.get(pm_id)
        if not worker:
            # Worker 信息丢失, 标记取消
            self.controller.db.update_task_status(task_id, "cancelled")
            self.controller.db.update_pm_status(pm_id, "cancelled")
            return {"ok": True, "message": "任务已标记取消 (Worker 信息丢失)"}

        # 本机 PM 直接取消
        if worker.get("local"):
            result = call_pm(self.controller, "cancel", expected_pm_id=pm_id)
            if not result.get("ok") and result.get("code") not in ("owner_changed", "not_running"):
                return result
            self.controller.db.update_task_status(task_id, "cancelled")
            self.controller.db.update_pm_status(pm_id, "cancelled")
            # iter-66 (Bug H): 清理映射, 防止 busy 误判
            self.controller._pm_worker_map.pop(pm_id, None)
            logger.info("本机任务已取消: %s", task_id)
            self.controller.bot_gateway.notify("task_cancelled", {"task_id": task_id, "name": task.name})
            return {"ok": True, "message": "任务已取消"}

        ip = worker.get("ip", "")
        port = worker.get("api_port", 0)
        if not ip or not port:
            return {"ok": False, "message": "Worker 地址信息不完整"}

        try:
            # iter-66 (Bug G): 认证启用时远程 stop/cancel 端点同样要求 Bearer
            from .http_retry import auth_headers
            resp = http_post(
                f"http://{ip}:{port}/role/cancel-pm",
                json={"pm_id": pm_id},
                headers=auth_headers(),
                retries=1,
                timeout=10,
            )
            if resp.status_code == 200:
                self.controller.db.update_task_status(task_id, "cancelled")
                self.controller.db.update_pm_status(pm_id, "cancelled")
                # iter-66 (Bug H): 清理映射, 否则 _is_worker_busy 误判
                # Worker 仍忙 → F3.1 自动扩容永不派发到该 Worker
                self.controller._pm_worker_map.pop(pm_id, None)
                logger.info("任务已取消: %s", task_id)
                self.controller.bot_gateway.notify("task_cancelled", {
                    "task_id": task_id, "name": task.name,
                })
                return {"ok": True, "message": "任务已取消"}
            else:
                return {"ok": False, "message": f"Worker 返回错误: {resp.text[:200]}"}
        except Exception as e:
            return {"ok": False, "message": f"取消失败: {e}"}


    def inject_input_to_pm(self, pm_id: str, input_data: dict) -> dict:
        """向指定 PM Agent 注入来自 Boss 的回复。

        优先检查本机 PM, 否则 HTTP POST 到远程 Worker。
        """
        from .station_pm_control import http_post
        worker = self.controller._pm_worker_map.get(pm_id)

        # 本机 PM 直接调用
        if worker and worker.get("local") and self.controller._local_pm_agent:
            result = call_pm(self.controller, "inject_input", input_data=input_data, expected_pm_id=pm_id)
            if result.get("ok"):
                logger.info("已注入回复到本机 PM %s", pm_id[:12])
                return {"ok": True, "message": "回复已发送到 PM Agent"}
            return result

        if not worker:
            return {"ok": False, "message": f"PM {pm_id[:12]} 的 Worker 信息未找到"}

        ip = worker.get("ip", "")
        port = worker.get("api_port", 0)
        if not ip or not port:
            return {"ok": False, "message": "Worker 地址信息不完整"}

        try:
            resp = http_post(
                f"http://{ip}:{port}/pm/inject-input",
                headers={**auth_headers(), "X-PM-ID": pm_id},
                json=input_data,
                timeout=10,
            )
            if resp.status_code == 200:
                result = resp.json()
                logger.info("已注入回复到 PM %s", pm_id[:12])
                return {"ok": True, "message": "回复已发送到 PM Agent"}
            else:
                return {"ok": False, "message": f"Worker 返回错误: {resp.text[:200]}"}
        except Exception as e:
            return {"ok": False, "message": f"注入失败: {e}"}



def call_pm_control(controller: object, operation: str, **arguments: Any) -> dict:
    """Compose and invoke the same service from HTTP, chat and internal commands."""
    runtime = get_service_runtime(controller)
    runtime.ensure("/PmControlService", lambda server: PmControlService(server, controller),
                   ["inject_input_to_pm", "cancel_task", "pause_task"])
    return runtime.request_sync(f"/PmControlService/{operation}", **arguments)
