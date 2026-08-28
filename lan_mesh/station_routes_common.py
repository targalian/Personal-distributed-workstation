"""
Station API 路由公共层 — 限流/认证中间件与共享工具 (P1 #2 拆分产物)

station_api.py 按路由分层拆分后, 各 station_routes_*.py 模块共用:
  - _RateLimiter / api_guard_middleware: F1.5 限流 + API Key + mesh token
  - configure_mesh_auth / mesh_auth_enabled / get_mesh_auth_token: Phase 0 认证态
  - _heal_mesh_token_from: S1 密钥接收自愈收敛
  - _merge_db_and_udp_hosts / _broadcast: 主机合并与 WebSocket 广播
  - check_secretary: Secretary 未激活 503 守卫
"""
import json
import threading
import time

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from .protocol import HostRecord
from .host_rating import rate_host
from .logger import get_logger

logger = get_logger("station_api")


# ── F1.5: API 限流器 ─────────────────────────────────────────────

class _RateLimiter:
    """F1.5: 简单滑动窗口限流器 (per-IP, 信任/非信任双桶)。

    iter-57 (补强#5): 压测发现全局 120/min 会误伤合法并发负载
    (20 并发任务 + UI 轮询即撞墙); 改为双桶 — 携带合法 mesh token
    的请求走信任桶 (高阈值), 未认证请求保持严格桶防滥用。
    """

    def __init__(self, max_requests: int = 120, window_secs: float = 60.0):
        self._max = max_requests
        self._trusted_max = max_requests
        self._window = window_secs
        self._hits: dict[str, list[float]] = {}          # ip → [时间戳] 严格桶
        self._trusted_hits: dict[str, list[float]] = {}  # ip → [时间戳] 信任桶
        self._lock = threading.Lock()

    def set_limits(self, strict_max: int, trusted_max: int):
        """运行时调整阈值 (0/负数 = 禁用对应桶, 由 configure_rate_limit 调用)。

        iter-57: 阈值变化时清空窗口内历史命中 — 旧速率记录与新阈值
        语义不一致 (测试/热更新时避免历史残留误伤)。
        """
        with self._lock:
            self._max = int(strict_max)
            self._trusted_max = int(trusted_max)
            self._hits.clear()
            self._trusted_hits.clear()

    def is_allowed(self, client_ip: str, trusted: bool = False) -> bool:
        now = time.time()
        cutoff = now - self._window
        with self._lock:
            limit = self._trusted_max if trusted else self._max
            if limit <= 0:
                return True  # 对应桶禁用限流
            bucket = self._trusted_hits if trusted else self._hits
            hits = bucket.get(client_ip, [])
            hits = [t for t in hits if t > cutoff]
            if len(hits) >= limit:
                bucket[client_ip] = hits
                return False
            hits.append(now)
            bucket[client_ip] = hits
            return True


_rate_limiter = _RateLimiter(max_requests=120, window_secs=60.0)


def configure_rate_limit(strict_max: int, trusted_max: int):
    """配置限流阈值 (由 StationController 启动时调用)。

    iter-57 (补强#5): 严格桶 (未认证请求) 防滥用, 信任桶 (合法
    mesh token) 覆盖 10-20 并发任务 + UI 轮询的合法负载。
    0/负数 = 禁用对应桶。
    """
    _rate_limiter.set_limits(strict_max, trusted_max)

# F1.5: API Key 认证 (可选, 通过环境变量 LAN_MESH_API_KEY 启用)
import os as _os
_API_KEY = _os.environ.get("LAN_MESH_API_KEY", "")  # 空 = 不启用认证

# 无需认证的白名单路径
#   /: Web UI 仪表盘 HTML 入口 (页面加载后才能执行 auth-token 自举,
#      信任假设与 /api/station/auth-token 一致: 能访问 UI 者视为内网成员)
#   /api/register, /api/heartbeat: 节点引导注册 (注册响应中下发 mesh token)
#   /health, /api/health: 健康探活 (限流除外)
#   /ws: WebSocket 实时推送 (会话建立后由 UI 持有 token)
#   /ws/worker: M5-2 Worker 事件直推通道 (握手后在端点内自验 mesh_token)
#   /api/station/auth-token: Web UI 引导获取 token (信任根: 能访问 UI 者视为内网成员)
_AUTH_WHITELIST = {"/", "/health", "/api/health",
                   "/api/register", "/api/heartbeat",
                   "/ws", "/ws/worker",
                   "/api/station/auth-token", "/api/station/bootstrap-token",
                   "/api/version/upgrade-notice", "/api/secrets/fetch"}


# Phase 0: 节点间 mesh token 认证 (P2 #5 起默认启用, config.yaml security.auth_enabled 可关)
_mesh_auth_enabled = False
_mesh_auth_token = ""


def configure_mesh_auth(enabled: bool, token: str):
    """配置节点间 mesh token 认证 (由 StationController 启动时调用)。"""
    global _mesh_auth_enabled, _mesh_auth_token
    _mesh_auth_enabled = bool(enabled)
    _mesh_auth_token = (token or "").strip()


def mesh_auth_enabled() -> bool:
    """查询节点认证是否启用。"""
    return _mesh_auth_enabled


def get_mesh_auth_token() -> str:
    """返回当前内存态 mesh token (供各路由模块读取)。"""
    return _mesh_auth_token


def _heal_mesh_token_from(controller, ip: str, port: int) -> str:
    """S1 自愈: 从推送方 (Secretary) 收敛加密信任根并同步内存态。

    mesh_token 分歧 (历史双 Secretary 脑裂 / token 文件重建) 会导致
    密钥互推解密失败; 接收方据此从推送方拉取 bootstrap-token 收敛,
    复用 controller 既有收敛逻辑 (拉取 + 持久化 + 更新内存态)。
    """
    global _mesh_auth_token
    controller._converge_mesh_token(target_ip=ip, target_port=port)
    from .auth import get_mesh_token
    healed = get_mesh_token()
    _mesh_auth_token = healed
    return healed


async def api_guard_middleware(request: Request, call_next):
    """F1.5: 全局限流 + API Key 认证 + Phase 0 mesh token 节点认证中间件。"""
    path = request.url.path

    # 限流 (iter-57: 合法 mesh token 走信任桶, 未认证走严格桶防滥用)
    client_ip = request.client.host if request.client else "unknown"
    trusted = False
    if _mesh_auth_enabled and _mesh_auth_token:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            from .auth import verify_token
            trusted = verify_token(auth_header[7:], _mesh_auth_token)
    else:
        trusted = True  # auth 未启用 (内网自由模式) 不限制合法流量
    if not _rate_limiter.is_allowed(client_ip, trusted=trusted):
        return JSONResponse(status_code=429, content={"detail": "请求过于频繁, 请稍后重试"})

    # API Key 认证 (仅当配置了 key 时启用)
    if _API_KEY and path not in _AUTH_WHITELIST and not path.startswith("/static") \
            and not path.startswith("/spa"):
        provided = request.headers.get("X-API-Key", "") or request.query_params.get("api_key", "")
        if provided != _API_KEY:
            return JSONResponse(status_code=401, content={"detail": "未授权: 缺少有效的 API Key"})

    # Phase 0: mesh token 节点认证 (auth_enabled 时启用)
    # iter-56: /spa 静态资源放行 (SPA 页面加载后才能 auth-token 自举,
    # 与 / 同一信任假设)
    if _mesh_auth_enabled and path not in _AUTH_WHITELIST and not path.startswith("/static") \
            and not path.startswith("/spa"):
        from .auth import verify_token
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return JSONResponse(status_code=401, content={"detail": "缺少认证 token"})
        provided = auth_header[7:]
        if not verify_token(provided, _mesh_auth_token):
            return JSONResponse(status_code=403, content={"detail": "token 无效"})

    return await call_next(request)


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
        logger.info("合并列表: 补充 %d 台 UDP-only 设备", merged_count)
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


def check_secretary(controller):
    """检查 Secretary 是否激活, 未激活抛出 503。"""
    if not controller.secretary_active:
        raise HTTPException(
            status_code=503,
            detail="Secretary 未激活,请在 Station 面板中点击「启动秘书」"
        )
