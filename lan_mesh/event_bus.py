"""Station 事件总线 (M5)。

进程内发布/订阅事件通道: 后台线程 (资源记账/R7 预警等) 发布事件,
station_api 在启动时装配 sink (经 asyncio 事件循环线程安全地
广播到 /ws 客户端), 替代纯 HTTP 轮询的实时感知。

事件结构: {"type": <event_type>, "data": {...}, "ts": unix 秒}

首版事件类型:
- usage_reported   Worker 用量批次到达 Secretary
- resource_alert   R7 到期/额度预警 (新推送)
- resource_config  资源配置保存热重载
- task_stall_alert iter-41 任务停滞告警 (新推/档位升级)
- host_event       主机上线/离线 (预留)

线程安全: publish 可从任意线程调用; sink 投递由 asyncio loop 兜底。
"""
import asyncio
import threading
import time
from collections import deque
from typing import Callable, Optional

from .logger import get_logger

logger = get_logger("event_bus")


class EventBus:
    """进程内事件总线 — 单 sink 广播 + 环形历史。"""

    def __init__(self, history: int = 100):
        self._lock = threading.Lock()
        self._recent: deque = deque(maxlen=history)
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._sink: Optional[Callable[[dict], None]] = None

    def attach(self, loop: asyncio.AbstractEventLoop,
               sink: Callable[[dict], None]):
        """装配事件循环与发送回调 (由 station_api 启动时调用)。"""
        with self._lock:
            self._loop = loop
            self._sink = sink
        logger.info("[event_bus] sink 已装配 (history=%d)",
                    self._recent.maxlen)

    def detach(self):
        """解除装配 (服务关闭/热重载时调用)。"""
        with self._lock:
            self._loop = None
            self._sink = None

    @property
    def has_sink(self) -> bool:
        with self._lock:
            return self._sink is not None

    def publish(self, event_type: str, data: dict):
        """发布事件 — 任意线程可调, 异常仅告警不抛出。"""
        evt = {"type": event_type, "data": data or {}, "ts": time.time()}
        with self._lock:
            self._recent.append(evt)
            loop, sink = self._loop, self._sink
        if not sink:
            return
        try:
            if loop is not None and loop.is_running():
                loop.call_soon_threadsafe(sink, evt)
            else:
                sink(evt)
        except Exception as e:
            logger.warning("[event_bus] 事件投递失败 (%s): %s", event_type, e)

    def recent(self, n: int = 20) -> list:
        """最近 n 条事件 (按时间升序); n<=0 返回空。"""
        n = max(0, int(n))
        with self._lock:
            items = list(self._recent)
        return items[-n:] if n else []


# ── 模块级单例与钩子 ────────────────────────────────────────────

_bus = EventBus()


def get_event_bus() -> EventBus:
    return _bus


def publish_event(event_type: str, data: dict):
    """发布事件钩子 — 后台模块调用, 异常不影响主流程。"""
    try:
        _bus.publish(event_type, data)
    except Exception:
        pass


def recent_events(n: int = 20) -> list:
    """最近事件查询 (API 端点用)。"""
    try:
        return _bus.recent(n)
    except Exception:
        return []
