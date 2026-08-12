"""
内部 HTTP 通信重试工具

为节点间 (PM ↔ Secretary ↔ Worker) 的 HTTP 调用提供:
1. 指数退避重试 (默认 3 次, 间隔 0.5s → 1s → 2s)
2. 可配置超时
3. 仅对网络错误 / 5xx 重试, 4xx 不重试
4. 线程安全, 无全局状态

用法:
    from .http_retry import http_post, http_get, set_auth_token

    # 简单 POST (自动重试 + 自动附加 mesh token)
    resp = http_post(url, json=payload, timeout=5)

    # 自定义重试策略
    resp = http_post(url, json=payload, retries=5, backoff_base=1.0)

    # GET
    data = http_get(url, params={"key": "val"})

节点认证 (Phase 0):
    - set_auth_token(token) 注册本节点 mesh token
    - 注册后所有 http_* 请求自动携带 Authorization: Bearer <token>
    - 未注册 token 时不附加任何头 (向后兼容, 认证关闭时透明)
"""

# ── 节点间 mesh token 认证 ──────────────────────────────────────

_auth_token: str = ""  # 本节点持有的 mesh token (空 = 未启用认证)


def set_auth_token(token: str):
    """注册本节点的 mesh token, 后续所有请求自动携带。

    传空字符串则清除 (认证关闭)。
    """
    global _auth_token
    _auth_token = (token or "").strip()


def get_auth_token() -> str:
    """返回当前注册的 mesh token (空字符串 = 未启用)。"""
    return _auth_token


def auth_headers() -> dict:
    """生成携带 mesh token 的请求头 (无 token 时返回空 dict)。"""
    if _auth_token:
        return {"Authorization": f"Bearer {_auth_token}"}
    return {}


def _merge_auth_headers(kwargs: dict) -> dict:
    """将 mesh token 头合并进 kwargs (调用方显式传入的 headers 优先)。"""
    headers = dict(kwargs.get("headers") or {})
    if _auth_token:
        headers.setdefault("Authorization", f"Bearer {_auth_token}")
    return headers
import time
from typing import Optional

import requests

from .logger import get_logger

logger = get_logger("http_retry")


# ── 默认配置 ──────────────────────────────────────────────────

DEFAULT_RETRIES = 3          # 最大重试次数 (不含首次)
DEFAULT_TIMEOUT = 10         # 请求超时 (秒)
DEFAULT_BACKOFF_BASE = 0.5   # 退避基数 (秒): 0.5, 1.0, 2.0, ...
DEFAULT_BACKOFF_FACTOR = 2.0 # 退避倍增因子
RETRYABLE_STATUS_CODES = {500, 502, 503, 504}  # 可重试的 HTTP 状态码


class HttpRetryError(Exception):
    """所有重试均失败后抛出。"""

    def __init__(self, url: str, attempts: int, last_error: Exception):
        self.url = url
        self.attempts = attempts
        self.last_error = last_error
        super().__init__(
            f"HTTP 请求失败 (已重试 {attempts} 次): {url} → {last_error}"
        )


def _should_retry(exc: Exception) -> bool:
    """判断异常是否可重试。"""
    if isinstance(exc, requests.ConnectionError):
        return True
    if isinstance(exc, requests.Timeout):
        return True
    if isinstance(exc, requests.HTTPError):
        status = exc.response.status_code if exc.response is not None else 0
        return status in RETRYABLE_STATUS_CODES
    return False


def _request_with_retry(
    method: str,
    url: str,
    retries: int = DEFAULT_RETRIES,
    timeout: float = DEFAULT_TIMEOUT,
    backoff_base: float = DEFAULT_BACKOFF_BASE,
    backoff_factor: float = DEFAULT_BACKOFF_FACTOR,
    raise_on_error: bool = False,
    **kwargs,
) -> Optional[requests.Response]:
    """通用 HTTP 请求 + 指数退避重试。

    Args:
        method: HTTP 方法 ("GET" / "POST" / "PUT" / "DELETE")
        url: 请求 URL
        retries: 最大重试次数
        timeout: 单次请求超时 (秒)
        backoff_base: 退避基数 (秒)
        backoff_factor: 退避倍增因子
        raise_on_error: True 时所有重试失败后抛出 HttpRetryError;
                        False 时返回 None
        **kwargs: 传递给 requests.request() 的额外参数

    Returns:
        requests.Response 或 None (所有重试失败且 raise_on_error=False)
    """
    kwargs.setdefault("timeout", timeout)
    # Phase 0: 自动附加 mesh token (认证启用时)
    kwargs["headers"] = _merge_auth_headers(kwargs)
    last_error: Optional[Exception] = None

    for attempt in range(retries + 1):
        try:
            resp = requests.request(method, url, **kwargs)
            # 5xx 视为可重试错误
            if resp.status_code in RETRYABLE_STATUS_CODES and attempt < retries:
                last_error = requests.HTTPError(
                    f"HTTP {resp.status_code}", response=resp
                )
                delay = backoff_base * (backoff_factor ** attempt)
                logger.debug(
                    "[Retry] %s %s → %d, %.1fs 后重试 (%d/%d)",
                    method, url, resp.status_code, delay, attempt + 1, retries,
                )
                time.sleep(delay)
                continue
            return resp

        except (requests.ConnectionError, requests.Timeout) as e:
            last_error = e
            if attempt < retries:
                delay = backoff_base * (backoff_factor ** attempt)
                logger.debug(
                    "[Retry] %s %s → %s, %.1fs 后重试 (%d/%d)",
                    method, url, type(e).__name__, delay, attempt + 1, retries,
                )
                time.sleep(delay)
            continue

        except requests.HTTPError as e:
            if _should_retry(e) and attempt < retries:
                delay = backoff_base * (backoff_factor ** attempt)
                time.sleep(delay)
                last_error = e
                continue
            # 4xx 不重试, 直接返回
            if raise_on_error:
                raise
            return e.response if e.response is not None else None

    # 所有重试耗尽
    if raise_on_error and last_error:
        raise HttpRetryError(url, retries + 1, last_error)

    logger.warning(
        "[Retry] %s %s 最终失败 (共 %d 次尝试): %s",
        method, url, retries + 1, last_error,
    )
    return None


# ── 便捷函数 ──────────────────────────────────────────────────


def http_get(url: str, retries: int = DEFAULT_RETRIES,
             timeout: float = DEFAULT_TIMEOUT, **kwargs) -> Optional[requests.Response]:
    """GET 请求 (带重试)。"""
    return _request_with_retry("GET", url, retries=retries, timeout=timeout, **kwargs)


def http_post(url: str, retries: int = DEFAULT_RETRIES,
              timeout: float = DEFAULT_TIMEOUT, **kwargs) -> Optional[requests.Response]:
    """POST 请求 (带重试)。"""
    return _request_with_retry("POST", url, retries=retries, timeout=timeout, **kwargs)


def http_put(url: str, retries: int = DEFAULT_RETRIES,
             timeout: float = DEFAULT_TIMEOUT, **kwargs) -> Optional[requests.Response]:
    """PUT 请求 (带重试)。"""
    return _request_with_retry("PUT", url, retries=retries, timeout=timeout, **kwargs)


def http_delete(url: str, retries: int = DEFAULT_RETRIES,
                timeout: float = DEFAULT_TIMEOUT, **kwargs) -> Optional[requests.Response]:
    """DELETE 请求 (带重试)。"""
    return _request_with_retry("DELETE", url, retries=retries, timeout=timeout, **kwargs)
