"""
Station Director API 路由层 (装配入口)

P1 #2 按路由分层拆分: 原 2500+ 行单文件拆为公共层 + 6 个路由域模块,
本文件只负责装配与 WebSocket 通道:

  - station_routes_common   限流/认证中间件与共享工具 (单一事实源)
  - station_routes_basic    健康/错误/角色/注册心跳/主机/Director (始终可用)
  - station_routes_tasks    Agent/任务/图/交付闭环/任务记忆 (Secretary)
  - station_routes_resources 模型资源/配置向导/密钥同步/事件 (Secretary)
  - station_routes_pm       PM Agent 管理/进度/子任务/团队 (Secretary)
  - station_routes_chat     秘书聊天/多对话/PM 线程/Bot 入口 (Secretary)
  - station_routes_projects 项目/MCP 工具/模型路由/技能库/Bot 通道 (Secretary)
  - station_routes_worker   内嵌 Worker 端点/P2P/云同步 (始终可用)
  - station_routes_shadow   影子开发提交/查询/守护状态 (始终可用)
  - station_routes_optimization 工作站常驻优化提交/决策/状态 (始终可用)

设计要点:
  - 所有组件通过 controller 对象访问 (可变引用)
  - Secretary 路由检查 controller.secretary_active, 未激活时返回 503
  - 激活/停用 Secretary 无需重启服务
"""
import asyncio

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from .logger import get_logger
from .station_routes_basic import build_basic_routes
from .station_routes_chat import build_chat_routes
from .station_routes_pm import build_pm_routes
from .station_routes_optimization import build_optimization_routes
from .station_routes_projects import build_project_routes
from .station_routes_shadow import build_shadow_dev_routes
from .station_routes_resources import apply_usage_batch, build_resource_routes
from .station_routes_tasks import build_task_routes
from .station_routes_worker import build_worker_routes

# 兼容再导出: 外部 (station_controller/worker) 历史上从本模块导入,
# 事实源已移至 station_routes_common, 此处保持导入路径不变。
from .station_routes_common import (  # noqa: F401
    _broadcast, _heal_mesh_token_from, _merge_db_and_udp_hosts,
    _RateLimiter, api_guard_middleware, configure_mesh_auth,
    get_mesh_auth_token, mesh_auth_enabled,
)

logger = get_logger("station_api")


def create_station_router(controller) -> APIRouter:
    """创建 Station Director 的完整 API 路由 (装配各路由域 + WebSocket)。

    Args:
        controller: StationController 实例 (持有所有可变状态)
    """
    router = APIRouter()
    router.include_router(build_basic_routes(controller))
    router.include_router(build_optimization_routes(controller))
    router.include_router(build_shadow_dev_routes(controller))
    router.include_router(build_task_routes(controller))
    router.include_router(build_resource_routes(controller))
    router.include_router(build_pm_routes(controller))
    router.include_router(build_chat_routes(controller))
    router.include_router(build_project_routes(controller))
    router.include_router(build_worker_routes(controller))

    # ════════════════════════════════════════════════════════════
    #  WebSocket 实时推送
    # ════════════════════════════════════════════════════════════

    db = controller.db
    state = controller.state

    # P0/P1: 注入 Database 引用供运行时追踪双写 SQLite
    from .runtime_trace import set_db as _trace_set_db
    _trace_set_db(db)

    @router.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket):
        """WebSocket 实时推送主机状态变更 + M5 事件总线事件。"""
        await websocket.accept()
        state.ws_clients.add(websocket)
        # M5: 懒装配事件总线 sink (幂等) — 事件经本通道广播
        from .event_bus import get_event_bus
        bus = get_event_bus()
        if not bus.has_sink:
            def _event_sink(evt: dict):
                asyncio.ensure_future(_broadcast(state, "event", evt))
            bus.attach(asyncio.get_running_loop(), _event_sink)
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

    @router.websocket("/ws/worker")
    async def worker_ws_endpoint(websocket: WebSocket):
        """M5-2: Worker 事件直推通道 (替代 60s HTTP 批量轮询的实时路径)。

        鉴权: 认证启用时校验 query 参数 token (mesh_token, 恒定时间
        比较), 不通过直接拒绝握手。帧协议 (JSON):
          - {"type": "usage_batch", "records": [...]} → 复用 HTTP 批量
            同一幂等路径 (usage_id 去重), 回 ack {ok, total,
            recorded, duplicate}; Secretary 未激活时 ack 失败,
            Worker 不推游标 (HTTP 兜底链路后续补报)。
          - 其他 type → 转发 event_bus → 自动广播前端 /ws。
        """
        from .auth import verify_token
        if mesh_auth_enabled():
            token = websocket.query_params.get("token", "")
            if not token or not verify_token(token, get_mesh_auth_token()):
                await websocket.close(code=4003)
                return
        await websocket.accept()
        client = websocket.client.host if websocket.client else "?"
        logger.info("[WS] /ws/worker 连接建立: %s", client)
        try:
            while True:
                try:
                    msg = await asyncio.wait_for(
                        websocket.receive_json(), timeout=60)
                except asyncio.TimeoutError:
                    await websocket.send_json({"type": "ping"})
                    continue
                if not isinstance(msg, dict):
                    continue
                msg_type = str(msg.get("type", ""))
                if msg_type == "usage_batch":
                    if not controller.secretary_active:
                        await websocket.send_json(
                            {"ok": False, "error": "secretary_inactive"})
                        continue
                    result = apply_usage_batch(msg.get("records") or [])
                    await websocket.send_json({"ok": True, **result})
                elif msg_type:
                    from .event_bus import publish_event
                    publish_event(msg_type, msg.get("data") or {})
                    await websocket.send_json({"ok": True})
        except WebSocketDisconnect:
            logger.info("[WS] /ws/worker 断开: %s", client)
        except Exception:
            logger.info("[WS] /ws/worker 异常断开: %s", client)

    return router
