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
import secrets
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
                   "/api/version/upgrade-notice", "/api/secrets/fetch",
                   "/sw.js"}  # iter-62: SW 注册请求由浏览器发起不带 Authorization 头


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


# F5.2 (iter-58): 多用户权限 — 用户表内存态 (name → role/token),
# 由 StationController 启动时注入; 空表 = 关闭多用户权限
# (所有人持 mesh token 即 boss, 向后兼容)。
# iter-63 (团队场景深化): token 仅存 SHA256 哈希 (不存明文),
# config.security.users 作为首次种子 (DB 空时导入), 之后以 DB 为准
# → token 轮换/角色修改跨重启保留。
_users: dict[str, dict] = {}
_user_tokens: dict[str, dict] = {}
_users_db = None  # Database 引用 (set_users_db 注入, 供端点写操作持久化)
_VALID_ROLES = {"boss", "operator", "viewer"}


def _hash_token(token: str) -> str:
    """token → SHA256 十六进制 (仅存哈希, 不落明文)。"""
    import hashlib
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _gen_token() -> str:
    """生成新用户 token (URL 安全, 含用户可见尾 4 位)。"""
    return secrets.token_urlsafe(24)


def set_users_db(db):
    """注入 Database 引用 (StationController 启动时调用)。"""
    global _users_db
    _users_db = db


def configure_users(users: list):
    """注入用户表 (由 StationController 启动时调用)。

    users: [{"name": str, "role": str, "token": str}, ...]
    非法 role 归一到 viewer; 空 token 的用户跳过。
    iter-63: 明文 token 在此哈希化后入内存 (config 种子语义,
    仅当 DB 为空时使用; DB 非空时调用方应改走 load_users_from_db)。
    """
    global _users, _user_tokens
    _users = {}
    _user_tokens = {}
    for u in (users or []):
        name = str(u.get("name", "")).strip()
        token = str(u.get("token", "")).strip()
        if not token:
            continue  # 无 token 的用户不可认证, 跳过
        role = str(u.get("role", "viewer")).strip().lower()
        if role not in _VALID_ROLES:
            role = "viewer"
        entry = {"name": name, "role": role,
                 "token_hash": _hash_token(token),
                 "token_tail4": token[-4:]}
        _users[name or token[:8]] = entry
        _user_tokens[entry["token_hash"]] = entry


def load_users_from_db(db) -> bool:
    """从 DB users 表加载内存用户表; DB 为空时用 config 种子导入。

    返回是否启用了多用户 (非空)。config 用户仅当 DB 完全为空时
    写入 (首次启动), 之后 DB 为准 → 轮换持久化。
    """
    global _users, _user_tokens
    rows = db.list_users_db()
    if not rows:
        # 首次启动: config 种子导入 DB (明文→哈希)
        for entry in _users.values():
            db.upsert_user_db(entry["name"], entry["role"],
                              entry["token_hash"], entry["token_tail4"])
        return bool(_users)
    _users = {}
    _user_tokens = {}
    for r in rows:
        entry = {"name": r["name"], "role": r["role"],
                 "token_hash": r["token_hash"],
                 "token_tail4": r["token_tail4"]}
        _users[r["name"] or r["token_tail4"]] = entry
        _user_tokens[r["token_hash"]] = entry
    return True


def resolve_role(provided_token: str) -> dict:
    """判定请求 token 归属: 节点 mesh token → boss; 用户 token → 角色。

    返回 {"name", "role"}; 未知 token 返回 None。调用方应只在认证
    已通过后调用 (mesh token 优先, 用户 token 其次)。
    iter-63: 用户 token 按 SHA256 哈希恒定时间比较 (内存不存明文)。
    """
    if _mesh_auth_enabled and _mesh_auth_token:
        from .auth import verify_token
        if verify_token(provided_token, _mesh_auth_token):
            return {"name": "节点", "role": "boss"}
    # 用户 token 哈希恒定时间比较 (防时序侧信道)
    provided_hash = _hash_token(provided_token)
    entry = _user_tokens.get(provided_hash)
    if entry is not None and secrets.compare_digest(provided_hash, entry["token_hash"]):
        return {"name": entry["name"], "role": entry["role"]}
    return None


def users_configured() -> bool:
    """是否配置了多用户 (非空则启用角色分级)。"""
    return bool(_user_tokens)


def list_users_public() -> list:
    """列出用户 (脱敏 token, 供管理员页面展示)。"""
    return [{"name": e["name"], "role": e["role"]}
            for e in _users.values()]


def list_users_admin() -> list:
    """列出用户 (含 token 尾 4 位快照, 仅 boss 端点返回)。"""
    return [{"name": e["name"], "role": e["role"],
             "token_tail4": e.get("token_tail4", "")}
            for e in _users.values()]


def _last_boss_guard(name: str, target_role: str) -> bool:
    """最后 boss 保护: 不允许把最后一个 boss 降级/删除 (防自锁)。

    target_role 仅用于语义表达 (None = 删除); 判定只看目标自身
    是否为唯一 boss: 是 → 拦截降级与删除; 升权不受限。
    """
    me = _users.get(name)
    if not me or me["role"] != "boss":
        return False
    boss_count = sum(1 for e in _users.values() if e["role"] == "boss")
    return boss_count <= 1


def create_user(name: str, role: str) -> dict:
    """新增用户 (boss 端点): 生成 token 并写内存 + DB, 返回明文一次。"""
    global _users, _user_tokens
    name = name.strip()
    role = role if role in _VALID_ROLES else "viewer"
    if not name:
        return {"error": "用户名不能为空"}
    if name in _users:
        return {"error": "用户名已存在"}
    token = _gen_token()
    entry = {"name": name, "role": role,
             "token_hash": _hash_token(token), "token_tail4": token[-4:]}
    _users[name] = entry
    _user_tokens[entry["token_hash"]] = entry
    if _users_db:
        _users_db.upsert_user_db(name, role, entry["token_hash"],
                                 entry["token_tail4"])
    return {"name": name, "role": role, "token": token}


def rotate_user_token(name: str) -> dict:
    """轮换用户 token (旧 token 立即失效), 返回新明文一次。"""
    global _user_tokens
    entry = _users.get(name)
    if not entry:
        return {"error": "用户不存在"}
    token = _gen_token()
    old_hash = entry["token_hash"]
    entry["token_hash"] = _hash_token(token)
    entry["token_tail4"] = token[-4:]
    _user_tokens.pop(old_hash, None)
    _user_tokens[entry["token_hash"]] = entry
    if _users_db:
        _users_db.upsert_user_db(name, entry["role"], entry["token_hash"],
                                 entry["token_tail4"])
    return {"name": name, "role": entry["role"], "token": token}


def set_user_role(name: str, role: str) -> dict:
    """修改用户角色 (最后 boss 保护)。"""
    entry = _users.get(name)
    if not entry:
        return {"error": "用户不存在"}
    if role not in _VALID_ROLES:
        return {"error": f"非法角色 (仅支持 {sorted(_VALID_ROLES)})"}
    if _last_boss_guard(name, role):
        return {"error": "不能降级最后一个 boss (防自锁)"}
    entry["role"] = role
    if _users_db:
        _users_db.update_user_role_db(name, role)
    return {"name": name, "role": role}


def remove_user(name: str) -> dict:
    """移除用户 (最后 boss 保护)。"""
    entry = _users.get(name)
    if not entry:
        return {"error": "用户不存在"}
    if _last_boss_guard(name, None):
        return {"error": "不能删除最后一个 boss (防自锁)"}
    _users.pop(name, None)
    _user_tokens.pop(entry["token_hash"], None)
    if _users_db:
        _users_db.delete_user_db(name)
    return {"name": name, "removed": True}


# 管理员路径前缀: 仅 boss 可写 (读仍对全部角色开放)
_ADMIN_PREFIXES = ("/api/station/", "/api/runtime/", "/api/secrets/",
                    "/api/version/", "/api/resources/", "/api/network/",
                    "/api/agents/")


def _check_role_access(path: str, method: str, role: str) -> bool:
    """F5.2: 角色访问判定 (读操作全角色放行)。

    规则:
    - boss: 全部放行
    - operator: 写操作放行 (管理员路径除外)
    - viewer: 仅 GET/HEAD/OPTIONS 放行
    """
    if role == "boss":
        return True
    if method in ("GET", "HEAD", "OPTIONS"):
        return True  # 读操作全角色放行
    # 写操作: 管理员路径仅 boss; 其余 boss/operator
    if role == "operator" and not path.startswith(_ADMIN_PREFIXES):
        return True
    return False


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
    # iter-58 (F5.2): 用户个人 token 与 mesh token 等效认证, 认证后
    # 按角色分级授权 (见下方角色检查段)
    if _mesh_auth_enabled and path not in _AUTH_WHITELIST and not path.startswith("/static") \
            and not path.startswith("/spa"):
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return JSONResponse(status_code=401, content={"detail": "缺少认证 token"})
        provided = auth_header[7:]
        identity = resolve_role(provided)
        if not identity:
            return JSONResponse(status_code=403, content={"detail": "token 无效"})

        # F5.2 (iter-58): 角色分级授权 (配置了用户表才启用; mesh token
        # 为 boss 不受限, 向后兼容)
        if users_configured():
            if not _check_role_access(path, request.method, identity["role"]):
                return JSONResponse(
                    status_code=403,
                    content={"detail": f"权限不足: 角色 {identity['role']} "
                                      f"不可执行此操作 (需 boss/operator)"})

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
