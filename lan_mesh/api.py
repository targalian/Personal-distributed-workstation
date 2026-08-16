"""
FastAPI 路由层 - Worker API

Worker API:
  GET  /info          - 返回本机完整配置
  GET  /shared        - 列出共享文件
  GET  /shared/{path} - 下载共享文件
  POST /shared        - 上传文件到共享目录

历史说明 (P3 清理): 旧 Secretary API (create_secretary_router /
broadcast_ws 及其专用 payload 模型) 已随 secretary.py 历史入口删除,
Secretary 端路由现由 station_routes_* 三件套承担。
"""
import time

from fastapi import APIRouter, HTTPException, UploadFile, File
from fastapi.responses import FileResponse

from .shared_folder import SharedFolderManager
from .logger import get_logger

logger = get_logger("api")


# ── Worker 路由 ─────────────────────────────────────────────────

def create_worker_router(
    collect_info_fn,        # Callable[[], HostInfo]
    shared_folder: SharedFolderManager,
    agent_runtime=None,     # AgentRuntime instance (optional, for task execution)
    role_manager=None,      # WorkerAgent instance (optional, for remote role management)
) -> APIRouter:
    """创建 Worker 节点的 API 路由。"""
    router = APIRouter()

    @router.get("/info")
    async def get_info():
        """返回本机完整配置信息。"""
        return collect_info_fn().to_dict()

    # ── 任务执行端点 ─────────────────────────────────────

    @router.post("/tasks/execute")
    async def execute_task(payload: dict):
        """接收 Secretary 分发的子任务并执行。"""
        if not agent_runtime:
            raise HTTPException(status_code=503, detail="Agent 运行时未初始化")
        result = agent_runtime.execute(payload)
        return result

    @router.get("/agents/cli-status")
    async def cli_agent_status():
        """查询本机可用的 CLI Agent 后端状态。"""
        if not agent_runtime:
            return {"agents": [], "preferred": None, "error": "Agent 运行时未初始化"}
        return agent_runtime.list_cli_agents()

    @router.get("/shared")
    async def list_shared():
        """列出共享文件夹内容。"""
        return {
            "folder": str(shared_folder.path),
            "files": shared_folder.list_files(),
            "file_count": shared_folder.file_count(),
        }

    @router.get("/shared/{file_path:path}")
    async def download_shared(file_path: str):
        """下载共享文件。"""
        try:
            full = shared_folder.get_file(file_path)
            return FileResponse(
                path=str(full),
                filename=full.name,
                media_type="application/octet-stream",
            )
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail=f"文件不存在: {file_path}")
        except ValueError as e:
            raise HTTPException(status_code=403, detail=str(e))

    @router.post("/shared")
    async def upload_shared(file: UploadFile = File(...)):
        """上传文件到共享目录。"""
        data = await file.read()
        dest = shared_folder.save_upload(file.filename or "upload.bin", data)
        return {
            "ok": True,
            "filename": dest.name,
            "path": str(dest),
            "size": len(data),
        }

    # ── 角色管理端点 (远程 Secretary 分配) ──────────────────

    @router.post("/role/start-secretary")
    async def start_secretary(payload: dict = None):
        """在本机启动 Secretary 子进程 (由 Station Director 远程调用)。"""
        if not role_manager:
            raise HTTPException(status_code=503, detail="角色管理未初始化")
        port = (payload or {}).get("port")
        result = role_manager.start_secretary(port)
        if not result.get("ok"):
            raise HTTPException(status_code=409, detail=result.get("message", "启动失败"))
        return result

    @router.post("/role/stop-secretary")
    async def stop_secretary():
        """停止本机的 Secretary 子进程。"""
        if not role_manager:
            raise HTTPException(status_code=503, detail="角色管理未初始化")
        result = role_manager.stop_secretary()
        return result

    @router.get("/role/status")
    async def role_status():
        """查询本机 Secretary 运行状态。"""
        if not role_manager:
            return {"running": False, "role_manager": "unavailable"}
        return role_manager.get_secretary_status()

    # ── PM Agent 端点 (Secretary 远程激活 PM) ───────────────────

    @router.post("/role/start-pm")
    async def start_pm(payload: dict):
        """在本 Worker 上启动 PM Agent 接管任务。

        请求体:
            {
                "task_id": "task-xxx",
                "secretary_url": "http://192.168.1.100:45470",
                "task_data": {}  // 可选, 任务详情
            }
        """
        if not role_manager:
            raise HTTPException(status_code=503, detail="角色管理未初始化")
        task_id = payload.get("task_id", "")
        secretary_url = payload.get("secretary_url", "")
        task_data = payload.get("task_data")
        if not task_id or not secretary_url:
            raise HTTPException(status_code=400, detail="缺少 task_id 或 secretary_url")
        result = role_manager.start_pm(task_id, secretary_url, task_data)
        if not result.get("ok"):
            raise HTTPException(status_code=409, detail=result.get("message", "启动失败"))
        return result

    @router.post("/role/stop-pm")
    async def stop_pm():
        """停止本 Worker 上的 PM Agent。"""
        if not role_manager:
            raise HTTPException(status_code=503, detail="角色管理未初始化")
        return role_manager.stop_pm()

    @router.get("/role/pm-status")
    async def pm_status():
        """查询本 Worker 上的 PM Agent 运行状态。"""
        if not role_manager:
            return {"running": False, "role_manager": "unavailable"}
        return role_manager.get_pm_status()

    # ── PM 子 Agent 端点 (PM 调用, 在目标 Worker 创建子 Agent) ──

    @router.post("/pm/create-subagent")
    async def create_subagent(payload: dict):
        """在本 Worker 上为 PM 创建子 Agent。

        请求体:
            {
                "agent_name": "code-reviewer-01",
                "skills": ["code_review"],
                "task_description": "审查 auth 模块代码",
                "system_prompt": "PM 生成的定制 prompt (可选)",
                "preferred_agent_id": "sub-abc123 (可选, PM预生成)"
            }
        """
        if not role_manager:
            raise HTTPException(status_code=503, detail="角色管理未初始化")
        agent_name = payload.get("agent_name", "")
        skills = payload.get("skills", [])
        task_desc = payload.get("task_description", "")
        system_prompt = payload.get("system_prompt", "")
        preferred_id = payload.get("preferred_agent_id", "")
        if not agent_name:
            raise HTTPException(status_code=400, detail="缺少 agent_name")
        return role_manager.create_subagent(agent_name, skills, task_desc, system_prompt, preferred_id)

    @router.post("/pm/progress-report")
    async def pm_progress_report(payload: dict):
        """子 Agent 向 PM 上报进度 (Worker 转发给 PM Agent)。

        请求体:
            {
                "pm_id": "pm-xxx",
                "reporter_id": "sub-xxx",
                "task_name": "代码审查",
                "progress": 0.8,
                "status": "in_progress",
                "message": "已完成 80%"
            }
        """
        if not role_manager:
            raise HTTPException(status_code=503, detail="角色管理未初始化")
        return role_manager.forward_progress_report(payload)

    @router.get("/pm/subagents")
    async def list_subagents():
        """列出本 Worker 上的所有子 Agent 状态。"""
        if not role_manager:
            return {"sub_agents": [], "total": 0}
        return role_manager.get_subagent_status()

    @router.post("/pm/update-prompt")
    async def update_subagent_prompt(payload: dict):
        """动态更新子 Agent 的 system prompt (优化2)。

        请求体:
            {
                "agent_id": "sub-abc123",
                "system_prompt": "新的 prompt 内容"
            }
        """
        if not role_manager:
            raise HTTPException(status_code=503, detail="角色管理未初始化")
        agent_id = payload.get("agent_id", "")
        new_prompt = payload.get("system_prompt", "")
        if not agent_id:
            raise HTTPException(status_code=400, detail="缺少 agent_id")
        return role_manager.update_subagent_prompt(agent_id, new_prompt)

    # ── 优化7: 反向沟通通道 ──

    @router.post("/pm/inject-input")
    async def pm_inject_input(payload: dict):
        """向 PM Agent 注入来自 Secretary/Boss 的回复 (反向沟通通道)。

        请求体:
            {
                "response": "Boss的回复文本",
                "choice": "选中的选项",
                "task_name": "请求决策"
            }
        """
        if not role_manager:
            raise HTTPException(status_code=503, detail="角色管理未初始化")
        result = role_manager.inject_pm_input(payload)
        if not result.get("ok"):
            raise HTTPException(status_code=409, detail=result.get("message", "注入失败"))
        return result

    # ── 优化8: PM 控制端点 ──

    @router.post("/role/cancel-pm")
    async def cancel_pm():
        """取消本 Worker 上的 PM Agent 任务。"""
        if not role_manager:
            raise HTTPException(status_code=503, detail="角色管理未初始化")
        return role_manager.cancel_pm()

    @router.post("/role/pause-pm")
    async def pause_pm():
        """暂停本 Worker 上的 PM Agent 任务。"""
        if not role_manager:
            raise HTTPException(status_code=503, detail="角色管理未初始化")
        return role_manager.pause_pm()

    # ── P2P 消息接收 (主机间通讯) ───────────────────────────

    # Worker 端内存级消息存储
    _p2p_store = {}

    @router.post("/api/p2p/receive")
    async def p2p_receive_message(payload: dict):
        """接收来自远程主机的 P2P 消息。

        其他主机通过 HTTP POST 调用此端点向本机发送消息。
        Worker 端存储并打印消息,不做 WebSocket 广播。
        """
        from_device_id = payload.get("from_device_id", "")
        from_name = payload.get("from_name", "未知")
        message = payload.get("message", "")
        timestamp = payload.get("timestamp", time.time())

        if not from_device_id or not message:
            raise HTTPException(status_code=400, detail="缺少 from_device_id 或 message")

        msg = {
            "from_device_id": from_device_id,
            "from_name": from_name,
            "message": message,
            "timestamp": timestamp,
        }

        # 存储到内存
        if from_device_id not in _p2p_store:
            _p2p_store[from_device_id] = []
        _p2p_store[from_device_id].append(msg)

        logger.info("[P2P] 收到来自 %s (%s) 的消息: %s", from_name, from_device_id[:8], message[:80])

        return {"ok": True}

    return router
