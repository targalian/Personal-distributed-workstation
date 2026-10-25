"""工作站常驻自我优化管理器 - 汇聚建议、审批与影子执行。"""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable
from typing import Optional

from .database import Database
from .shadow_dev import ShadowDevManager


SOURCES = {"boss", "bottleneck", "agent"}
PRIORITIES = {"low", "normal", "high", "urgent"}
ACTIVE_STATUSES = {"queued", "running"}
WAITING_STATUSES = {"candidate", "waiting_boss"}
PRIORITY_ORDER = {"urgent": 0, "high": 1, "normal": 2, "low": 3}


def log(message: str) -> None:
    """Print a prefixed guardian log line."""
    print(f"[Station] {message}", flush=True)


from typing import Any

class WorkstationOptimizationManager:
    """Manage persistent optimization items and serial shadow execution."""

    def __init__(self, db: Database, shadow_dev_manager: ShadowDevManager,
                 broadcast: Optional[Callable[[str, dict], None]] = None,
                 simulate: bool = False) -> None:
        self.db = db
        self.shadow_dev_manager = shadow_dev_manager
        self.broadcast = broadcast
        self.simulate = simulate
        self._condition = threading.Condition()
        self._thread: threading.Thread | None = None
        self._stopping = False

    def start_guardian(self) -> dict:
        """Start the resident optimization guardian (idempotent)."""
        with self._condition:
            if self._thread and self._thread.is_alive():
                return {"running": True}
            self._stopping = False
            self._thread = threading.Thread(
                target=self._guardian_loop, name="workstation-optimizer", daemon=True)
            self._thread.start()
        log("工作站优化守护已启动")
        return {"running": True}

    def stop_guardian(self) -> dict:
        """Stop the guardian; queued items remain persisted for restart."""
        with self._condition:
            self._stopping = True
            self._condition.notify_all()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=2)
        return {"running": bool(self._thread and self._thread.is_alive())}

    def submit(self, source: str, title: str, description: str, priority: str='normal') -> dict:
        return call_component(self, OptimizationQueueService, 'submit', source=source, title=title, description=description, priority=priority)

    def decide(self, item_id: str, decision: str, reply: str='') -> dict:
        return call_component(self, OptimizationQueueService, 'decide', item_id=item_id, decision=decision, reply=reply)

    def list_items(self, status: str='', limit: int=100) -> list[dict]:
        return call_component(self, OptimizationQueueService, 'list_items', status=status, limit=limit)

    def get_item(self, item_id: str) -> dict | None:
        return call_component(self, OptimizationQueueService, 'get_item', item_id=item_id)

    def summary(self) -> dict:
        return call_component(self, OptimizationQueueService, 'summary')

    def guardian_status(self) -> dict:
        return call_component(self, OptimizationQueueService, 'guardian_status')

    def _guardian_loop(self) -> None:
        """Consume approved optimization items in the background."""
        while True:
            with self._condition:
                while not self._stopping:
                    self._condition.wait(timeout=5)
                if self._stopping:
                    return
            self._process_once()

    def _process_once(self) -> None:
        return call_component(self, OptimizationQueueService, '_process_once')

    def _start_item(self, item: dict) -> None:
        return call_component(self, OptimizationQueueService, '_start_item', item=item)

    def _sync_shadow_result(self, item: dict) -> None:
        return call_component(self, OptimizationQueueService, '_sync_shadow_result', item=item)

    def _shadow_result(self, report: dict) -> dict:
        return call_component(self, OptimizationQueueService, '_shadow_result', report=report)

    def _build_prompt(self, item: dict) -> str:
        return call_component(self, OptimizationQueueService, '_build_prompt', item=item)

    def _publish(self, event_type: str, item: dict) -> None:
        return call_component(self, OptimizationQueueService, '_publish', event_type=event_type, item=item)


from typing import Any
from .component_service import call_component
from .optimization_queue_service import OptimizationQueueService
