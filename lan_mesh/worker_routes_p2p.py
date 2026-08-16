"""
Worker P2P 路由 — 主机间消息接收 (iter-31 拆分产物)

始终可用 (Worker 独立进程与 Station 内嵌 Worker 共用):
  - 接收远程主机 P2P 消息 (/api/p2p/receive), 内存级存储不做 WS 广播

路由函数名/端点路径/行为与原 api.py create_worker_router 逐字一致。
"""
import time

from fastapi import APIRouter, HTTPException

from .logger import get_logger

logger = get_logger("api")


def build_p2p_routes() -> APIRouter:
    """P2P 消息接收端点。"""
    router = APIRouter()

    # Worker 端内存级消息存储
    _p2p_store: dict = {}

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
