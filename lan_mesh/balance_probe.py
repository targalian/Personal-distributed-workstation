"""
资源余额探测 (R2) — 从服务商 API 自动获取资源池余额

适配器模式: provider → probe 函数, 未支持的 provider 返回 unsupported + 引导提示。

调研结论 (2026-08, 已查证):
- siliconflow:  GET https://api.siliconflow.cn/v1/user/info  (Bearer key) → data.balance (CNY)
                (国际版: api.siliconflow.com)
- deepseek:     GET https://api.deepseek.com/user/balance  (Bearer key)
                → balance_infos[0].{total_balance, currency}
- moonshot:     GET https://api.moonshot.cn/v1/users/me/balance (Bearer key)
                → data.available_balance (宽容解析多字段)
- zhipu:        GET https://open.bigmodel.cn/api/monitor/usage/quota/limit (Bearer key)
                → 配额字段 (实验性, 格式可能变化, best-effort 解析)
- openai:       普通 API Key 无余额接口 (旧 dashboard/billing 接口已撤销,
                官方需 Organization admin key) → unsupported
- ark / aliyun: 需云厂商 OpenAPI (AK/SK), 非模型 API Key → unsupported

统一返回结构:
    {"supported": bool, "provider": str, "balance": float|None,
     "currency": str, "source": str, "checked_at": float, "error": str, "hint": str}
"""
import os
import time
from typing import Callable, Optional

import requests

from .logger import get_logger

logger = get_logger("resources")

DEFAULT_TIMEOUT = 10.0

# provider 别名归一化 (资源池 provider 字段 → 探测实现键)
PROVIDER_ALIASES = {
    "siliconflow": "siliconflow",
    "sf": "siliconflow",
    "deepseek": "deepseek",
    "moonshot": "moonshot",
    "kimi": "moonshot",
    "zhipu": "zhipu",
    "glm": "zhipu",
    "bigmodel": "zhipu",
}

# 未支持 provider 的引导提示
UNSUPPORTED_HINTS = {
    "openai": "OpenAI 普通 API Key 无余额接口 (旧 dashboard/billing 已撤销); "
              "需 Organization admin key 走 Usage API, 或控制台查看",
    "anthropic": "Anthropic 无公开余额 API, 请在控制台 console.anthropic.com 查看",
    "ark": "火山方舟余额需火山引擎 OpenAPI (AK/SK), 非模型 API Key; "
           "可在方舟控制台-资源包查看",
    "aliyun": "阿里云百炼余额需阿里云 OpenAPI (AK/SK), 非模型 API Key; "
              "可在百炼控制台-资源包查看",
    "qwen": "通义千问余额需阿里云 OpenAPI (AK/SK), 请在百炼控制台查看",
    "google": "Gemini 余额需 Google Cloud Billing API, 请在 Google AI Studio 查看",
    "ollama": "本地模型无余额概念",
    "local": "本地模型无余额概念",
}


def _normalize_provider(provider: str) -> str:
    """provider 别名归一化 (小写)。"""
    key = (provider or "").lower().strip()
    return PROVIDER_ALIASES.get(key, key)


# ── 探测实现 ────────────────────────────────────────────────────

def _probe_siliconflow(api_key: str, timeout: float = DEFAULT_TIMEOUT) -> dict:
    """SiliconFlow 余额: GET /v1/user/info → data.balance (CNY)。"""
    base = os.environ.get("SILICONFLOW_BASE_URL",
                          "https://api.siliconflow.cn/v1")
    resp = requests.get(
        f"{base.rstrip('/')}/user/info",
        headers={"Authorization": f"Bearer {api_key.strip()}"},
        timeout=timeout,
    )
    resp.raise_for_status()
    data = resp.json()
    bal = (data.get("data") or {}).get("balance")
    if bal is None:
        raise ValueError(f"响应缺少 balance 字段: {str(data)[:200]}")
    return {"balance": float(bal), "currency": "CNY", "source": f"{base}/user/info"}


def _probe_deepseek(api_key: str, timeout: float = DEFAULT_TIMEOUT) -> dict:
    """DeepSeek 余额: GET /user/balance → balance_infos[0]。"""
    resp = requests.get(
        "https://api.deepseek.com/user/balance",
        headers={"Authorization": f"Bearer {api_key.strip()}"},
        timeout=timeout,
    )
    resp.raise_for_status()
    data = resp.json()
    infos = data.get("balance_infos") or []
    if not infos:
        raise ValueError(f"响应缺少 balance_infos: {str(data)[:200]}")
    info = infos[0]
    return {
        "balance": float(info.get("total_balance", 0.0)),
        "currency": info.get("currency", "CNY"),
        "source": "https://api.deepseek.com/user/balance",
    }


def _probe_moonshot(api_key: str, timeout: float = DEFAULT_TIMEOUT) -> dict:
    """Moonshot/Kimi 余额: GET /v1/users/me/balance (宽容解析字段)。"""
    resp = requests.get(
        "https://api.moonshot.cn/v1/users/me/balance",
        headers={"Authorization": f"Bearer {api_key.strip()}"},
        timeout=timeout,
    )
    resp.raise_for_status()
    data = resp.json()
    inner = data.get("data") if isinstance(data.get("data"), dict) else data
    # 宽容解析: available_balance / balance / total_balance 任一字段
    bal = None
    for key in ("available_balance", "balance", "total_balance"):
        if key in inner:
            bal = inner[key]
            break
    if bal is None:
        raise ValueError(f"响应缺少余额字段: {str(data)[:200]}")
    return {
        "balance": float(bal),
        "currency": "CNY",
        "source": "https://api.moonshot.cn/v1/users/me/balance",
    }


def _probe_zhipu(api_key: str, timeout: float = DEFAULT_TIMEOUT) -> dict:
    """智谱 GLM 配额 (实验性): GET /api/monitor/usage/quota/limit。"""
    resp = requests.get(
        "https://open.bigmodel.cn/api/monitor/usage/quota/limit",
        headers={"Authorization": f"Bearer {api_key.strip()}"},
        timeout=timeout,
    )
    resp.raise_for_status()
    data = resp.json()
    # 宽容解析: 常见字段名 quota / remain / remaining / total
    bal = None
    currency = "token"
    for key in ("quota", "remain", "remaining", "total", "balance"):
        if isinstance(data.get(key), (int, float, str)):
            try:
                bal = float(data[key])
                break
            except (TypeError, ValueError):
                continue
    if bal is None:
        raise ValueError(f"响应缺少配额字段: {str(data)[:200]}")
    return {
        "balance": bal,
        "currency": currency,
        "source": "https://open.bigmodel.cn/api/monitor/usage/quota/limit",
        "experimental": True,
    }


# 探测注册表
PROBE_REGISTRY: dict[str, Callable[[str, float], dict]] = {
    "siliconflow": _probe_siliconflow,
    "deepseek": _probe_deepseek,
    "moonshot": _probe_moonshot,
    "zhipu": _probe_zhipu,
}


def supported_providers() -> list[str]:
    """返回支持自动余额探测的 provider 列表。"""
    return sorted(PROBE_REGISTRY.keys())


def probe_balance(provider: str, api_key: str,
                  timeout: float = DEFAULT_TIMEOUT) -> dict:
    """统一余额探测入口。

    Args:
        provider: 服务商 (支持别名, 见 PROVIDER_ALIASES)
        api_key:  API Key (Bearer 认证)

    Returns:
        {"supported", "provider", "balance", "currency", "source",
         "checked_at", "error", "hint"} — 失败/不支持时 balance=None
    """
    key = _normalize_provider(provider)
    result = {
        "supported": False,
        "provider": key,
        "balance": None,
        "currency": "",
        "source": "",
        "checked_at": time.time(),
        "error": "",
        "hint": "",
    }
    if not api_key:
        result["error"] = "未配置 API Key"
        result["hint"] = "在资源池配置 api_key_env 并设置对应环境变量"
        return result

    probe = PROBE_REGISTRY.get(key)
    if not probe:
        result["error"] = f"provider '{key}' 无自动余额探测"
        result["hint"] = UNSUPPORTED_HINTS.get(key, "可手动在服务商控制台查看余额")
        return result

    try:
        data = probe(api_key, timeout)
        result.update(data)
        result["supported"] = True
        result["checked_at"] = time.time()
        logger.info("[余额探测] %s 余额: %s %s", key, result["balance"],
                    result.get("currency", ""))
    except requests.RequestException as e:
        status = getattr(e.response, "status_code", None)
        result["error"] = f"HTTP {status} {e}" if status else f"网络错误: {e}"
        if status == 401:
            result["hint"] = "API Key 无效或已过期, 请检查"
        elif status == 402:
            result["hint"] = "余额不足 (402)"
        elif status == 429:
            result["hint"] = "请求过于频繁, 请稍后重试"
        logger.warning("[余额探测] %s 失败: %s", key, result["error"])
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
        logger.warning("[余额探测] %s 解析失败: %s", key, result["error"])
    return result


def probe_resource(pool: dict, timeout: float = DEFAULT_TIMEOUT) -> dict:
    """按资源池定义探测余额。

    Key 来源优先级: api_key 直填值 (R4, 文件已 gitignore) >
    api_key_env 环境变量。两者均空 → 返回未配置提示。
    """
    api_key = (pool.get("api_key") or "").strip()
    if not api_key:
        env_name = (pool.get("api_key_env") or "").strip()
        api_key = os.environ.get(env_name, "") if env_name else ""
    result = probe_balance(pool.get("provider", ""), api_key, timeout)
    result["resource_id"] = pool.get("id", "")
    return result
