"""
节点间通信认证 — 轻量级 Shared Token 机制

设计:
- 局域网内所有节点共享一个 mesh_token (启动时生成或从配置读取)
- Secretary 在 Worker 注册成功后将 token 下发给 Worker
- 后续所有内部 API 请求携带 Authorization: Bearer <token>
- 各节点通过 FastAPI 中间件/依赖注入校验 token

安全边界:
- 仅防止局域网内未授权设备误接入, 不替代 TLS
- token 长度 32 字节 (hex 64 字符), 密码学安全随机数
- 可选启用 (config.yaml: security.auth_enabled: true)

用法:
    from .auth import get_mesh_token, verify_token, AuthDependency

    # Secretary 启动时
    token = get_mesh_token(cfg)

    # FastAPI 路由保护
    @router.get("/api/hosts", dependencies=[Depends(AuthDependency(token))])
    async def list_hosts(): ...
"""
import os
import secrets
from pathlib import Path
from typing import Optional

from fastapi import HTTPException, Request

from .logger import get_logger

logger = get_logger("auth")

# Token 文件路径 (持久化, 重启不变)
_TOKEN_FILE = Path.home() / ".lan_mesh" / "mesh_token"


def generate_token() -> str:
    """生成密码学安全的随机 token (64 hex 字符 = 32 字节熵)。"""
    return secrets.token_hex(32)


def get_mesh_token(cfg=None) -> str:
    """获取或创建 mesh token。

    优先级:
    1. config.security.mesh_token (config.yaml 显式配置, 全网共享)
    2. 环境变量 LAN_MESH_TOKEN
    3. 持久化文件 ~/.lan_mesh/mesh_token
    4. 自动生成并持久化

    Args:
        cfg: AppConfig (可选, 支持从 config.yaml 读取显式 token)

    Returns:
        64 字符 hex token
    """
    # 1. config.yaml 显式配置 (全网共享同一 token)
    if cfg is not None:
        explicit = getattr(getattr(cfg, "security", None), "mesh_token", "") or ""
        if explicit.strip():
            return explicit.strip()

    # 2. 环境变量
    env_token = os.environ.get("LAN_MESH_TOKEN", "").strip()
    if env_token:
        return env_token

    # 3. 持久化文件
    if _TOKEN_FILE.is_file():
        stored = _TOKEN_FILE.read_text(encoding="utf-8").strip()
        if len(stored) >= 32:
            return stored

    # 4. 生成新 token
    token = generate_token()
    try:
        _TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        _TOKEN_FILE.write_text(token, encoding="utf-8")
        _TOKEN_FILE.chmod(0o600)  # 仅 owner 可读写
        logger.info("已生成新 mesh token 并保存到 %s", _TOKEN_FILE)
    except OSError as e:
        logger.warning("无法持久化 mesh token: %s (每次重启将重新生成)", e)

    return token


def verify_token(provided: str, expected: str) -> bool:
    """恒定时间比较 token (防时序攻击)。"""
    return secrets.compare_digest(provided, expected)


class AuthDependency:
    """FastAPI 依赖注入 — 校验 Authorization: Bearer <token>。

    用法:
        auth = AuthDependency(token)

        @router.get("/api/hosts", dependencies=[Depends(auth)])
        async def list_hosts(): ...

    如果 enabled=False, 则跳过校验 (开发模式)。
    """

    def __init__(self, token: str, enabled: bool = True):
        self._token = token
        self._enabled = enabled

    async def __call__(self, request: Request):
        if not self._enabled:
            return

        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="缺少认证 token")

        provided = auth_header[7:]  # 去掉 "Bearer " 前缀
        if not verify_token(provided, self._token):
            raise HTTPException(status_code=403, detail="token 无效")


def make_auth_headers(token: str) -> dict:
    """生成携带 token 的请求头 (供 http_retry / requests 使用)。

    用法:
        headers = make_auth_headers(token)
        requests.post(url, json=payload, headers=headers)
    """
    return {"Authorization": f"Bearer {token}"}
