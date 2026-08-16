"""
S1-key-sync: API Key 加密自动分发 (节点间密钥同步)

设计:
- 以 mesh_token 为信任根, HKDF-SHA256 派生 32 字节 AES-256 密钥
- AES-256-GCM 加密资源配置 (含 api_key 直填值), 每次传输随机
  12 字节 nonce, GCM 同时提供机密性与完整性校验
- 依赖 cryptography>=41.0; 缺失时明确报错, 绝不降级明文

安全边界 (与 auth.py LAN 信任模型一致):
- 防局域网嗅探/未授权设备读取密钥, 不替代 TLS
- mesh_token 本身经明文 HTTP 下发 (注册引导), 为既有信任假设

用法:
    from .secret_sync import encrypt_config, decrypt_config, config_hash

    payload = encrypt_config(config_data, mesh_token)   # Secretary 侧
    data = decrypt_config(payload, mesh_token)          # Worker 侧
"""
import base64
import hashlib
import json
import os
from typing import Union

from .logger import get_logger

logger = get_logger("secret-sync")

# HKDF 派生参数 (固定 salt/info, 密钥熵全部来自 mesh_token)
_HKDF_SALT = b"lan-mesh-secret-sync-v1"
_HKDF_INFO = b"lan-mesh-aes256gcm"
_NONCE_BYTES = 12  # GCM 推荐 nonce 长度


def _crypto_available() -> bool:
    """cryptography 库是否可用。"""
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: F401
        return True
    except ImportError:
        return False


def _derive_key(mesh_token: str) -> bytes:
    """由 mesh_token 派生 AES-256 密钥 (HKDF-SHA256)。"""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=_HKDF_SALT,
        info=_HKDF_INFO,
    ).derive(mesh_token.encode("utf-8"))


def encrypt_config(data: dict, mesh_token: str) -> dict:
    """加密配置 dict → {"nonce": b64, "blob": b64}。

    Raises:
        RuntimeError: cryptography 缺失
        ValueError: mesh_token 为空
    """
    if not _crypto_available():
        raise RuntimeError(
            "缺少 cryptography 依赖, 无法加密 (pip install cryptography)")
    if not (mesh_token or "").strip():
        raise ValueError("mesh_token 为空, 无法加密 (需启用 security.auth_enabled)")

    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    key = _derive_key(mesh_token.strip())
    nonce = os.urandom(_NONCE_BYTES)
    plaintext = json.dumps(
        data, ensure_ascii=False, sort_keys=True).encode("utf-8")
    blob = AESGCM(key).encrypt(nonce, plaintext, None)
    return {
        "nonce": base64.b64encode(nonce).decode("ascii"),
        "blob": base64.b64encode(blob).decode("ascii"),
    }


def decrypt_config(payload: dict, mesh_token: str) -> dict:
    """解密 {"nonce", "blob"} → 配置 dict。

    Raises:
        RuntimeError: cryptography 缺失
        ValueError: 报文残缺 / 密钥错误 / 内容被篡改 (GCM 校验失败)
    """
    if not _crypto_available():
        raise RuntimeError(
            "缺少 cryptography 依赖, 无法解密 (pip install cryptography)")
    if not (mesh_token or "").strip():
        raise ValueError("mesh_token 为空, 无法解密")

    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    try:
        nonce = base64.b64decode(payload.get("nonce", ""))
        blob = base64.b64decode(payload.get("blob", ""))
    except Exception:
        raise ValueError("密文报文格式非法 (nonce/blob 非 base64)")
    if not nonce or not blob:
        raise ValueError("密文报文残缺 (nonce 或 blob 为空)")

    key = _derive_key(mesh_token.strip())
    try:
        plaintext = AESGCM(key).decrypt(nonce, blob, None)
    except InvalidTag:
        raise ValueError("解密失败: mesh_token 不匹配或内容被篡改")
    try:
        return json.loads(plaintext.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("解密失败: 明文非合法 JSON")


def config_hash(data: Union[dict, str]) -> str:
    """配置内容指纹 (sha256 hex) — 幂等判断用。

    dict 先规范化 (sort_keys), 字符串直接哈希;
    相同语义配置必得相同指纹。

    F1: config_ts 为对齐仲裁元数据, 不计入内容指纹 —
    否则接收方落盘刷新时间戳后与推送方指纹永久不同,
    引发周期对齐 ping-pong 漂移。
    """
    if isinstance(data, str):
        raw = data
    else:
        clean = {k: v for k, v in data.items() if k != "config_ts"}
        raw = json.dumps(clean, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def mask_secret(value: str, keep: int = 4) -> str:
    """密钥脱敏 (日志展示用): 保留前 keep 位 + ***。"""
    value = (value or "").strip()
    if len(value) <= keep:
        return "***"
    return value[:keep] + "***"
