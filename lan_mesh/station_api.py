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
import uuid as _uuid
from typing import Optional

import requests as http_requests

from fastapi import (
    APIRouter, WebSocket, WebSocketDisconnect, HTTPException,
    UploadFile, File, Form,
)
from fastapi.responses import FileResponse

from .protocol import HostInfo, HostRecord, AgentCard, Task
from .host_rating import rate_host


def _merge_db_and_udp_hosts(db, discovery):
    """合并 DB 主机列表与 UDP 发现设备 (DB 为主, 补充 UDP-only 设备)。"""
    db_hosts = db.list_hosts()
    discovery_devices = discovery.list_devices()
    db_ids = {h.device_id for h in db_hosts}
    merged_count = 0
    for dev in discovery_devices:
        if dev["device_id"] in db_ids:
            continue
        rating = rate_host(
            dev.get("cpu_count", 0),
            dev.get("memory_total_mb", 0),
            dev.get("disk_total_gb", 0),
        )
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
            rating_tier=rating.tier,
            rating_score=rating.score,
            rating_summary=rating.summary,
        ))
        merged_count += 1
    if merged_count:
        print(f"[Station] 合并列表: 补充 {merged_count} 台 UDP-only 设备")
    return db_hosts


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
        controller.bot_gateway.notify("secretary_activated", {"port": controller.state.api_port})
        return result

    @router.post("/api/station/deactivate-secretary")
    async def deactivate_secretary():
        """停用 Secretary 模式。"""
        result = controller.deactivate_secretary()
        controller.secretary_host_id = None
        controller.secretary_host_port = None
        await _broadcast(state, "secretary_revoked", {"device_id": controller.state.device_id})
        await _broadcast(state, "secretary_deactivated", result)
        controller.bot_gateway.notify("secretary_deactivated", {})
        return result

    @router.get("/api/station/roles")
    async def get_roles():
        """查询当前激活的角色。"""
        return {
            "station": "active",
            "secretary": "active" if controller.secretary_active else "inactive",
            "components": {
                "project_manager": controller.project_manager is not None,
                "chat_handler": controller.chat_handler is not None,
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
        controller.bot_gateway.notify("host_online", {"device_name": record.device_name or record.hostname or "未知", "ip": record.ip or ""})
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
        all_hosts = _merge_db_and_udp_hosts(db, discovery)
        return {
            "hosts": [h.to_dict() for h in all_hosts],
            "total": len(all_hosts),
            "online": sum(1 for h in all_hosts if h.online),
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
        """舰队概览: 在线/离线/各评级分布 + 主机列表 (DB + UDP 合并)。"""
        all_hosts = _merge_db_and_udp_hosts(db, discovery)
        online_count = sum(1 for h in all_hosts if h.online)
        offline_count = len(all_hosts) - online_count
        tiers = {"S": 0, "A": 0, "B": 0, "C": 0, "D": 0}
        for h in all_hosts:
            if h.rating_tier in tiers:
                tiers[h.rating_tier] += 1
        return {
            "total": len(all_hosts),
            "online": online_count,
            "offline": offline_count,
            "tiers": tiers,
            "hosts": [h.to_dict() for h in all_hosts],
        }

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
        """提交新任务 — 创建 Task 记录并分配 PM Agent。

        新流程 (PM Agent 架构):
        1. 创建 Task 记录 (status=pending, pm_agent_id="")
        2. 选择合适 work_station (按评级/在线状态)
        3. POST 到目标 Worker /role/start-pm (携带 task_id, secretary_url)
        4. 更新 Task.pm_agent_id, status=running
        5. WebSocket 广播 task_submitted + pm_registered
        """
        _check_secretary()
        project_manager = controller.project_manager
        project_id = payload.get("project_id", "")
        if project_id and project_manager:
            if not project_manager.check_budget(project_id):
                raise HTTPException(status_code=402, detail="项目预算已起支或已暂停,无法提交任务")

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
        db.save_task(task)
        await _broadcast(state, "task_submitted", task.to_dict())
        controller.bot_gateway.notify("task_submitted", {
            "name": task.name, "task_id": task.task_id[:8],
        })

        # 2. 选择合适的 work_station (按评级排序, 取在线最高评级)
        from .protocol import PMAgent
        hosts = db.list_hosts()
        online_hosts = [h for h in hosts if h.online and h.device_id != state.device_id]
        # 如果没有在线 Worker, 使用本机
        if not online_hosts:
            online_hosts = [h for h in hosts if h.online]

        if not online_hosts:
            task.status = "failed"
            task.output_data = {"error": "无可用 work_station"}
            db.save_task(task)
            return task.to_dict()

        # 按评级排序 (S > A > B > C > D)
        tier_order = {"S": 5, "A": 4, "B": 3, "C": 2, "D": 1, "": 0}
        online_hosts.sort(
            key=lambda h: tier_order.get(h.rating_tier, 0),
            reverse=True,
        )
        target_host = online_hosts[0]

        # 3. 构造 Secretary URL
        secretary_url = f"http://{state.device_id}:{state.api_port}"
        # 使用本机 IP 更准确
        local_ips = controller._collect_info().ip_addresses
        if local_ips:
            secretary_url = f"http://{local_ips[0]}:{state.api_port}"

        # 4. POST 到目标 Worker 启动 PM Agent
        try:
            resp = http_requests.post(
                f"http://{target_host.ip}:{target_host.api_port}/role/start-pm",
                json={
                    "task_id": task.task_id,
                    "secretary_url": secretary_url,
                    "task_data": task.to_dict(),
                },
                timeout=15,
            )
            if resp.status_code == 200:
                pm_data = resp.json()
                pm_id = pm_data.get("pm_id", "")
                # 5. 更新 Task
                task.pm_agent_id = pm_id
                task.status = "running"
                db.save_task(task)

                # 在 DB 中创建 PM Agent 记录
                pm_agent = PMAgent(
                    pm_id=pm_id,
                    agent_name=f"PM-{pm_id[:8]}",
                    task_id=task.task_id,
                    project_id=project_id,
                    device_id=target_host.device_id,
                    hostname=target_host.hostname or target_host.device_name,
                    ip=target_host.ip,
                    api_port=target_host.api_port,
                    status="starting",
                )
                db.upsert_pm_agent(pm_agent)
                # 优化7: 记录 PM→Worker 映射
                controller._pm_worker_map[pm_id] = {
                    "ip": target_host.ip,
                    "api_port": target_host.api_port,
                    "device_id": target_host.device_id,
                }

                await _broadcast(state, "pm_registered", {
                    "pm_id": pm_id,
                    "task_id": task.task_id,
                    "device_id": target_host.device_id,
                    "device_name": target_host.device_name,
                })
                controller.bot_gateway.notify("pm_registered", {
                    "pm_id": pm_id[:12], "task": task.name,
                    "station": target_host.device_name or target_host.hostname,
                })
            else:
                task.status = "failed"
                task.output_data = {"error": f"PM 启动失败: {resp.text}"}
                db.save_task(task)
        except Exception as e:
            task.status = "failed"
            task.output_data = {"error": f"PM 启动异常: {e}"}
            db.save_task(task)

        await _broadcast(state, "task_updated", task.to_dict())
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

    # ── 优化8: 取消/暂停任务 ──

    @router.post("/api/tasks/{task_id}/cancel")
    async def cancel_task(task_id: str):
        """取消指定任务及其 PM Agent。"""
        _check_secretary()
        result = controller.cancel_task(task_id)
        if result.get("ok"):
            await _broadcast(state, "task_cancelled", {
                "task_id": task_id, "message": result.get("message", ""),
            })
            return result
        raise HTTPException(status_code=409, detail=result.get("message", "取消失败"))

    @router.post("/api/tasks/{task_id}/pause")
    async def pause_task(task_id: str):
        """暂停指定任务及其 PM Agent。"""
        _check_secretary()
        result = controller.pause_task(task_id)
        if result.get("ok"):
            await _broadcast(state, "task_paused", {
                "task_id": task_id, "message": result.get("message", ""),
            })
            return result
        raise HTTPException(status_code=409, detail=result.get("message", "暂停失败"))

    # ── Graph Engine: DAG 图结构 / Checkpoint / 断点恢复 ──

    @router.get("/api/tasks/{task_id}/graph")
    async def get_task_graph(task_id: str):
        """获取任务的 DAG 图结构 JSON (供前端 SVG 渲染)。

        返回: {nodes: [{id, name, status, skill, x, y, ...}], edges: [{source, target, condition}]}
        """
        _check_secretary()
        orchestrator = controller.orchestrator
        if not orchestrator:
            raise HTTPException(status_code=503, detail="编排器未初始化")
        graph = orchestrator.get_task_graph(task_id)
        if not graph:
            raise HTTPException(status_code=404, detail="任务无 DAG 图数据")
        return graph

    @router.put("/api/tasks/{task_id}/graph")
    async def update_task_graph(task_id: str, payload: dict):
        """保存编辑后的 DAG 图结构 (前端图编辑器保存)。

        接收: {nodes: [...], edges: [...]}
        验证: 环检测, 失败则拒绝保存。
        """
        _check_secretary()
        orchestrator = controller.orchestrator
        if not orchestrator:
            raise HTTPException(status_code=503, detail="编排器未初始化")
        result = orchestrator.update_task_graph(task_id, payload)
        if result.get("ok"):
            await _broadcast(state, "task_graph_updated", {
                "task_id": task_id, "message": result.get("message", ""),
            })
            return result
        raise HTTPException(status_code=409, detail=result.get("message", "保存失败"))

    @router.post("/api/tasks/{task_id}/resume")
    async def resume_task(task_id: str):
        """从 checkpoint 恢复任务执行。"""
        _check_secretary()
        orchestrator = controller.orchestrator
        if not orchestrator:
            raise HTTPException(status_code=503, detail="编排器未初始化")
        success = orchestrator.resume_task(task_id)
        if success:
            await _broadcast(state, "task_resumed", {
                "task_id": task_id, "message": "任务已从 checkpoint 恢复",
            })
            return {"ok": True, "message": "任务已恢复执行"}
        raise HTTPException(status_code=409, detail="无可用 checkpoint 或恢复失败")

    @router.get("/api/tasks/{task_id}/checkpoints")
    async def list_checkpoints(task_id: str):
        """查看任务的 checkpoint 列表。"""
        _check_secretary()
        checkpoints = db.list_checkpoints(task_id)
        return {"checkpoints": checkpoints, "total": len(checkpoints)}

    @router.get("/api/tasks/{task_id}/graph-state")
    async def get_graph_state(task_id: str):
        """获取任务的当前状态机状态 (phase + history)。"""
        _check_secretary()
        orchestrator = controller.orchestrator
        if not orchestrator:
            raise HTTPException(status_code=503, detail="编排器未初始化")
        gs = orchestrator.get_graph_state(task_id)
        if not gs:
            raise HTTPException(status_code=404, detail="任务无图状态数据")
        return gs

    # ── 优化7: 反向沟通 — PM 注入回复 ──

    @router.post("/api/pm/{pm_id}/inject-input")
    async def inject_pm_input(pm_id: str, payload: dict):
        """向 PM Agent 注入来自 Boss/秘书的回复 (反向沟通通道)。

        当 PM 上报 awaiting_input 状态后, 秘书/Web UI 可通过此端点
        将 Boss 的决策回复注入到 PM Agent。
        """
        _check_secretary()
        result = controller.inject_input_to_pm(pm_id, payload)
        if result.get("ok"):
            await _broadcast(state, "pm_input_injected", {
                "pm_id": pm_id, "message": "回复已注入 PM Agent",
            })
            return result
        raise HTTPException(status_code=409, detail=result.get("message", "注入失败"))

    # ── 优化9: 交付闭环 ──

    @router.post("/api/pm/{pm_id}/deliver")
    async def receive_pm_delivery(pm_id: str, payload: dict):
        """接收 PM Agent 的最终交付物 (任务完成后调用)。

        存储交付物 → WebSocket 广播 → Bot 推送 → 聊天窗口通知 Boss。
        Boss 可随后「验收」或「退回」。
        """
        _check_secretary()
        task_id = payload.get("task_id", "")
        task_name = payload.get("task_name", "")
        deliverable = payload.get("deliverable", "")
        summary = payload.get("summary", "")
        subtask_stats = payload.get("subtask_stats", {})

        # 存储交付物到 DB (追加到 task output_data)
        if task_id:
            task = db.get_task(task_id)
            if task:
                if not task.output_data:
                    task.output_data = {}
                task.output_data["_delivery"] = {
                    "pm_id": pm_id,
                    "deliverable": deliverable,
                    "summary": summary,
                    "subtask_stats": subtask_stats,
                    "delivered_at": payload.get("delivered_at", time.time()),
                    "accepted": None,  # 等待 Boss 验收
                }
                db.save_task(task)

        # WebSocket 广播
        await _broadcast(state, "task_delivered", {
            "pm_id": pm_id,
            "task_id": task_id,
            "task_name": task_name,
            "summary": summary,
            "subtask_stats": subtask_stats,
        })

        # Bot 推送
        controller.bot_gateway.notify("task_delivered", {
            "name": task_name, "task_id": task_id[:12] if task_id else "",
        })

        print(f"[Station] PM {pm_id[:12]} 交付物已接收: {task_name}")
        return {"ok": True, "task_id": task_id, "message": "交付物已接收, 等待 Boss 验收"}

    # ── 优化14: 任务记忆 ──

    @router.post("/api/pm/{pm_id}/task-memory")
    async def receive_task_memory(pm_id: str, payload: dict):
        """接收 PM Agent 的任务记忆 (任务完成后调用)。

        存储到 task_memory 表, 用于未来同类任务的决策参考:
        - 推荐协作模式
        - 历史成功率
        - 常见错误预警
        """
        _check_secretary()
        db.save_task_memory(
            task_name=payload.get("task_name", ""),
            task_keywords=payload.get("task_keywords", []),
            task_type=payload.get("task_type", "general"),
            collaboration_mode=payload.get("collaboration_mode", ""),
            team_size=payload.get("team_size", 0),
            duration_secs=payload.get("duration_secs", 0),
            success=payload.get("success", True),
            error_pattern=payload.get("error_pattern", ""),
            boss_feedback=payload.get("boss_feedback", ""),
            device_id=payload.get("device_id", ""),
        )
        print(f"[Station] PM {pm_id[:12]} 任务记忆已存储 (type={payload.get('task_type', '')})")
        return {"ok": True, "message": "任务记忆已存储"}

    @router.get("/api/task-memory/stats")
    async def get_task_memory_stats(task_type: str = ""):
        """查询任务记忆统计 (成功率、推荐模式等)。"""
        _check_secretary()
        stats = db.get_task_memory_stats(task_type=task_type)
        return stats

    # ── PM Agent 管理 ──

    @router.get("/api/pm")
    async def list_pm_agents(status: str = None):
        """列出所有 PM Agent 及其状态。"""
        _check_secretary()
        agents = db.list_pm_agents(status=status)
        return {
            "pm_agents": [a.to_dict() if hasattr(a, 'to_dict') else a for a in agents],
            "total": len(agents),
        }

    @router.get("/api/pm/{pm_id}")
    async def get_pm_agent(pm_id: str):
        """PM Agent 详情 (含 team_structure)。"""
        _check_secretary()
        pm = db.get_pm_agent(pm_id)
        if not pm:
            raise HTTPException(status_code=404, detail="PM Agent 不存在")
        result = pm.to_dict() if hasattr(pm, 'to_dict') else pm
        # 附加团队信息
        teams = db.get_teams_by_pm(pm_id)
        result["teams"] = [t.to_dict() if hasattr(t, 'to_dict') else t for t in teams]
        return result

    @router.get("/api/pm/{pm_id}/teams")
    async def get_pm_teams(pm_id: str):
        """PM 下属团队列表。"""
        _check_secretary()
        teams = db.get_teams_by_pm(pm_id)
        return {
            "teams": [t.to_dict() if hasattr(t, 'to_dict') else t for t in teams],
            "total": len(teams),
        }

    @router.get("/api/pm/{pm_id}/progress")
    async def get_pm_progress(pm_id: str, limit: int = 50):
        """PM 进度报告列表。"""
        _check_secretary()
        reports = db.get_progress_reports(pm_id, limit=limit)
        return {"reports": reports, "total": len(reports)}

    @router.post("/api/pm/{pm_id}/status")
    async def update_pm_status(pm_id: str, payload: dict):
        """PM 上报状态变更 (Worker 调用)。"""
        _check_secretary()
        pm = db.get_pm_agent(pm_id)
        if not pm:
            raise HTTPException(status_code=404, detail="PM Agent 不存在")

        status = payload.get("status", "")
        team_structure = payload.get("team_structure")
        task_list = payload.get("task_list")
        collaboration_mode = payload.get("collaboration_mode")

        db.update_pm_status(
            pm_id, status,
            team_structure=team_structure,
            task_list=task_list,
            collaboration_mode=collaboration_mode,
        )

        # 优化7: awaiting_input 状态时附带澄清问题
        broadcast_data = {
            "pm_id": pm_id, "status": status,
            "collaboration_mode": collaboration_mode or "",
        }
        clarification_question = payload.get("clarification_question", "")
        if clarification_question:
            broadcast_data["clarification_question"] = clarification_question
            broadcast_data["clarification_options"] = payload.get("clarification_options", [])

        # 优化10: escalated 状态时附带升级详情
        escalation = payload.get("escalation")
        if escalation:
            broadcast_data["escalation"] = escalation

        await _broadcast(state, "pm_status_change", broadcast_data)

        # 优化10: escalated 时额外广播 + Bot 推送
        if status == "escalated" and escalation:
            await _broadcast(state, "task_escalated", escalation)
            controller.bot_gateway.notify("task_escalated", {
                "task_name": escalation.get("task_name", ""),
                "failed_subtask": escalation.get("failed_subtask", ""),
                "error": escalation.get("error", "")[:200],
            })
        return {"ok": True, "pm_id": pm_id, "status": status}

    @router.post("/api/pm/{pm_id}/progress")
    async def receive_pm_progress(pm_id: str, payload: dict):
        """PM 上报进度 (Worker 调用)。"""
        _check_secretary()
        from .protocol import ProgressReport
        report = ProgressReport(
            report_id=f"rpt-{_uuid.uuid4().hex[:8]}",
            pm_id=pm_id,
            reporter_id=payload.get("reporter_id", ""),
            reporter_type=payload.get("reporter_type", "pm"),
            task_name=payload.get("task_name", ""),
            progress=payload.get("progress", 0.0),
            status=payload.get("status", "in_progress"),
            message=payload.get("message", ""),
            timestamp=payload.get("timestamp", time.time()),
        )
        db.save_progress_report(report)

        await _broadcast(state, "progress_report", {
            "pm_id": pm_id,
            "reporter_id": report.reporter_id,
            "progress": report.progress,
            "status": report.status,
            "message": report.message,
            "task_name": report.task_name,
        })
        return {"ok": True, "report_id": report.report_id}

    # ── 团队管理 ──

    @router.get("/api/teams")
    async def list_teams(pm_id: str = None):
        """所有团队 (支持按 pm_id 过滤)。"""
        _check_secretary()
        teams = db.list_teams(pm_id=pm_id)
        return {
            "teams": [t.to_dict() if hasattr(t, 'to_dict') else t for t in teams],
            "total": len(teams),
        }

    @router.get("/api/teams/{team_id}")
    async def get_team(team_id: str):
        """团队详情 (含 members 嵌套)。"""
        _check_secretary()
        team = db.get_team(team_id)
        if not team:
            raise HTTPException(status_code=404, detail="团队不存在")
        return team.to_dict() if hasattr(team, 'to_dict') else team

    # ── 秘书聊天 ──

    @router.post("/api/secretary/chat")
    async def secretary_chat(payload: dict):
        """与秘书对话 — 处理用户消息并返回回复。"""
        _check_secretary()
        chat_handler = getattr(controller, 'chat_handler', None)
        if not chat_handler:
            raise HTTPException(status_code=503, detail="聊天处理器未初始化")
        message = payload.get("message", "")
        if not message:
            raise HTTPException(status_code=400, detail="消息不能为空")
        history = payload.get("history")
        result = chat_handler.chat(message, history)
        await _broadcast(state, "chat_reply", result)
        return result

    @router.get("/api/secretary/chat/history")
    async def secretary_chat_history(limit: int = 50):
        """返回最近的聊天历史。"""
        _check_secretary()
        chat_handler = getattr(controller, 'chat_handler', None)
        if not chat_handler:
            return {"history": [], "total": 0}
        history = chat_handler.get_history(limit)
        return {"history": history, "total": len(history)}

    @router.delete("/api/secretary/chat/history")
    async def secretary_chat_history_clear():
        """清空秘书聊天历史 (内存 + DB 持久化记录)。"""
        _check_secretary()
        chat_handler = getattr(controller, 'chat_handler', None)
        if not chat_handler:
            return {"ok": True, "message": "聊天处理器未初始化"}
        chat_handler.clear_history()
        return {"ok": True, "message": "聊天历史已清空"}

    # ── 优化15: Bot 统一消息入口 ──

    @router.post("/api/bot/message")
    async def bot_message_ingress(payload: dict):
        """优化15: Bot 消息统一入口 (Webhook 模式)。

        用于 Telegram Webhook 或其他 Bot 平台回调:
        - 接收外部 Bot 平台推送的用户消息
        - 转发给 ChatHandler 处理 (与 Web 聊天相同逻辑)
        - 返回回复内容, 由调用方推送给用户

        Payload:
            {"message": "...", "chat_id": "...", "platform": "telegram"}
        """
        _check_secretary()
        chat_handler = getattr(controller, 'chat_handler', None)
        if not chat_handler:
            raise HTTPException(status_code=503, detail="秘书未激活")

        message = payload.get("message", "").strip()
        if not message:
            raise HTTPException(status_code=400, detail="消息不能为空")

        chat_id = payload.get("chat_id", "")
        platform = payload.get("platform", "unknown")
        print(f"[Station] Bot 消息入口 ({platform}): {message[:50]} (from {chat_id})")

        # 统一转发给 ChatHandler
        result = chat_handler.chat(message)

        # 广播到 Web UI (显示 Bot 来源的对话)
        await _broadcast(state, "bot_chat", {
            "platform": platform,
            "chat_id": chat_id,
            "message": message,
            "reply": result.get("reply", ""),
        })

        return {
            "ok": True,
            "reply": result.get("reply", ""),
            "action_taken": result.get("action_taken", ""),
        }

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
    #  技能库管理 (Skill Registry)
    # ════════════════════════════════════════════════════════════

    @router.get("/api/station/skills")
    async def list_skills(category: str = None):
        """列出所有已注册技能，可按分类过滤。"""
        return [s.to_dict() for s in controller.skill_registry.list_skills(category)]

    @router.get("/api/station/skills/stats")
    async def skill_stats():
        """返回技能库统计信息。"""
        return controller.skill_registry.stats()

    @router.get("/api/station/skills/scan")
    async def scan_skills():
        """手动触发扫描注册新技能。"""
        result = controller.skill_registry.scan_and_register()
        await _broadcast(state, "skills_scanned", {"scanned": len(result), "details": result})
        return {"ok": True, "scanned": len(result), "details": result}

    @router.get("/api/station/skills/download")
    async def download_skill_package(role: str, agent_id: str = None):
        """Worker 拉取已授权的技能包。"""
        return controller.skill_registry.build_skill_package(role, agent_id)

    @router.get("/api/station/skills/role/{role}")
    async def get_skills_for_role(role: str):
        """获取角色可用的技能列表。"""
        return [s.to_dict() for s in controller.skill_registry.get_skills_for_role(role)]

    @router.get("/api/station/skills/{skill_id}")
    async def get_skill_detail(skill_id: str):
        """获取技能详情及完整内容。"""
        skill = controller.skill_registry.get_skill(skill_id)
        if not skill:
            raise HTTPException(status_code=404, detail="技能不存在")
        content = controller.skill_registry.get_skill_content(skill_id)
        assignments = controller.skill_registry.get_skill_assignments(skill_id)
        return {**skill.to_dict(), "content": content.get("content", ""),
                "reference": content.get("reference", ""), "assignments": assignments}

    @router.post("/api/station/skills/{skill_id}/assign")
    async def assign_skill(skill_id: str, payload: dict):
        """分配技能给角色/Agent/主机。"""
        assignee_type = payload.get("assignee_type", "role")
        assignee_id = payload.get("assignee_id", "")
        if not assignee_id:
            raise HTTPException(status_code=400, detail="assignee_id 不能为空")
        controller.skill_registry.assign_skill(skill_id, assignee_type, assignee_id)
        await _broadcast(state, "skill_assigned", {
            "skill_id": skill_id, "assignee_type": assignee_type, "assignee_id": assignee_id
        })
        return {"ok": True}

    @router.delete("/api/station/skills/{skill_id}/assign")
    async def revoke_skill(skill_id: str, assignee_type: str, assignee_id: str):
        """撤销技能分配。"""
        controller.skill_registry.revoke_skill(skill_id, assignee_type, assignee_id)
        await _broadcast(state, "skill_revoked", {
            "skill_id": skill_id, "assignee_type": assignee_type, "assignee_id": assignee_id
        })
        return {"ok": True}

    # ════════════════════════════════════════════════════════════
    #  Bot 通道管理 (手机消息通道)
    # ════════════════════════════════════════════════════════════

    @router.get("/api/station/bot/channels")
    async def list_bot_channels():
        """列出所有 Bot 通道配置（脱敏）。"""
        return controller.bot_gateway.list_channels()

    @router.post("/api/station/bot/channels")
    async def add_bot_channel(payload: dict):
        """添加或更新 Bot 通道。"""
        from .bot_gateway import BotChannel
        channel = BotChannel(
            channel_type=payload.get("channel_type", "wechat_webhook"),
            enabled=payload.get("enabled", False),
            webhook_url=payload.get("webhook_url", ""),
            bot_token=payload.get("bot_token", ""),
            chat_id=payload.get("chat_id", ""),
            webhook_url_base=payload.get("webhook_url_base", ""),
            min_priority=payload.get("min_priority", "normal"),
        )
        controller.bot_gateway.add_channel(channel)
        return {"ok": True, "message": f"通道 {channel.channel_type} 已配置"}

    @router.delete("/api/station/bot/channels/{channel_type}")
    async def remove_bot_channel(channel_type: str):
        """移除 Bot 通道。"""
        controller.bot_gateway.remove_channel(channel_type)
        return {"ok": True, "message": f"通道 {channel_type} 已移除"}

    @router.post("/api/station/bot/test/{channel_type}")
    async def test_bot_channel(channel_type: str):
        """发送测试消息到指定通道。"""
        result = controller.bot_gateway.test_channel(channel_type)
        if not result.get("ok"):
            raise HTTPException(status_code=400, detail=result.get("error", "测试失败"))
        return result

    # ════════════════════════════════════════════════════════════
    #  P2P 主机间通讯 (聊天 + 文件传输)
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
    #  WebSocket 实时推送
    # ════════════════════════════════════════════════════════════

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
