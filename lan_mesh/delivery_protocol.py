"""Versioned task/result envelopes with stable identities and full-payload checksums."""

import hashlib
import json
import uuid
from urllib.parse import urlsplit

TERMINAL_STATUSES = {"completed", "failed", "cancelled"}


def payload_digest(payload: dict) -> str:
    """Hash every envelope field except the checksum itself using canonical JSON."""
    body = {key: value for key, value in payload.items() if key != "payload_sha256"}
    raw = json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
                     allow_nan=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def seal_payload(payload: dict) -> dict:
    """Create an independent JSON envelope and attach its integrity checksum."""
    body = json.loads(json.dumps(payload, ensure_ascii=False, allow_nan=False))
    body["delivery_protocol"] = 1
    body["payload_sha256"] = payload_digest(body)
    return body


def validate_payload(payload: dict, kind: str) -> None:
    """Reject malformed or modified protocol messages before durable acceptance."""
    if payload.get("delivery_protocol") != 1:
        raise ValueError("unsupported delivery protocol")
    keys = ["pm_id", "parent_task_id", "subtask_id", "attempt_id"]
    keys += ["result_id", "task_name"] if kind == "result" else ["name", "reply_url"]
    for key in keys:
        if not isinstance(payload.get(key), str) or not payload[key].strip():
            raise ValueError(f"missing field: {key}")
    if payload.get("payload_sha256") != payload_digest(payload):
        raise ValueError("payload checksum mismatch")
    if kind == "task":
        if not isinstance(payload.get("input_data"), dict):
            raise ValueError("input_data must be an object")
        validate_destination(payload["reply_url"])
    elif payload.get("status") not in TERMINAL_STATUSES:
        raise ValueError("result must have a terminal status")


def validate_destination(destination: str) -> None:
    """Allow the same HTTP transports used by mesh endpoints, without credentials."""
    parts = urlsplit(destination)
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username:
        raise ValueError("invalid delivery destination")


def build_result(payload: dict, result: dict) -> dict:
    """Preserve complete output, usage and errors in a stable result envelope."""
    attempt_id = payload["attempt_id"]
    status = result.get("status", "failed")
    output = result.get("output", {})
    return seal_payload({
        "pm_id": payload["pm_id"], "parent_task_id": payload["parent_task_id"],
        "subtask_id": payload["subtask_id"], "attempt_id": attempt_id,
        "result_id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"lan-mesh:result:{attempt_id}")),
        "task_name": payload["name"], "reporter_id": payload.get("reporter_id", ""),
        "status": status if status in TERMINAL_STATUSES else "failed",
        "progress": 1.0 if status == "completed" else 0.0,
        "output": output if isinstance(output, dict) else {"result": str(output)},
        "execution_result": result,
        "message": str(result.get("error", "")) or f"子任务 {payload['name']} {status}",
    })
