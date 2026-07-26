"""
FastAPI 路由层 - Worker API 与 Secretary API

Worker API:
  GET  /info          - 返回本机完整配置
  GET  /shared        - 列出共享文件
  GET  /shared/{path} - 下载共享文件
  POST /shared        - 上传文件到共享目录

Secretary API:
  POST /api/register       - Worker 注册 (接收完整 HostInfo)
  POST /api/heartbeat      - Worker 心跳 (实时资源使用率)
  GET  /api/hosts          - 所有主机列表
  GET  /api/hosts/{id}     - 单台主机详情
  GET  /api/network        - 本机网络状态
  POST /api/probe/{ip}     - 主动探测指定IP
  GET  /api/discovery      - UDP 发现到的设备列表
  WS   /ws                 - 实时推送主机状态变更
"""
import asyncio
import json
import time
from pathlib import Path
from typing import Optional

from fastapi import (
    APIRouter, WebSocket, WebSocketDisconnect, HTTPException,
    UploadFile, File, Query,
)
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from .protocol import HostInfo, HostRecord, NetworkStatus, AgentCard
from .shared_folder import SharedFolderManager


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

        print(f"[P2P] 收到来自 {from_name} ({from_device_id[:8]}) 的消息: {message[:80]}")

        return {"ok": True}

    return router


# ── Secretary 路由 ────────────────────────────────────────────────

def create_secretary_router(
    db,                     # Database instance
    discovery,              # DiscoveryService instance
    collect_info_fn,        # Callable[[], HostInfo]
    shared_folder: SharedFolderManager,
    state,                  # SecretaryState shared object
    orchestrator=None,       # Orchestrator instance (optional)
    mcp_gateway=None,        # MCPGateway instance (optional)
    project_manager=None,    # ProjectManager instance (optional)
    model_router=None,       # ModelRouter instance (optional)
    station_director=None,   # StationDirector instance (optional)
) -> APIRouter:
    """创建 Secretary 节点的 API 路由。"""
    router = APIRouter()

    @router.post("/api/register")
    async def register_worker(payload: dict):
        """接收 Worker 的注册请求 (完整 HostInfo)。"""
        info = HostInfo.from_dict(payload)
        if station_director:
            record = station_director.on_host_registered(info)
        else:
            record = HostRecord(
                device_id=info.device_id, device_name=info.device_name,
                role=info.role, hostname=info.hostname, platform=info.platform,
                ip="", api_port=info.api_port,
                cpu_count=info.cpu_count, memory_total_mb=info.memory_total_mb,
                disk_total_gb=info.disk_total_gb,
                cpu_percent=info.cpu_percent, memory_percent=info.memory_percent,
                disk_percent=info.disk_percent,
                shared_folder=info.shared_folder, shared_file_count=info.shared_file_count,
                online=True, registered_at=time.time(), last_seen=time.time(),
            )
            dev = discovery.find_device(info.device_id)
            if dev:
                record.ip = dev.get("ip", "")
            db.upsert_host(record)
        await broadcast_ws(state, "host_registered", record.to_dict())
        return {"ok": True, "device_id": info.device_id}

    @router.post("/api/heartbeat")
    async def heartbeat(payload: dict):
        """接收 Worker 心跳 (实时资源使用率)。"""
        device_id = payload.get("device_id", "")
        if station_director:
            record = station_director.on_heartbeat(device_id, {
                "cpu_percent": payload.get("cpu_percent"),
                "memory_percent": payload.get("memory_percent"),
                "disk_percent": payload.get("disk_percent"),
                "shared_file_count": payload.get("shared_file_count"),
            })
            if not record:
                raise HTTPException(status_code=404, detail="设备未注册")
        else:
            record = db.get_host(device_id)
            if not record:
                raise HTTPException(status_code=404, detail="设备未注册")
            record.cpu_percent = payload.get("cpu_percent", record.cpu_percent)
            record.memory_percent = payload.get("memory_percent", record.memory_percent)
            record.disk_percent = payload.get("disk_percent", record.disk_percent)
            record.shared_file_count = payload.get("shared_file_count", record.shared_file_count)
            record.online = True
            record.last_seen = time.time()
            dev = discovery.find_device(device_id)
            if dev:
                record.ip = dev.get("ip", record.ip)
            db.upsert_host(record)
            db.log_heartbeat(device_id, record.cpu_percent, record.memory_percent, record.disk_percent)
        await broadcast_ws(state, "heartbeat", record.to_dict())
        return {"ok": True}

    @router.get("/api/hosts")
    async def list_hosts():
        """返回所有注册主机列表 (DB 持久化 + UDP 发现合并)。"""
        db_hosts = db.list_hosts()
        discovery_devices = discovery.list_devices()

        # 以 DB 为主,补充 UDP 发现到的设备
        db_ids = {h.device_id for h in db_hosts}
        for dev in discovery_devices:
            if dev["device_id"] not in db_ids:
                # UDP 发现但未通过 HTTP 注册的设备
                db_hosts.append(HostRecord(
                    device_id=dev["device_id"],
                    device_name=dev.get("device_name", ""),
                    role=dev.get("role", "worker"),
                    hostname=dev.get("hostname", ""),
                    platform=dev.get("platform", ""),
                    ip=dev.get("ip", ""),
                    api_port=dev.get("api_port", 0),
                    cpu_count=dev.get("cpu_count", 0),
                    memory_total_mb=dev.get("memory_total_mb", 0),
                    disk_total_gb=dev.get("disk_total_gb", 0),
                    cpu_percent=dev.get("cpu_percent", 0),
                    memory_percent=dev.get("memory_percent", 0),
                    disk_percent=dev.get("disk_percent", 0),
                    shared_folder=dev.get("shared_folder", ""),
                    online=dev.get("online", False),
                    last_seen=time.time(),
                ))

        return {
            "hosts": [h.to_dict() for h in db_hosts],
            "total": len(db_hosts),
            "online": sum(1 for h in db_hosts if h.online),
        }

    @router.get("/api/hosts/{device_id}")
    async def get_host(device_id: str):
        """查询单台主机详情。"""
        record = db.get_host(device_id)
        if not record:
            dev = discovery.find_device(device_id)
            if not dev:
                raise HTTPException(status_code=404, detail="主机不存在")
            return dev
        return record.to_dict()

    @router.get("/api/network")
    async def get_network():
        """返回 Secretary 本机网络状态。"""
        ns = discovery.network_status()
        return {
            "udp_port": ns.udp_port,
            "api_port": ns.api_port,
            "local_ips": ns.local_ips,
            "broadcast_targets": ns.broadcast_targets,
        }

    @router.get("/api/discovery")
    async def get_discovery():
        """返回 UDP 发现到的所有设备 (实时)。"""
        return {
            "devices": discovery.list_devices(),
            "total": len(discovery._devices),
        }

    @router.post("/api/probe/{ip}")
    async def probe_ip(ip: str):
        """主动探测指定 IP。"""
        discovery.probe_ip(ip)
        return {"ok": True, "message": f"已向 {ip} 发送探测包"}

    @router.get("/api/health")
    async def health():
        """健康检查。"""
        return {
            "status": "ok",
            "role": "secretary",
            "uptime": time.time() - state.start_time,
            "device_id": state.device_id,
        }

    @router.get("/api/secretary-info")
    async def secretary_info():
        """返回 Secretary 自身的主机信息。"""
        return collect_info_fn().to_dict()

    @router.get("/api/shared")
    async def list_shared():
        """列出 Secretary 共享文件夹内容。"""
        return {
            "folder": str(shared_folder.path),
            "files": shared_folder.list_files(),
            "file_count": shared_folder.file_count(),
        }

    # ── Agent 管理端点 ─────────────────────────────────────

    @router.post("/api/agents/register")
    async def register_agent(payload: dict):
        """接收 Worker 的 Agent Card 注册。"""
        card = AgentCard.from_dict(payload)
        card.last_seen = time.time()
        # 尝试从 UDP 发现列表获取真实 IP
        dev = discovery.find_device(card.device_id)
        if dev:
            card.ip = dev.get("ip", card.ip)
        db.upsert_agent(card)
        await broadcast_ws(state, "agent_registered", card.to_dict())
        return {"ok": True, "agent_id": card.agent_id}

    @router.get("/api/agents")
    async def list_agents(status: str = None):
        """列出所有注册的 Agent。"""
        agents = db.list_agents(status=status)
        return {
            "agents": [a.to_dict() for a in agents],
            "total": len(agents),
            "idle": sum(1 for a in agents if a.status == "idle"),
            "busy": sum(1 for a in agents if a.status == "busy"),
        }

    @router.get("/api/agents/{agent_id}")
    async def get_agent(agent_id: str):
        """查询单个 Agent 详情。"""
        agent = db.get_agent(agent_id)
        if not agent:
            raise HTTPException(status_code=404, detail="Agent 不存在")
        return agent.to_dict()

    # ── 任务管理端点 ─────────────────────────────────────────

    @router.post("/api/tasks")
    async def submit_task(payload: dict):
        """提交新任务,自动分解并调度。

        请求体可选 project_id 字段,关联到指定项目进行预算控制。
        """
        if not orchestrator:
            raise HTTPException(status_code=503, detail="编排器未初始化")
        project_id = payload.get("project_id", "")
        # 如果关联了项目,检查预算
        if project_id and project_manager:
            if not project_manager.check_budget(project_id):
                raise HTTPException(
                    status_code=402,
                    detail="项目预算已起支或已暂停,无法提交任务",
                )
        task = orchestrator.submit_task(
            name=payload.get("name", ""),
            description=payload.get("description", ""),
            input_data=payload.get("input_data", {}),
            created_by=payload.get("created_by", "user"),
            project_id=project_id,
        )
        await broadcast_ws(state, "task_submitted", task.to_dict())
        return task.to_dict()

    @router.get("/api/tasks")
    async def list_tasks(status: str = None, limit: int = 50):
        """列出任务。"""
        tasks = db.list_tasks(status=status, limit=limit)
        return {
            "tasks": [t.to_dict() for t in tasks],
            "total": len(tasks),
        }

    @router.get("/api/tasks/{task_id}")
    async def get_task(task_id: str):
        """查询单个任务状态。"""
        task = db.get_task(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="任务不存在")
        return task.to_dict()

    # ── 项目管理端点 ─────────────────────────────────────────

    @router.post("/api/projects")
    async def create_project(payload: dict):
        """创建新项目。"""
        if not project_manager:
            raise HTTPException(status_code=503, detail="项目管理器未初始化")
        project = project_manager.create_project(
            name=payload.get("name", ""),
            description=payload.get("description", ""),
            budget_limit_usd=payload.get("budget_limit_usd", 10.0),
            allowed_models=payload.get("allowed_models", []),
            routing_strategy=payload.get("routing_strategy", "balanced"),
            workspace_base=payload.get("workspace_base", ""),
        )
        await broadcast_ws(state, "project_created", project.to_dict())
        return project.to_dict()

    @router.get("/api/projects")
    async def list_projects(status: str = None):
        """列出所有项目,可按状态过滤。"""
        if not project_manager:
            return {"projects": [], "total": 0}
        projects = project_manager.list_projects(status=status)
        return {
            "projects": [p.to_dict() for p in projects],
            "total": len(projects),
        }

    @router.get("/api/projects/{project_id}")
    async def get_project(project_id: str):
        """查询单个项目详情 (含预算状态)。"""
        if not project_manager:
            raise HTTPException(status_code=503, detail="项目管理器未初始化")
        status_info = project_manager.get_project_status(project_id)
        if not status_info:
            raise HTTPException(status_code=404, detail="项目不存在")
        return status_info

    @router.put("/api/projects/{project_id}")
    async def update_project(project_id: str, payload: dict):
        """更新项目字段 (预算/模型/策略/状态)。"""
        if not project_manager:
            raise HTTPException(status_code=503, detail="项目管理器未初始化")
        project = project_manager.update_project(
            project_id,
            name=payload.get("name"),
            description=payload.get("description"),
            budget_limit_usd=payload.get("budget_limit_usd"),
            allowed_models=payload.get("allowed_models"),
            routing_strategy=payload.get("routing_strategy"),
            status=payload.get("status"),
        )
        if not project:
            raise HTTPException(status_code=404, detail="项目不存在")
        await broadcast_ws(state, "project_updated", project.to_dict())
        return project.to_dict()

    @router.delete("/api/projects/{project_id}")
    async def archive_project(project_id: str):
        """归档项目 (软删除)。"""
        if not project_manager:
            raise HTTPException(status_code=503, detail="项目管理器未初始化")
        if not project_manager.archive_project(project_id):
            raise HTTPException(status_code=404, detail="项目不存在")
        await broadcast_ws(state, "project_archived", {"project_id": project_id})
        return {"ok": True, "project_id": project_id}

    @router.get("/api/projects/{project_id}/usage")
    async def get_usage(project_id: str, limit: int = 100):
        """查询项目消费记录。"""
        if not project_manager:
            return {"records": [], "total": 0}
        records = db.get_usage_log(project_id, limit=limit)
        return {
            "records": records,
            "total": len(records),
            "project_id": project_id,
        }

    # ── MCP 网关端点 ─────────────────────────────────────────

    @router.get("/tools/list")
    async def list_all_tools(model: str = None):
        """列出网关上所有可用工具 (聚合所有 MCP Server)。

        Agent 初始化时调用此接口获取工具定义,
        填入 LLM 的 tools 参数。
        """
        if not mcp_gateway:
            return {"tools": [], "total": 0, "error": "MCP 网关未初始化"}
        tools = mcp_gateway.list_all_tools(model_type=model)
        return {
            "tools": tools,
            "total": len(tools),
            "servers": mcp_gateway.list_servers(),
        }

    @router.post("/tools/call")
    async def call_tool(payload: dict):
        """调用工具 — 路由到正确的 MCP Server 执行。

        请求体:
            {
                "tool_name": "read_file",
                "arguments": {"path": "/tmp/test.txt"},
                "server_name": null  // 可选,指定 Server
            }

        返回:
            {
                "content": [{"type": "text", "text": "..."}],
                "isError": false
            }
        """
        if not mcp_gateway:
            raise HTTPException(status_code=503, detail="MCP 网关未初始化")
        tool_name = payload.get("tool_name", "")
        arguments = payload.get("arguments", {})
        server_name = payload.get("server_name")
        if not tool_name:
            raise HTTPException(status_code=400, detail="缺少 tool_name")
        result = mcp_gateway.call_tool(tool_name, arguments, server_name)
        return result

    @router.get("/tools/servers")
    async def list_mcp_servers():
        """列出所有已注册的 MCP Server 及其状态。"""
        if not mcp_gateway:
            return {"servers": [], "total": 0}
        return {
            "servers": mcp_gateway.list_servers(),
            "stats": mcp_gateway.stats(),
        }

    @router.post("/tools/servers")
    async def register_mcp_server(payload: dict):
        """动态注册新的 MCP Server。"""
        if not mcp_gateway:
            raise HTTPException(status_code=503, detail="MCP 网关未初始化")
        name = payload.get("name", "")
        config = payload.get("config", {})
        if not name:
            raise HTTPException(status_code=400, detail="缺少 name")
        ok = mcp_gateway.register_server(name, config)
        return {"ok": ok, "name": name}

    @router.delete("/tools/servers/{name}")
    async def unregister_mcp_server(name: str):
        """注销 MCP Server。"""
        if not mcp_gateway:
            raise HTTPException(status_code=503, detail="MCP 网关未初始化")
        mcp_gateway.unregister_server(name)
        return {"ok": True, "name": name}

    # ── 模型路由 API (Phase 2) ───────────────────────────────────

    @router.post("/api/route/dry-run")
    async def route_dry_run(payload: dict):
        """模型路由决策预览 (dry-run)。

        输入任务描述, 返回路由器推荐模型、难度分级和评分详情。
        不会实际执行任务。
        """
        if not model_router:
            raise HTTPException(status_code=503, detail="模型路由器未加载")

        text = payload.get("text", payload.get("description", ""))
        skill = payload.get("skill", "")
        project_id = payload.get("project_id", "")

        routing = model_router.route(
            text=text,
            skill=skill,
            project_id=project_id,
        )
        return routing.to_dict()

    @router.get("/api/models")
    async def list_models():
        """返回模型池列表 (含可用状态)。"""
        if not model_router:
            return {"models": [], "message": "模型路由器未加载 (请配置 model_pool.yaml)"}
        return {"models": model_router.list_models()}

    # ── Station Director API (工作站主管) ─────────────────────────

    @router.get("/api/station/fleet")
    async def get_fleet():
        """舰队概览: 在线/离线/各评级分布。"""
        if not station_director:
            return {"error": "Station Director 未初始化"}
        return station_director.get_fleet_summary()

    @router.get("/api/station/hosts")
    async def get_station_hosts(min_tier: str = "D", online_only: bool = False):
        """所有主机列表 (含评级+状态), 可按评级筛选。"""
        if not station_director:
            return {"hosts": [], "error": "Station Director 未初始化"}
        if online_only:
            return {"hosts": station_director.get_hosts_by_tier(min_tier, online_only=True)}
        return {"hosts": station_director.get_all_hosts()}

    @router.get("/api/station/hosts/{device_id}/events")
    async def get_host_events(device_id: str, limit: int = 20):
        """单台主机出入站事件历史。"""
        if not station_director:
            return {"events": [], "error": "Station Director 未初始化"}
        return {"events": station_director.get_host_events(device_id, limit)}

    @router.get("/api/station/events")
    async def get_station_events(limit: int = 50):
        """最近全站事件流。"""
        if not station_director:
            return {"events": [], "error": "Station Director 未初始化"}
        return {"events": station_director.get_host_events(None, limit)}

    @router.post("/api/station/rate")
    async def recompute_ratings():
        """手动触发重新评级所有在线主机。"""
        if not station_director:
            raise HTTPException(status_code=503, detail="Station Director 未初始化")
        updated = station_director.recompute_ratings()
        return {"ok": True, "updated": updated}

    @router.get("/api/station/stats")
    async def get_station_stats():
        """统计摘要。"""
        if not station_director:
            return {"error": "Station Director 未初始化"}
        return station_director.get_fleet_summary()

    # WebSocket 实时推送
    @router.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket):
        """WebSocket 实时推送主机状态变更。"""
        await websocket.accept()
        state.ws_clients.add(websocket)
        try:
            # 首次推送当前状态
            hosts = db.list_hosts()
            await websocket.send_json({
                "type": "hosts",
                "data": [h.to_dict() for h in hosts],
            })
            # 保持连接,等待客户端心跳
            while True:
                try:
                    await asyncio.wait_for(websocket.receive_text(), timeout=30)
                except asyncio.TimeoutError:
                    await websocket.send_json({"type": "ping"})
        except WebSocketDisconnect:
            pass
        except Exception:
            pass
        finally:
            state.ws_clients.discard(websocket)

    return router


async def broadcast_ws(state, msg_type: str, data):
    """向所有 WebSocket 客户端广播消息。"""
    message = json.dumps({"type": msg_type, "data": data})
    dead = set()
    for ws in state.ws_clients:
        try:
            await ws.send_text(message)
        except Exception:
            dead.add(ws)
    state.ws_clients -= dead
