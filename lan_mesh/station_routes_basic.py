"""
Station 基础路由 — 健康/错误/角色/注册心跳/主机网络/Director (P1 #2 拆分产物)

始终可用的基础层端点 (不依赖 Secretary 激活):
  - F1.2 健康检查与自愈重启 / F1.4 错误追踪
  - Secretary 激活/停用/角色查询
  - Worker 注册/心跳、mesh token 下发、S2 版本升级提醒
  - 主机/网络/发现查询、Station Director 舰队管理
  - 远程 Secretary 分配/撤销/状态/角色查询
"""
import threading
import time

import requests as http_requests

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from .protocol import HostInfo
from .event_bus import publish_event
from .http_retry import auth_headers
from .logger import get_logger
from .station_routes_common import (
    _broadcast, _merge_db_and_udp_hosts, get_mesh_auth_token,
    mesh_auth_enabled,
)

logger = get_logger("station_api")


def build_basic_routes(controller) -> APIRouter:
    """基础路由 (始终可用): 健康/角色/注册心跳/主机/Director。"""
    router = APIRouter()

    # 便捷别名
    db = controller.db
    state = controller.state
    discovery = controller.discovery
    shared_folder = controller.state.shared_folder
    station_director = controller.station_director

    # ════════════════════════════════════════════════════════════
    #  F1.2: 健康检查端点
    # ════════════════════════════════════════════════════════════

    @router.get("/health")
    async def health_check():
        """F1.2: 系统健康检查端点。

        返回各组件状态、资源使用、运行时间。
        用于外部监控 / 自愈重启判断 / 负载均衡探活。
        """
        import os
        import shutil
        import psutil

        proc = psutil.Process(os.getpid())
        mem_mb = proc.memory_info().rss / (1024 * 1024)
        uptime_secs = time.time() - controller._start_timestamp

        # 磁盘使用
        try:
            disk = shutil.disk_usage(str(controller.state.shared_folder.path))
            disk_percent = round(disk.used / disk.total * 100, 1)
        except Exception:
            disk_percent = -1

        # 活跃任务数
        try:
            all_tasks = db.list_tasks()
            active_tasks = len([t for t in all_tasks if getattr(t, 'status', '') in ("pending", "running", "monitoring")])
        except Exception:
            active_tasks = 0

        status = "healthy"
        if mem_mb > 1024:
            status = "degraded"
        if disk_percent > 90:
            status = "critical"

        return {
            "status": status,
            "uptime_secs": round(uptime_secs, 1),
            "timestamp": time.time(),
            "components": {
                "station": "active",
                "secretary": "active" if controller.secretary_active else "inactive",
                "discovery": "active" if controller.discovery else "inactive",
                "local_pm": "active" if controller._local_pm_agent else "idle",
                "bot_gateway": "active" if controller.bot_gateway and controller.bot_gateway._channels else "idle",
            },
            "resources": {
                "memory_mb": round(mem_mb, 1),
                "cpu_percent": proc.cpu_percent(interval=0.1),
                "disk_percent": disk_percent,
                "threads": threading.active_count(),
            },
            "workload": {
                "active_tasks": active_tasks,
                "active_pms": 1 if controller._local_pm_agent else 0,
                "ws_clients": len(state.ws_clients),
            },
        }

    @router.post("/health/restart")
    async def request_restart():
        """F1.2: 触发自愈重启 (graceful)。"""
        controller.request_restart("manual_health_restart")
        return {"ok": True, "message": "重启已调度, 服务将在 3 秒后关闭"}

    # ════════════════════════════════════════════════════════════
    #  F1.4: 错误追踪端点
    # ════════════════════════════════════════════════════════════

    @router.get("/api/errors/stats")
    async def error_stats():
        """F1.4: 错误统计摘要。"""
        from .error_tracker import error_tracker
        return error_tracker.get_stats()

    @router.get("/api/errors/recent")
    async def error_recent(limit: int = 20, module: str = ""):
        """F1.4: 最近错误记录。"""
        from .error_tracker import error_tracker
        return {"errors": error_tracker.get_recent(limit=min(limit, 100), module=module)}

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
        result = {"ok": True, "device_id": info.device_id}
        # Phase 0: 向新节点下发 mesh token — S1 起无论认证开关都下发
        # (加密信任根与认证开关解耦; 认证启用时它同时是传输校验凭证)
        from .auth import get_mesh_token
        result["mesh_token"] = get_mesh_auth_token() or get_mesh_token()
        logger.info("已向新节点下发 mesh token: %s", info.device_id[:8])
        # S1: 注册成功后后台推送加密资源密钥 (新节点上线即武装,
        # 不阻塞注册响应; 发现信息可能尚未入库, 传入回退地址)
        threading.Thread(
            target=controller.push_resource_secrets,
            args=(info.device_id,),
            kwargs={"fallback_ip": record.ip,
                    "fallback_port": info.api_port},
            daemon=True, name="secret-sync-register",
        ).start()
        return result

    @router.get("/api/station/auth-token")
    async def get_auth_token():
        """Phase 0: Web UI 引导获取 mesh token (认证启用时)。

        信任根: 能访问 Web UI 的局域网成员视为内网成员, 凭此 token 操作 API。
        认证关闭时返回空字符串 (前端不附加认证头)。
        """
        token = get_mesh_auth_token()
        return {"auth_enabled": mesh_auth_enabled(), "mesh_token": token if mesh_auth_enabled() else ""}

    @router.get("/api/station/bootstrap-token")
    async def bootstrap_token():
        """S1: mesh token 引导端点 — Station 间无注册链路, 非 Secretary
        节点启动选举时从此处拉取信任根并持久化 (与注册下发同一
        信任假设: 能触及本端口的局域网成员视为内网成员)。
        """
        from .auth import get_mesh_token
        return {"mesh_token": get_mesh_auth_token() or get_mesh_token()}

    # ── S2: 版本升级提醒 ─────────────────────────────────────

    @router.get("/api/version")
    async def get_version():
        """S2: 返回本机代码版本信息 (VERSION.json 记录 + git HEAD)。"""
        from .version_sync import local_version_info
        return local_version_info()

    @router.post("/api/version/upgrade-notice")
    async def upgrade_notice(request: Request):
        """S2: 接收领先节点的升级提醒 — 记录日志并发布事件 (Web Toast/横幅)。

        白名单放行 (与 /api/heartbeat 同一信任假设): 能触及本端口的
        局域网成员视为内网成员; 提醒内容仅展示, 不触发任何自动操作。
        """
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"ok": False, "error": "非法报文"}, status_code=400)
        if not isinstance(body, dict) or not body.get("commit"):
            return JSONResponse({"ok": False, "error": "缺少 commit 字段"}, status_code=400)
        from_name = body.get("from_name") or "未知主机"
        commit = body.get("commit", "")
        hint = body.get("upgrade_hint") or "git pull 升级后重启节点"
        logger.warning("[S2] 收到升级提醒: %s 版本领先 (%s), %s",
                       from_name, commit, hint)
        # F1: 角色无关版本对齐 — 落后节点自动 git pull + 依赖安装
        try:
            if controller and getattr(controller, "auto_upgrade_enabled", True):
                controller._auto_upgrade(commit, from_name)
        except Exception as e:
            logger.warning("[F1] 自动升级触发失败: %s", e)
        publish_event("version_upgrade_notice", {
            "behind": True,
            "from_name": from_name,
            "commit": commit,
            "version": body.get("version", ""),
            "note": body.get("note", ""),
            "hint": hint,
        })
        return {"ok": True}

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

    @router.post("/api/health/restart")
    async def restart_system():
        """触发系统自愈重启。"""
        import threading
        import time

        def delayed_restart():
            time.sleep(1)  # 等待响应发送
            import os
            import sys
            logger.info("正在执行自愈重启...")
            os.execv(sys.executable, ['python'] + sys.argv)

        thread = threading.Thread(target=delayed_restart)
        thread.daemon = True
        thread.start()

        return {"ok": True, "message": "重启已触发"}

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
                headers=auth_headers(),
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
                headers=auth_headers(),
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
                    headers=auth_headers(),
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
    #  P0/P1: 运行时性能追踪端点
    # ════════════════════════════════════════════════════════════

    @router.get("/api/runtime/metrics")
    async def runtime_metrics(hours: float = 1.0):
        """P1: LLM 调用性能指标 (SQLite 聚合, 最近 N 小时)。

        返回: 调用次数、平均/P99 延迟、Token 用量、按模型/状态拆分。
        """
        hours = max(0.1, min(hours, 168))  # 0.1h ~ 7d
        return db.query_llm_metrics(hours=hours)

    @router.get("/api/runtime/trace")
    async def runtime_trace_log(limit: int = 50, type: str = ""):
        """P0: 最近 JSONL 追踪记录 (子任务执行 + LLM 调用)。

        Args:
            limit: 返回条数 (上限 200)
            type: 过滤类型 ("llm_call" / "subtask_end" / ""全部)
        """
        from .runtime_trace import read_trace_lines
        limit = max(1, min(limit, 200))
        return {"records": read_trace_lines(limit=limit, line_type=type)}

    @router.get("/api/runtime/calls")
    async def runtime_call_log(limit: int = 50):
        """P1: LLM 调用明细 (SQLite 最近 N 条, 调试/排查用)。"""
        limit = max(1, min(limit, 200))
        return {"calls": db.query_llm_recent(limit=limit)}

    @router.get("/api/runtime/stats")
    async def runtime_stats(hours: float = 1.0):
        """P0: JSONL 追踪统计 (子任务成功率、模型分布、错误 Top5)。

        与 /api/runtime/metrics 互补: metrics 走 SQLite 审计表,
        stats 走 JSONL 文件 (含子任务执行层数据)。
        """
        from .runtime_trace import trace_stats
        hours = max(0.1, min(hours, 168))
        return trace_stats(hours=hours)

    @router.get("/api/runtime/task-flow")
    async def runtime_task_flow(task_id: str, limit: int = 200):
        """P3: 任务流瀑布 — 按 task_id 聚合生命周期阶段时间线。

        返回: 各阶段事件 (提交/规划/执行/子任务结果/交付/失败)
        + 阶段间隔 (gap_ms) + 总耗时。
        """
        from .runtime_trace import task_flow_waterfall
        if not task_id or len(task_id) > 64:
            raise HTTPException(status_code=400, detail="task_id 必填且不超过 64 字符")
        limit = max(1, min(limit, 500))
        return task_flow_waterfall(task_id, limit=limit)

    @router.get("/api/runtime/task-flow-list")
    async def runtime_task_flow_list(limit: int = 20):
        """P3: 任务流总览 — 最近任务按阶段事件聚合 (末活动时间倒序)。

        返回: [{task_id, stage_count, last_stage/last_label, total_ms, done}, ...]
        """
        from .runtime_trace import task_flow_overview
        limit = max(1, min(limit, 100))
        return {"tasks": task_flow_overview(limit=limit)}

    return router
