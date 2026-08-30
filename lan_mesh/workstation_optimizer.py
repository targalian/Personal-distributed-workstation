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

    def submit(self, source: str, title: str, description: str,
               priority: str = "normal") -> dict:
        """Create an optimization item from Boss, bottleneck, or Agent input."""
        source = source.lower().strip()
        priority = priority.lower().strip()
        if source not in SOURCES:
            raise ValueError("source 必须是 boss/bottleneck/agent")
        if priority not in PRIORITIES:
            raise ValueError("priority 必须是 low/normal/high/urgent")
        title = title.strip()
        description = description.strip()
        if not title:
            raise ValueError("title 不能为空")
        now = time.time()
        status = "queued" if source == "boss" else (
            "waiting_boss" if source == "bottleneck" else "candidate")
        item = {
            "id": f"opt-{uuid.uuid4().hex[:12]}", "source": source,
            "title": title[:120], "description": description[:4000],
            "priority": priority, "status": status, "decision_reply": "",
            "shadow_run_id": "", "result": {},
            "created_at": now, "updated_at": now,
        }
        self.db.save_optimization_item(item)
        event = ("workstation_optimization_waiting_boss"
                 if status in WAITING_STATUSES else
                 "workstation_optimization_created")
        self._publish(event, item)
        log(f"工作站优化项已创建: {item['id']} ({source}/{status})")
        self.start_guardian()
        return item

    def decide(self, item_id: str, decision: str, reply: str = "") -> dict:
        """Apply a Boss decision to a candidate optimization item."""
        item = self.db.get_optimization_item(item_id)
        if not item:
            raise KeyError("优化项不存在")
        if item["status"] not in WAITING_STATUSES:
            raise ValueError("该优化项当前不需要 Boss 决策")
        decision = decision.lower().strip()
        if decision not in {"approve", "reject", "clarify"}:
            raise ValueError("decision 必须是 approve/reject/clarify")
        if decision == "approve":
            item["status"] = "queued"
        elif decision == "reject":
            item["status"] = "rejected"
        if reply.strip():
            previous = item.get("decision_reply", "")
            item["decision_reply"] = (
                f"{previous}\n{reply.strip()}".strip() if previous else reply.strip())
        item["updated_at"] = time.time()
        self.db.save_optimization_item(item)
        self._publish("workstation_optimization_updated", item)
        log(f"工作站优化项已决策: {item_id} ({decision})")
        self.start_guardian()
        return item

    def list_items(self, status: str = "", limit: int = 100) -> list[dict]:
        """List optimization items, optionally filtered by status."""
        return self.db.list_optimization_items(status=status, limit=limit)

    def get_item(self, item_id: str) -> dict | None:
        """Return one optimization item."""
        return self.db.get_optimization_item(item_id)

    def summary(self) -> dict:
        """Return guardian status and queue counters."""
        items = self.list_items(limit=500)
        running = next((item for item in items if item["status"] == "running"), None)
        completed = next(
            (item for item in items if item["status"] == "completed"), None)
        guardian_running = bool(self._thread and self._thread.is_alive())
        return {
            "guardian_running": guardian_running,
            "queue_count": sum(item["status"] == "queued" for item in items),
            "waiting_boss_count": sum(
                item["status"] in WAITING_STATUSES for item in items),
            "running_item": running,
            "recent_completed": completed,
        }

    def guardian_status(self) -> dict:
        """Return only guardian lifecycle state."""
        return {
            "running": bool(self._thread and self._thread.is_alive()),
            "stopping": self._stopping,
        }

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
        """Poll running shadow work and submit one queued optimization item."""
        for item in self.list_items(status="running", limit=50):
            self._sync_shadow_result(item)
        queued = self.list_items(status="queued", limit=100)
        if queued:
            self._start_item(sorted(
                queued, key=lambda item: PRIORITY_ORDER.get(item["priority"], 2))[0])

    def _start_item(self, item: dict) -> None:
        """Submit one approved optimization item to shadow development."""
        prompt = self._build_prompt(item)
        try:
            record = self.shadow_dev_manager.submit(
                task=prompt, simulate=self.simulate)
            item["shadow_run_id"] = record["run_id"]
            item["status"] = "running"
            item["result"] = {"shadow_status": record.get("status", "queued")}
        except Exception as exc:
            item["status"] = "failed"
            item["result"] = {"error": str(exc)}
            log(f"工作站优化项启动失败: {item['id']}: {exc}")
        item["updated_at"] = time.time()
        self.db.save_optimization_item(item)
        self._publish("workstation_optimization_updated", item)

    def _sync_shadow_result(self, item: dict) -> None:
        """Update an optimization item from its shadow run report."""
        run_id = item.get("shadow_run_id", "")
        report = self.shadow_dev_manager.get_run(run_id) if run_id else None
        if not report:
            return
        status = str(report.get("status", report.get("verdict", "")))
        if status in {"READY_FOR_REVIEW", "completed"}:
            item["status"] = "completed"
            item["result"] = self._shadow_result(report)
            log(f"工作站优化项已生成待审 diff: {item['id']}")
        elif status in {"GATES_FAILED", "AGENT_FAILED", "ERROR", "failed"}:
            item["status"] = "failed"
            item["result"] = self._shadow_result(report)
            log(f"工作站优化项执行失败: {item['id']} ({status})")
        else:
            return
        item["updated_at"] = time.time()
        self.db.save_optimization_item(item)
        event = ("workstation_optimization_completed" if item["status"] == "completed"
                 else "workstation_optimization_updated")
        self._publish(event, item)

    def _shadow_result(self, report: dict) -> dict:
        """Extract a compact result from a shadow report."""
        diff = report.get("diff", {}) if isinstance(report.get("diff"), dict) else {}
        return {
            "verdict": report.get("verdict", report.get("status", "")),
            "diff_file": diff.get("diff_file", ""),
            "diff_bytes": diff.get("bytes", 0),
            "error": report.get("error", ""),
        }

    def _build_prompt(self, item: dict) -> str:
        """Build the shadow agent prompt for one optimization item."""
        reply = item.get("decision_reply", "")
        return (
            "【工作站常驻优化】\n"
            f"标题: {item['title']}\n"
            f"来源: {item['source']}\n"
            f"优先级: {item['priority']}\n"
            f"说明: {item['description']}\n"
            + (f"Boss 补充: {reply}\n" if reply else "")
            + "请在影子副本内完成最小安全实现, 并运行全量验证。"
        )

    def _publish(self, event_type: str, item: dict) -> None:
        """Broadcast one optimization event, isolating callback failures."""
        if not self.broadcast:
            return
        try:
            self.broadcast(event_type, {"item": dict(item)})
        except Exception as exc:
            log(f"工作站优化事件广播失败: {exc}")
