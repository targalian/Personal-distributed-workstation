"""Owned durable mesh delivery service; receipt precedes acknowledgement and execution."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import requests

from .application_lifecycle import stop_signal
from .component_service import ComponentService, call_component
from .delivery_protocol import build_result, validate_destination, validate_payload
from .http_retry import auth_headers

_init_lock = threading.RLock()


def delivery_repository(owner: object) -> Any:
    """Station uses its DB; standalone Worker owns a separate durable journal."""
    with _init_lock:
        database = vars(owner).get("db") or vars(owner).get("_delivery_db")
        if database is None:
            from .database import Database
            database = Database(str(Path(owner.data_dir) / "worker-delivery.sqlite3"))
            owner._delivery_db = database
        return database.repositories.delivery


def call_delivery(owner: object, operation: str, **arguments: Any) -> Any:
    """Route compatibility adapters through the owned Yomk component."""
    return call_component(owner, DurableDeliveryService, operation, **arguments)


def bind_pm_delivery(owner: object, pm: object) -> None:
    """Attach delivery ownership without starting work or changing PM constructors."""
    pm._delivery_owner = owner


class DurableDeliveryService(ComponentService):
    """Keep transport retries separate from execution and PM application."""

    OPERATIONS = ("start", "queue_task", "queue_cancel", "accept_task", "accept_cancel",
                  "accept_result", "pump", "inspect")

    def __init__(self, server: Any, owner: object):
        super().__init__(server, owner)
        self._pump_lock = threading.Lock()
        self._start_lock = threading.Lock()
        self._apply_lock = threading.Lock()
        self._started = False

    def start(self) -> None:
        """Recover crash receipts once, then start an owner-tracked retry loop."""
        with self._start_lock:
            if self._started or stop_signal(self.owner).is_set():
                return
            repo = delivery_repository(self.owner)
            interrupted = repo.executions("started")
            for receipt in interrupted:
                payload = json.loads(receipt["payload_json"])
                result = {"status": "failed", "output": {}, "stop_reason": "execution_interrupted",
                          "error": "Worker restarted during execution; side effects require review"}
                repo.finish_execution(receipt["execution_id"], "interrupted", result,
                                      payload["reply_url"].rstrip("/") + "/mesh-delivery/result",
                                      build_result(payload, result))
            for receipt in repo.executions("cancel_requested"):
                payload = json.loads(receipt["payload_json"])
                result = {"status": "cancelled", "output": {}}
                repo.finish_execution(receipt["execution_id"], "cancelled", result,
                                      payload["reply_url"].rstrip("/") + "/mesh-delivery/result",
                                      build_result(payload, result))
            review_count = repo.recover_applying()
            self._started = True
            self._launch(self._loop, "mesh-delivery")
            print(f"[Worker] 持久投递启动: interrupted={len(interrupted)}, review={review_count}")

    def queue_task(self, payload: dict, destination: str) -> dict:
        """Persist a dispatch before any network operation."""
        validate_payload(payload, "task")
        validate_destination(destination)
        delivery_id = f"task:{payload['attempt_id']}"
        repo = delivery_repository(self.owner)
        repo.enqueue(delivery_id, destination.rstrip("/") + "/mesh-delivery/task", payload, "task")
        print(f"[PM] 子任务投递入队: {payload['name']} attempt={payload['attempt_id']}")
        return {"ok": True, "delivery_id": delivery_id, "status": "queued"}

    def accept_task(self, payload: dict) -> dict:
        """Acknowledge only after storing an immutable execution receipt."""
        validate_payload(payload, "task")
        if stop_signal(self.owner).is_set():
            return {"ok": False, "message": "Worker stopping"}
        receipt = delivery_repository(self.owner).begin_execution(
            payload["attempt_id"], payload["parent_task_id"], payload["attempt_id"], payload)
        return self._ack(f"task:{payload['attempt_id']}", payload, receipt["status"])

    def queue_cancel(self, payload: dict, destination: str) -> None:
        """Keep cancellation retries independent of cancelled task dispatches."""
        validate_payload(payload, "task")
        validate_destination(destination)
        delivery_repository(self.owner).enqueue(
            f"cancel:{payload['attempt_id']}", destination.rstrip("/") + "/mesh-delivery/cancel",
            payload, "cancel")

    def accept_cancel(self, payload: dict) -> dict:
        """Cancellation arriving before its task creates a persistent tombstone."""
        validate_payload(payload, "task")
        repo = delivery_repository(self.owner)
        receipt = repo.begin_execution(payload["attempt_id"], payload["parent_task_id"],
                                       payload["attempt_id"], payload)
        status = repo.cancel_execution(payload["attempt_id"])
        if receipt["status"] == "queued":
            result = {"status": "cancelled", "output": {}}
            repo.finish_execution(payload["attempt_id"], "cancelled", result,
                                  payload["reply_url"].rstrip("/") + "/mesh-delivery/result",
                                  build_result(payload, result))
        agents = getattr(self.owner, "_local_sub_agents", None)
        if agents is None:
            agents = self.owner.state.sub_agents
        info = agents.get(payload.get("reporter_id"), {})
        if (info and status == "cancel_requested"
                and info.get("current_attempt_id") == payload["attempt_id"]):
            info["status"] = "cancelled"
            info["runtime"].cancel()
        return self._ack(f"cancel:{payload['attempt_id']}", payload, status)

    def accept_result(self, payload: dict) -> dict:
        """Durable inbox receipt is the transport acknowledgement, not PM completion."""
        validate_payload(payload, "result")
        state = delivery_repository(self.owner).accept_inbox(payload["result_id"], "result", payload)
        return self._ack(payload["result_id"], payload, state)

    def pump(self) -> None:
        """Retry due sends and launch queued work without blocking the event loop."""
        if not self._pump_lock.acquire(blocking=False):
            return
        try:
            if stop_signal(self.owner).is_set():
                return
            repo = delivery_repository(self.owner)
            for receipt in repo.executions("queued"):
                if stop_signal(self.owner).is_set():
                    break
                if repo.claim_execution(receipt["execution_id"]):
                    self._launch(lambda item=receipt: self._execute(item), "mesh-execute")
            for item in repo.claim_due(limit=1):
                self._send(item)
            if repo.received_results(limit=1) and self._apply_lock.acquire(blocking=False):
                self._launch(self._apply_results, "mesh-result")
        finally:
            self._pump_lock.release()

    def inspect(self) -> dict:
        """Expose journal state counts without returning task content or credentials."""
        repo = delivery_repository(self.owner)
        conn = repo.database._get_conn()
        result = {}
        for name, table in (("outbox", "mesh_delivery_outbox"),
                            ("inbox", "mesh_delivery_inbox"),
                            ("executions", "mesh_task_executions")):
            column = "status" if name == "executions" else "state"
            result[name] = {row[0]: row[1] for row in conn.execute(
                f"SELECT {column}, COUNT(*) FROM {table} GROUP BY {column}").fetchall()}
        return result

    def _loop(self) -> None:
        while not stop_signal(self.owner).is_set():
            try:
                self.pump()
            except Exception as exc:
                print(f"[Worker] 持久投递轮询失败: {exc}")
            stop_signal(self.owner).wait(0.5)

    def _launch(self, target: Any, name: str) -> None:
        thread = threading.Thread(target=target, name=name, daemon=True)
        with _init_lock:
            threads = vars(self.owner).setdefault("_threads", [])
            threads[:] = [item for item in threads if item.is_alive()]
            threads.append(thread)
            thread.start()

    def _send(self, item: dict) -> None:
        repo = delivery_repository(self.owner)
        payload = json.loads(item["payload_json"])
        if item["kind"] == "task" and self._obsolete_dispatch(payload):
            repo.supersede(item["delivery_id"])
            return
        try:
            response = requests.post(item["destination"], json=payload,
                                     headers=auth_headers(), timeout=10)
            response.raise_for_status()
            ack = response.json()
            if not (ack.get("ok") is True and ack.get("ack_id") == item["delivery_id"]
                    and ack.get("ack_sha256") == payload["payload_sha256"]):
                raise ValueError("missing or mismatched durable acknowledgement")
            repo.acknowledge(item["delivery_id"])
            print(f"[Worker] 持久投递已确认: {item['delivery_id']}")
        except Exception as exc:
            repo.fail(item["delivery_id"], str(exc), min(60, 2 ** min(item["attempts"], 6)))
            print(f"[Worker] 持久投递待重试: {item['delivery_id']}: {exc}")

    def _obsolete_dispatch(self, payload: dict) -> bool:
        repo = delivery_repository(self.owner)
        checkpoint = repo.get_pm(payload["pm_id"])
        if not checkpoint:
            return False
        if checkpoint["status"] in {"cancelled", "completed", "failed"}:
            return True
        state = json.loads(checkpoint["state_json"])
        active = state.get("active_attempts", {}).get(payload["name"], {})
        return active.get("attempt_id") != payload["attempt_id"]

    def _execute(self, receipt: dict) -> None:
        payload = json.loads(receipt["payload_json"])
        result = {"status": "failed", "output": {}, "error": "Runtime unavailable"}
        try:
            runtime, info = self._execution_runtime(payload)
            repo = delivery_repository(self.owner)
            if (info.get("status") == "cancelled" or stop_signal(self.owner).is_set()
                    or repo.execution_status(receipt["execution_id"]) in {"cancel_requested", "cancelled"}):
                result = {"status": "cancelled", "output": {}}
            else:
                info["status"] = "executing"
                info["current_attempt_id"] = payload["attempt_id"]
                info["current_task"] = payload["name"]
                result = runtime.execute(payload)
                info["status"] = "cancelled" if result.get("status") == "cancelled" else "idle"
        except Exception as exc:
            result = {"status": "failed", "output": {}, "error": str(exc)}
        if delivery_repository(self.owner).execution_status(receipt["execution_id"]) == "cancel_requested":
            result = {**result, "status": "cancelled"}
        report = build_result(payload, result)
        repo = delivery_repository(self.owner)
        saved = repo.finish_execution(
            receipt["execution_id"], report["status"], result,
            payload["reply_url"].rstrip("/") + "/mesh-delivery/result", report)
        if not saved:
            result = {**result, "status": "cancelled"}
            report = build_result(payload, result)
            repo.finish_execution(receipt["execution_id"], "cancelled", result,
                                  payload["reply_url"].rstrip("/") + "/mesh-delivery/result", report)
        print(f"[Worker] 子任务结果已持久化: {payload['name']} {report['status']}")

    def _execution_runtime(self, payload: dict) -> tuple[Any, dict]:
        station = hasattr(self.owner, "_local_sub_agents")
        agents = self.owner._local_sub_agents if station else self.owner.state.sub_agents
        agent_id = payload.get("reporter_id", "")
        with _init_lock:
            if agent_id not in agents:
                spec = dict(payload.get("agent_spec", {}))
                spec["preferred_agent_id"] = agent_id
                spec.setdefault("agent_name", payload["name"])
                spec.setdefault("skills", [payload.get("required_skill", "")])
                create = self.owner._local_create_subagent if station else self.owner.create_subagent
                create(**spec)
            info = agents[agent_id]
            return info["runtime"], info

    def _apply_results(self) -> None:
        try:
            repo = delivery_repository(self.owner)
            for item in repo.received_results():
                report = json.loads(item["payload_json"])
                pm = getattr(self.owner, "_local_pm_agent", None)
                if pm is None:
                    pm = getattr(self.owner.state, "pm_agent", None)
                checkpoint = repo.get_pm(report["pm_id"])
                if checkpoint and checkpoint["status"] in {"cancelled", "completed", "failed"}:
                    repo.complete_inbox(item["delivery_id"], {"ignored": "PM terminal"})
                elif pm is not None and pm.pm_id == report["pm_id"] and pm.running:
                    if repo.claim_result(item["delivery_id"]):
                        pm.receive_delivery_result(report)
                        pm.persist_delivery_state(item["delivery_id"])
        except Exception as exc:
            if 'item' in locals():
                delivery_repository(self.owner).require_review(item["delivery_id"], str(exc))
            print(f"[PM] 结果应用中断，收件记录保留待核对: {exc}")
        finally:
            self._apply_lock.release()

    @staticmethod
    def _ack(delivery_id: str, payload: dict, state: str) -> dict:
        return {"ok": True, "ack_id": delivery_id,
                "ack_sha256": payload["payload_sha256"], "receipt_state": state}
