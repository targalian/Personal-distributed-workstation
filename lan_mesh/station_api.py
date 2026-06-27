"""
Station Director API 路由层

包含:
  - 基础路由 (始终可用): 主机注册/心跳/查询/Station Director/角色激活
  - Secretary 路由 (激活后可用): 任务/Agent/项目/MCP工具/模型路由
  - WebSocket 实时推送

设计要点:
  - 所有组件通过 controller 对象访问 (可变引用)
  - Secretary 路由检查 controller.secretary_active, 未激活时返回 503
  - 激活/停用 Secretary 无需重启服务
"""
import asyncio
import json
import time
from typing import Optional

import requests as http_requests

from fastapi import (
    APIRouter, WebSocket, WebSocketDisconnect, HTTPException,
    UploadFile, File,
)
from fastapi.responses import FileResponse

from .protocol import HostInfo, HostRecord, AgentCard


async def _broadcast(state, msg_type: str, data):
    """向所有 WebSocket 客户端广播消息。"""
    message = json.dumps({"type": msg_type, "data": data})
    dead = set()
    for ws in state.ws_clients:
        try:
            await ws.send_text(message)
        except Exception:
            dead.add(ws)
    state.ws_clients -= dead


def create_station_router(controller) -> APIRouter:
    """创建 Station Director 的完整 API 路由。

    Args:
        controller: StationController 实例 (持有所有可变状态)
    """
    router = APIRouter()

    # 便捷别名
    db = controller.db
    state = controller.state
    discovery = controller.discovery
    shared_folder = controller.state.shared_folder
    station_director = controller.station_director

    # ════════════════════════════════════════════════════════════
    #  角色激活端点
    # ════════════════════════════════════════════════════════════

    @router.post("/api/station/activate-secretary")
    async def activate_secretary():
        """激活 Secretary 模式 (同进程加载项目管理组件)。"""
        result = controller.activate_secretary()
        if result.get("ok"):
            controller.secretary_host_id = controller.state.device_id
            controller.secretary_host_port = controller.state.api_port
            await _broadcast(state, "secretary_assigned", {
                "device_id": controller.state.device_id,
                "port": controller.state.api_port,
            })
        await _broadcast(state, "secretary_activated", result)
        return result

    @router.post("/api/station/deactivate-secretary")
    async def deactivate_secretary():
        """停用 Secretary 模式。"""
        result = controller.deactivate_secretary()
        controller.secretary_host_id = None
        controller.secretary_host_port = None
        await _broadcast(state, "secretary_revoked", {"device_id": controller.state.device_id})
        await _broadcast(state, "secretary_deactivated", result)
        return result

    @router.get("/api/station/roles")
    async def get_roles():
        """查询当前激活的角色。"""
        return {
            "station": "active",
            "secretary": "active" if controller.secretary_active else "inactive",
            "components": {
                "project_manager": controller.project_manager is not None,
                "orchestrator": controller.orchestrator is not None,
                "model_router": controller.model_router is not None,
                "mcp_gateway": controller.mcp_gateway is not None,
            },
        }

    # ════════════════════════════════════════════════════════════
    #  Worker 注册 / 心跳 (始终可用)
    # ════════════════════════════════════════════════════════════

    @router.post("/api/register")
    async def register_worker(payload: dict):
        """接收 Worker 的注册请求 (完整 HostInfo)。"""
        info = HostInfo.from_dict(payload)
        record = station_director.on_host_registered(info)
        await _broadcast(state, "host_registered", record.to_dict())
        return {"ok": True, "device_id": info.device_id}

    @router.post("/api/heartbeat")
    async def heartbeat(payload: dict):
        """接收 Worker 心跳 (实时资源使用率)。"""
        device_id = payload.get("device_id", "")
        record = station_director.on_heartbeat(device_id, {
            "cpu_percent": payload.get("cpu_percent"),
            "memory_percent": payload.get("memory_percent"),
            "disk_percent": payload.get("disk_percent"),
            "shared_file_count": payload.get("shared_file_count"),
        })
        if not record:
            raise HTTPException(status_code=404, detail="设备未注册")
        await _broadcast(state, "heartbeat", record.to_dict())
        return {"ok": True}

    # ════════════════════════════════════════════════════════════
    #  主机 / 网络查询 (始终可用)
    # ════════════════════════════════════════════════════════════

    @router.get("/api/hosts")
    async def list_hosts():
        """返回所有注册主机列表 (DB 持久化 + UDP 发现合并)。"""
        db_hosts = db.list_hosts()
        discovery_devices = discovery.list_devices()
        db_ids = {h.device_id for h in db_hosts}
        for dev in discovery_devices:
            if dev["device_id"] not in db_ids:
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
        """返回本机网络状态。"""
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
            "role": "station",
            "secretary_active": controller.secretary_active,
            "uptime": time.time() - state.start_time,
            "device_id": state.device_id,
        }

    @router.get("/api/station-info")
    async def station_info():
        """返回 Station Director 自身的主机信息。"""
        return controller._collect_info().to_dict()

    @router.get("/api/shared")
    async def list_shared():
        """列出共享文件夹内容。"""
        return {
            "folder": str(shared_folder.path),
            "files": shared_folder.list_files(),
            "file_count": shared_folder.file_count(),
        }

    # ════════════════════════════════════════════════════════════
    #  Station Director API (始终可用)
    # ════════════════════════════════════════════════════════════

    @router.get("/api/station/fleet")
    async def get_fleet():
        """舰队概览: 在线/离线/各评级分布 + 主机列表。"""
        summary = station_director.get_fleet_summary()
        summary["hosts"] = station_director.get_all_hosts()
        return summary

    @router.get("/api/station/hosts")
    async def get_station_hosts(min_tier: str = "D", online_only: bool = False):
        """所有主机列表 (含评级+状态), 可按评级筛选。"""
        if online_only:
            return {"hosts": station_director.get_hosts_by_tier(min_tier, online_only=True)}
        return {"hosts": station_director.get_all_hosts()}

    @router.get("/api/station/hosts/{device_id}/events")
    async def get_host_events(device_id: str, limit: int = 20):
        """单台主机出入站事件历史。"""
        return {"events": station_director.get_host_events(device_id, limit)}

    @router.get("/api/station/events")
    async def get_station_events(limit: int = 50):
        """最近全站事件流。"""
        return {"events": station_director.get_host_events(None, limit)}

    @router.post("/api/station/rate")
    async def recompute_ratings():
        """手动触发重新评级所有在线主机。"""
        updated = station_director.recompute_ratings()
        return {"ok": True, "updated": updated}

    @router.get("/api/station/stats")
    async def get_station_stats():
        """统计摘要。"""
        return station_director.get_fleet_summary()

    # ── 远程 Secretary 分配 (指定主机运行秘书) ───────────────────────

    @router.post("/api/station/hosts/{device_id}/assign-secretary")
    async def assign_secretary_to_host(device_id: str, payload: dict = None):
        """指定主机运行 Secretary。

        - 本机 (Station Director): 进程内激活
        - 远程主机: 发送 HTTP 到 Worker, 启动 Secretary 子进程
        """
        host = db.get_host(device_id)
        if not host:
            raise HTTPException(status_code=404, detail="主机不存在")
        if not host.online:
            raise HTTPException(status_code=400, detail="主机离线,无法分配")

        port = (payload or {}).get("port")

        # 本机 → 进程内激活
        if device_id == controller.state.device_id:
            result = controller.activate_secretary()
            if result.get("ok"):
                controller.secretary_host_id = device_id
                controller.secretary_host_port = controller.state.api_port
            return result

        # 远程主机 → HTTP 调用 Worker
        if not host.ip:
            raise HTTPException(status_code=400, detail="主机 IP 未知")

        try:
            resp = http_requests.post(
                f"http://{host.ip}:{host.api_port}/role/start-secretary",
                json={"port": port} if port else {},
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                controller.secretary_host_id = device_id
                controller.secretary_host_port = data.get("port")
                await _broadcast(state, "secretary_assigned", {
                    "device_id": device_id, "port": data.get("port")
                })
                return data
            else:
                detail = resp.json().get("detail", "远程启动失败")
                raise HTTPException(status_code=resp.status_code, detail=detail)
        except http_requests.RequestException as e:
            raise HTTPException(status_code=502, detail=f"无法连接到主机 {host.ip}:{host.api_port}: {e}")

    @router.post("/api/station/hosts/{device_id}/revoke-secretary")
    async def revoke_secretary_from_host(device_id: str):
        """撤销主机的 Secretary 角色。

        - 本机: 进程内停用
        - 远程主机: 发送 HTTP 到 Worker, 停止 Secretary 子进程
        """
        host = db.get_host(device_id)
        if not host:
            raise HTTPException(status_code=404, detail="主机不存在")

        # 本机 → 进程内停用
        if device_id == controller.state.device_id:
            result = controller.deactivate_secretary()
            controller.secretary_host_id = None
            controller.secretary_host_port = None
            await _broadcast(state, "secretary_revoked", {"device_id": device_id})
            return result

        # 远程主机 → HTTP 调用 Worker
        try:
            resp = http_requests.post(
                f"http://{host.ip}:{host.api_port}/role/stop-secretary",
                timeout=10,
            )
            if resp.status_code == 200:
                controller.secretary_host_id = None
                controller.secretary_host_port = None
                await _broadcast(state, "secretary_revoked", {"device_id": device_id})
                return resp.json()
            else:
                return resp.json()
        except http_requests.RequestException as e:
            raise HTTPException(status_code=502, detail=f"无法连接到主机: {e}")

    @router.get("/api/station/secretary-status")
    async def get_secretary_status():
        """查询当前 Secretary 分配状态。

        返回哪台主机正在运行 Secretary, 以及其端口。
        """
        host_id = getattr(controller, 'secretary_host_id', None)
        host_port = getattr(controller, 'secretary_host_port', None)
        host_name = None
        if host_id:
            host = db.get_host(host_id)
            if host:
                host_name = host.device_name

        return {
            "secretary_host_id": host_id,
            "secretary_host_name": host_name,
            "secretary_port": host_port,
            "active": host_id is not None,
            "is_local": host_id == controller.state.device_id if host_id else False,
        }

    @router.get("/api/station/hosts/{device_id}/role")
    async def get_host_role(device_id: str):
        """查询指定主机的角色状态 (含远程 Secretary 子进程状态)。"""
        host = db.get_host(device_id)
        if not host:
            raise HTTPException(status_code=404, detail="主机不存在")

        # 本机
        if device_id == controller.state.device_id:
            return {
                "device_id": device_id,
                "is_station": True,
                "secretary_active": controller.secretary_active,
                "secretary_in_process": True,
            }

        # 远程主机 → 查询 Worker
        secretary_assigned = (getattr(controller, 'secretary_host_id', None) == device_id)
        remote_status = {"running": False}
        if host.online and host.ip:
            try:
                resp = http_requests.get(
                    f"http://{host.ip}:{host.api_port}/role/status",
                    timeout=5,
                )
                if resp.status_code == 200:
                    remote_status = resp.json()
            except http_requests.RequestException:
                pass

        return {
            "device_id": device_id,
            "is_station": False,
            "secretary_assigned": secretary_assigned,
            "secretary_running": remote_status.get("running", False),
            "secretary_port": remote_status.get("port"),
            "secretary_pid": remote_status.get("pid"),
        }

    # ════════════════════════════════════════════════════════════
    #  Secretary 路由 (激活后可用, 未激活返回 503)
    # ════════════════════════════════════════════════════════════

    def _check_secretary():
        """检查 Secretary 是否激活, 未激活抛出 503。"""
        if not controller.secretary_active:
            raise HTTPException(
                status_code=503,
                detail="Secretary 未激活,请在 Station 面板中点击「启动秘书」"
            )

    # ── Agent 管理 ──

    @router.post("/api/agents/register")
    async def register_agent(payload: dict):
        _check_secretary()
        card = AgentCard.from_dict(payload)
        card.last_seen = time.time()
        dev = discovery.find_device(card.device_id)
        if dev:
            card.ip = dev.get("ip", card.ip)
        db.upsert_agent(card)
        await _broadcast(state, "agent_registered", card.to_dict())
        return {"ok": True, "agent_id": card.agent_id}

    @router.get("/api/agents")
    async def list_agents(status: str = None):
        _check_secretary()
        agents = db.list_agents(status=status)
        return {
            "agents": [a.to_dict() for a in agents],
            "total": len(agents),
            "idle": sum(1 for a in agents if a.status == "idle"),
            "busy": sum(1 for a in agents if a.status == "busy"),
        }

    @router.get("/api/agents/{agent_id}")
    async def get_agent(agent_id: str):
        _check_secretary()
        agent = db.get_agent(agent_id)
        if not agent:
            raise HTTPException(status_code=404, detail="Agent 不存在")
        return agent.to_dict()

    # ── 任务管理 ──

    @router.post("/api/tasks")
    async def submit_task(payload: dict):
        _check_secretary()
        orchestrator = controller.orchestrator
        project_manager = controller.project_manager
        project_id = payload.get("project_id", "")
        if project_id and project_manager:
            if not project_manager.check_budget(project_id):
                raise HTTPException(status_code=402, detail="项目预算已起支或已暂停,无法提交任务")
        task = orchestrator.submit_task(
            name=payload.get("name", ""),
            description=payload.get("description", ""),
            input_data=payload.get("input_data", {}),
            created_by=payload.get("created_by", "user"),
            project_id=project_id,
        )
        await _broadcast(state, "task_submitted", task.to_dict())
        return task.to_dict()

    @router.get("/api/tasks")
    async def list_tasks(status: str = None, limit: int = 50):
        _check_secretary()
        tasks = db.list_tasks(status=status, limit=limit)
        return {"tasks": [t.to_dict() for t in tasks], "total": len(tasks)}

    @router.get("/api/tasks/{task_id}")
    async def get_task(task_id: str):
        _check_secretary()
        task = db.get_task(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="任务不存在")
        return task.to_dict()

    # ── 项目管理 ──

    @router.post("/api/projects")
    async def create_project(payload: dict):
        _check_secretary()
        project_manager = controller.project_manager
        project = project_manager.create_project(
            name=payload.get("name", ""),
            description=payload.get("description", ""),
            budget_limit_usd=payload.get("budget_limit_usd", 10.0),
            allowed_models=payload.get("allowed_models", []),
            routing_strategy=payload.get("routing_strategy", "balanced"),
            workspace_base=payload.get("workspace_base", ""),
        )
        await _broadcast(state, "project_created", project.to_dict())
        return project.to_dict()

    @router.get("/api/projects")
    async def list_projects(status: str = None):
        if not controller.secretary_active:
            return {"projects": [], "total": 0}
        project_manager = controller.project_manager
        projects = project_manager.list_projects(status=status)
        return {"projects": [p.to_dict() for p in projects], "total": len(projects)}

    @router.get("/api/projects/{project_id}")
    async def get_project(project_id: str):
        _check_secretary()
        project_manager = controller.project_manager
        status_info = project_manager.get_project_status(project_id)
        if not status_info:
            raise HTTPException(status_code=404, detail="项目不存在")
        return status_info

    @router.put("/api/projects/{project_id}")
    async def update_project(project_id: str, payload: dict):
        _check_secretary()
        project_manager = controller.project_manager
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
        await _broadcast(state, "project_updated", project.to_dict())
        return project.to_dict()

    @router.delete("/api/projects/{project_id}")
    async def archive_project(project_id: str):
        _check_secretary()
        project_manager = controller.project_manager
        if not project_manager.archive_project(project_id):
            raise HTTPException(status_code=404, detail="项目不存在")
        await _broadcast(state, "project_archived", {"project_id": project_id})
        return {"ok": True, "project_id": project_id}

    @router.get("/api/projects/{project_id}/usage")
    async def get_usage(project_id: str, limit: int = 100):
        if not controller.secretary_active:
            return {"records": [], "total": 0}
        records = db.get_usage_log(project_id, limit=limit)
        return {"records": records, "total": len(records), "project_id": project_id}

    # ── MCP 工具网关 ──

    @router.get("/tools/list")
    async def list_all_tools(model: str = None):
        if not controller.secretary_active or not controller.mcp_gateway:
            return {"tools": [], "total": 0, "error": "Secretary 未激活或 MCP 网关未初始化"}
        tools = controller.mcp_gateway.list_all_tools(model_type=model)
        return {
            "tools": tools,
            "total": len(tools),
            "servers": controller.mcp_gateway.list_servers(),
        }

    @router.post("/tools/call")
    async def call_tool(payload: dict):
        _check_secretary()
        mcp_gateway = controller.mcp_gateway
        tool_name = payload.get("tool_name", "")
        arguments = payload.get("arguments", {})
        server_name = payload.get("server_name")
        if not tool_name:
            raise HTTPException(status_code=400, detail="缺少 tool_name")
        result = mcp_gateway.call_tool(tool_name, arguments, server_name)
        return result

    @router.get("/tools/servers")
    async def list_mcp_servers():
        if not controller.secretary_active or not controller.mcp_gateway:
            return {"servers": [], "total": 0}
        return {"servers": controller.mcp_gateway.list_servers(), "stats": controller.mcp_gateway.stats()}

    @router.post("/tools/servers")
    async def register_mcp_server(payload: dict):
        _check_secretary()
        name = payload.get("name", "")
        config = payload.get("config", {})
        if not name:
            raise HTTPException(status_code=400, detail="缺少 name")
        ok = controller.mcp_gateway.register_server(name, config)
        return {"ok": ok, "name": name}

    @router.delete("/tools/servers/{name}")
    async def unregister_mcp_server(name: str):
        _check_secretary()
        controller.mcp_gateway.unregister_server(name)
        return {"ok": True, "name": name}

    # ── 模型路由 ──

    @router.post("/api/route/dry-run")
    async def route_dry_run(payload: dict):
        _check_secretary()
        model_router = controller.model_router
        if not model_router:
            raise HTTPException(status_code=503, detail="模型路由器未加载")
        text = payload.get("text", payload.get("description", ""))
        skill = payload.get("skill", "")
        project_id = payload.get("project_id", "")
        routing = model_router.route(text=text, skill=skill, project_id=project_id)
        return routing.to_dict()

    @router.get("/api/models")
    async def list_models():
        if not controller.secretary_active or not controller.model_router:
            return {"models": [], "message": "Secretary 未激活或模型路由器未加载"}
        return {"models": controller.model_router.list_models()}

    # ════════════════════════════════════════════════════════════
    #  WebSocket 实时推送
    # ════════════════════════════════════════════════════════════

    @router.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket):
        """WebSocket 实时推送主机状态变更。"""
        await websocket.accept()
        state.ws_clients.add(websocket)
        try:
            hosts = db.list_hosts()
            await websocket.send_json({
                "type": "hosts",
                "data": [h.to_dict() for h in hosts],
            })
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
