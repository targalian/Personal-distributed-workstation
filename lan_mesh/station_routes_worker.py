"""
Station Worker 侧路由 — 内嵌 Worker 端点/P2P 通讯/云存储同步 (P1 #2 拆分产物)

始终可用 (Station 作为内嵌 Worker 被 PM/其他主机调用):
  - 本机 PM 生命周期 (/role/*: start/stop/cancel/pause/status)
  - 子 Agent 创建/进度上报/注入/列表、子任务异步执行 (/pm/*, /tasks/execute)
  - P2P 主机间聊天与文件传输 (/api/p2p/*)
  - 云存储同步 (/api/cloud-sync/*)
"""
import time
import uuid as _uuid

import requests as http_requests

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from .http_retry import auth_headers
from .logger import get_logger
from .station_routes_common import _broadcast

logger = get_logger("station_api")


def build_worker_routes(controller) -> APIRouter:
    """内嵌 Worker / P2P / 云同步路由。"""
    router = APIRouter()

    # 便捷别名
    db = controller.db
    state = controller.state
    discovery = controller.discovery

    # ════════════════════════════════════════════════════════════
    #  P2P 主机间通讯辅助
    # ════════════════════════════════════════════════════════════

    def _resolve_p2p_target(device_id: str) -> tuple:
        """解析 P2P 目标主机的网络信息, 返回 (ip, port, name) 或抛出 HTTPException。"""
        host = db.get_host(device_id)
        if not host:
            dev = discovery.find_device(device_id)
            if not dev:
                raise HTTPException(status_code=404, detail="目标主机不存在")
            ip = dev.get("ip", "")
            port = dev.get("api_port", 0)
            name = dev.get("device_name", dev.get("hostname", "未知"))
        else:
            ip = host.ip
            port = host.api_port
            name = host.device_name or host.hostname or "未知"
        if not ip or not port:
            raise HTTPException(status_code=400, detail="目标主机网络信息不完整")
        return ip, port, name

    def _append_p2p_msg(device_id: str, msg: dict):
        """向指定主机的 P2P 消息列表追加一条消息。"""
        state.p2p_messages.setdefault(device_id, []).append(msg)

    # ── 内嵌 Worker: 本机 PM Agent 端点 (PM 回调用) ────────────

    @router.post("/role/start-pm")
    async def local_start_pm(payload: dict):
        """在本机 Station 进程内启动 PM Agent (无需单独 Worker)。"""
        task_id = payload.get("task_id", "")
        secretary_url = payload.get("secretary_url", "")
        task_data = payload.get("task_data")
        if not task_id or not secretary_url:
            raise HTTPException(status_code=400, detail="缺少 task_id 或 secretary_url")
        # R3/M5-2: 注入用量上报目标 (本机作为 Worker, 记账汇总到
        # Secretary; 携带 mesh_token 启用 WS 直推通道)
        from .auth import get_mesh_token
        from .model_resources import set_report_target_global
        from .station_routes_common import get_mesh_auth_token
        set_report_target_global(
            secretary_url,
            token=get_mesh_auth_token() or get_mesh_token())
        result = controller._local_start_pm(task_id, secretary_url, task_data)
        if not result.get("ok"):
            raise HTTPException(status_code=409, detail=result.get("message", "启动失败"))
        return result

    @router.post("/role/stop-pm")
    async def local_stop_pm():
        return controller._local_stop_pm()

    @router.post("/role/cancel-pm")
    async def local_cancel_pm():
        return controller._local_cancel_pm()

    @router.post("/role/pause-pm")
    async def local_pause_pm():
        return controller._local_pause_pm()

    @router.get("/role/pm-status")
    async def local_pm_status():
        return controller._local_pm_status()

    @router.post("/pm/create-subagent")
    async def local_create_subagent(payload: dict):
        """在本机为 PM 创建子 Agent。"""
        agent_name = payload.get("agent_name", "")
        if not agent_name:
            raise HTTPException(status_code=400, detail="缺少 agent_name")
        return controller._local_create_subagent(
            agent_name=agent_name,
            skills=payload.get("skills", []),
            task_description=payload.get("task_description", ""),
            system_prompt=payload.get("system_prompt", ""),
            preferred_agent_id=payload.get("preferred_agent_id", ""),
        )

    @router.post("/pm/progress-report")
    async def local_progress_report(payload: dict):
        """子 Agent 向本机 PM 上报进度。"""
        return controller._local_forward_progress(payload)

    @router.post("/pm/inject-input")
    async def local_inject_input(payload: dict):
        """向本机 PM 注入 Boss 回复。"""
        result = controller._local_inject_input(payload)
        if not result.get("ok"):
            raise HTTPException(status_code=409, detail=result.get("message", "注入失败"))
        return result

    @router.get("/pm/subagents")
    async def local_list_subagents():
        """列出本机所有子 Agent。"""
        result = []
        for agent_id, info in controller._local_sub_agents.items():
            result.append({
                "agent_id": agent_id,
                "agent_name": info.get("agent_name", ""),
                "current_task": info.get("current_task", ""),
                "status": info.get("status", "idle"),
            })
        return {"sub_agents": result, "total": len(result)}

    @router.post("/tasks/execute")
    async def local_execute_task(payload: dict):
        """接收 PM Dispatcher 分发的子任务并在本机异步执行。

        立即返回 {"status": "started"}，实际执行在后台线程完成，
        执行完毕后通过 receive_subtask_result 回调 PM Monitor。

        调用方校验 (M1): 必填字段缺失直接 400, 不进入执行线程。
        """
        name = str(payload.get("name", "")).strip()
        input_data = payload.get("input_data")
        if not name:
            raise HTTPException(status_code=400, detail="缺少必填字段: name")
        if input_data is None or not isinstance(input_data, dict):
            raise HTTPException(status_code=400, detail="缺少必填字段: input_data")
        if not payload.get("subtask_id"):
            raise HTTPException(status_code=400, detail="缺少必填字段: subtask_id")

        import threading as _threading

        def _run():
            try:
                controller._local_execute_task(payload)
            except Exception as e:
                logger.error("本机子任务执行异常: %s", e)

        _threading.Thread(
            target=_run, daemon=True,
            name=f"subtask-{payload.get('name', '')[:20]}",
        ).start()
        return {"status": "started", "name": payload.get("name", "")}

    # ════════════════════════════════════════════════════════════
    #  P2P 主机间通讯 (聊天 + 文件传输)
    # ════════════════════════════════════════════════════════════

    @router.post("/api/p2p/send")
    async def p2p_send_message(payload: dict):
        """向目标主机发送聊天消息。

        消息存储在本地并通过 HTTP 转发到目标主机的 /api/p2p/receive 端点,
        同时通过 WebSocket 广播给本机 Dashboard。
        """
        target_device_id = payload.get("target_device_id", "")
        message = payload.get("message", "")
        if not target_device_id or not message:
            raise HTTPException(status_code=400, detail="缺少 target_device_id 或 message")

        target_ip, target_port, target_name = _resolve_p2p_target(target_device_id)

        # 存储发出消息
        msg = {
            "id": str(_uuid.uuid4()),
            "direction": "out",
            "type": "text",
            "content": message,
            "timestamp": time.time(),
            "from_device_id": state.device_id,
            "from_name": state.device_name or "本机",
            "to_device_id": target_device_id,
            "to_name": target_name,
        }
        _append_p2p_msg(target_device_id, msg)

        # WebSocket 广播
        await _broadcast(state, "p2p_chat", msg)

        # 转发到目标主机
        try:
            resp = http_requests.post(
                f"http://{target_ip}:{target_port}/api/p2p/receive",
                json={
                    "from_device_id": state.device_id,
                    "from_name": state.device_name or "本机",
                    "from_port": state.api_port,
                    "message": message,
                    "timestamp": msg["timestamp"],
                },
                headers=auth_headers(),
                timeout=10,
            )
            if resp.status_code != 200:
                err_msg = {
                    "id": str(_uuid.uuid4()),
                    "direction": "in",
                    "type": "system",
                    "content": f"[送达失败] 目标主机返回 {resp.status_code}",
                    "timestamp": time.time(),
                    "from_device_id": target_device_id,
                    "from_name": target_name,
                }
                _append_p2p_msg(target_device_id, err_msg)
                await _broadcast(state, "p2p_chat", err_msg)
        except Exception as e:
            err_msg = {
                "id": str(_uuid.uuid4()),
                "direction": "in",
                "type": "system",
                "content": f"[发送失败] {str(e)}",
                "timestamp": time.time(),
                "from_device_id": target_device_id,
                "from_name": target_name,
            }
            _append_p2p_msg(target_device_id, err_msg)
            await _broadcast(state, "p2p_chat", err_msg)

        return {"ok": True, "message_id": msg["id"]}

    @router.post("/api/p2p/receive")
    async def p2p_receive_message(payload: dict):
        """接收来自远程主机的消息。

        其他主机通过 HTTP POST 调用此端点发送消息给本机。
        """
        from_device_id = payload.get("from_device_id", "")
        from_name = payload.get("from_name", "未知")
        message = payload.get("message", "")
        timestamp = payload.get("timestamp", time.time())

        if not from_device_id or not message:
            raise HTTPException(status_code=400, detail="缺少 from_device_id 或 message")

        msg = {
            "id": str(_uuid.uuid4()),
            "direction": "in",
            "type": "text",
            "content": message,
            "timestamp": timestamp,
            "from_device_id": from_device_id,
            "from_name": from_name,
            "to_device_id": state.device_id,
            "to_name": state.device_name or "本机",
        }
        _append_p2p_msg(from_device_id, msg)

        # WebSocket 广播给本机 Dashboard
        await _broadcast(state, "p2p_chat", msg)

        return {"ok": True}

    @router.get("/api/p2p/messages/{device_id}")
    async def p2p_get_messages(device_id: str):
        """获取与指定主机的聊天历史。"""
        messages = state.p2p_messages.get(device_id, [])
        return {"messages": messages, "total": len(messages)}

    @router.post("/api/p2p/transfer")
    async def p2p_transfer_file(
        file: UploadFile = File(...),
        target_device_id: str = Form(...),
    ):
        """向目标主机传输文件。

        文件通过 HTTP 上传到目标主机的 /shared 端点。
        """
        target_ip, target_port, target_name = _resolve_p2p_target(target_device_id)

        # 读取文件数据
        data = await file.read()
        filename = file.filename or "upload.bin"

        # 上传到目标主机的 /shared 端点
        try:
            resp = http_requests.post(
                f"http://{target_ip}:{target_port}/shared",
                files={"file": (filename, data)},
                headers=auth_headers(),
                timeout=120,
            )
            result = resp.json()

            # 存储传输记录
            transfer_msg = {
                "id": str(_uuid.uuid4()),
                "direction": "out",
                "type": "file",
                "filename": filename,
                "size": len(data),
                "timestamp": time.time(),
                "from_device_id": state.device_id,
                "from_name": state.device_name or "本机",
                "to_device_id": target_device_id,
                "to_name": target_name,
                "status": "success" if result.get("ok") else "failed",
                "remote_path": result.get("path", ""),
            }
            _append_p2p_msg(target_device_id, transfer_msg)
            await _broadcast(state, "p2p_chat", transfer_msg)

            return {"ok": True, "filename": filename, "size": len(data), "remote_path": result.get("path", "")}
        except Exception as e:
            # 存储失败记录
            transfer_msg = {
                "id": str(_uuid.uuid4()),
                "direction": "out",
                "type": "file",
                "filename": filename,
                "size": len(data),
                "timestamp": time.time(),
                "from_device_id": state.device_id,
                "from_name": state.device_name or "本机",
                "to_device_id": target_device_id,
                "to_name": target_name,
                "status": "failed",
                "error": str(e),
            }
            _append_p2p_msg(target_device_id, transfer_msg)
            await _broadcast(state, "p2p_chat", transfer_msg)

            raise HTTPException(status_code=500, detail=f"文件传输失败: {str(e)}")

    # ════════════════════════════════════════════════════════════
    #  云存储同步 (Cloud Sync)
    # ════════════════════════════════════════════════════════════

    @router.get("/api/cloud-sync/status")
    async def cloud_sync_status():
        """查询云存储同步状态。"""
        cloud_sync = getattr(controller.state, 'cloud_sync', None)
        if not cloud_sync:
            cfg = controller.cfg.cloud_storage
            return {
                "enabled": cfg.enabled,
                "configured": bool(cfg.endpoint),
                "running": False,
                "message": "云存储同步未启动" if not cfg.enabled else "未配置 endpoint",
            }
        return cloud_sync.get_status()

    @router.post("/api/cloud-sync/sync")
    async def cloud_sync_now():
        """手动触发一次云存储同步。"""
        cloud_sync = getattr(controller.state, 'cloud_sync', None)
        if not cloud_sync:
            raise HTTPException(status_code=503, detail="云存储同步未启动")
        result = cloud_sync.sync()
        await _broadcast(state, "cloud_sync", result)
        return result

    @router.post("/api/cloud-sync/test")
    async def cloud_sync_test():
        """测试云存储连接。"""
        cloud_sync = getattr(controller.state, 'cloud_sync', None)
        if not cloud_sync:
            raise HTTPException(status_code=503, detail="云存储同步未启动")
        return cloud_sync.test_connection()

    return router
