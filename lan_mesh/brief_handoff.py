"""Persist confirmed project descriptions before dispatch and prevent duplicate handoffs."""
import hashlib
import json
import time
import uuid

from .project_brief import RevisionConflict


class BriefHandoff:
    """One handoff per saved revision, serialized by the active Secretary's store lock."""

    def __init__(self, store, controller):
        self.store = store
        self.controller = controller

    def _folder(self, conv_id: str):
        return self.store._path(conv_id).parent / "handoffs"

    def _write(self, conv_id: str, record: dict) -> None:
        folder = self._folder(conv_id)
        folder.mkdir(parents=True, exist_ok=True)
        self.store._atomic_write(folder / f"{record['revision']}.json",
                                 json.dumps(record, ensure_ascii=False, indent=2))

    def get(self, conv_id: str, revision: int) -> dict:
        with self.store.lock:
            path = self._folder(conv_id) / f"{revision}.json"
            if not path.is_file():
                raise KeyError("下发记录不存在")
            record = json.loads(path.read_text(encoding="utf-8"))
        db = getattr(self.controller, "db", None)
        task = db.get_task(record["task_id"]) if db is not None else None
        if task is not None:
            record["task_status"] = task.status
            record["pm_agent_id"] = task.pm_agent_id
        elif db is not None:
            # The task may not yet exist, or it may have been deleted. Do not
            # present the last recorded status as a live execution result.
            record["task_status"] = "unavailable"
        return record

    def list(self, conv_id: str) -> list[dict]:
        with self.store.lock:
            revisions = [int(path.stem) for path in self._folder(conv_id).glob("*.json") if path.stem.isdigit()]
        return [{key: value for key, value in self.get(conv_id, revision).items()
                 if key not in ("document", "markdown")}
                for revision in sorted(revisions, reverse=True)]

    def dispatch(self, conv_id: str, expected_revision: int) -> dict:
        with self.store.lock:
            current = self.store.get(conv_id)
            if current["revision"] != expected_revision:
                raise RevisionConflict("项目描述已更新，请重新审阅当前版本后下发")
            if not expected_revision:
                raise ValueError("请先整理并保存项目描述，再下发给 PM")
            path = self._folder(conv_id) / f"{expected_revision}.json"
            if path.exists():
                return self.get(conv_id, expected_revision)
            identity = uuid.uuid4().hex
            digest = hashlib.sha256(json.dumps(current["document"], ensure_ascii=False,
                                               sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            record = {"id": identity, "conv_id": conv_id, "revision": expected_revision,
                      "task_id": f"task-{identity}", "confirmed_at": time.time(),
                      "sha256": digest, "title": current["document"]["title"],
                      "document": current["document"], "markdown": current["markdown"],
                      "status": "prepared", "task_status": "unknown", "pm_agent_id": ""}
            self._write(conv_id, record)
        reference = {key: record[key] for key in ("id", "conv_id", "revision", "confirmed_at", "sha256")}
        try:
            result = self.controller.submit_task_from_chat(
                name=record["title"], description=record["markdown"], created_by="secretary",
                priority="normal", task_id=record["task_id"], input_data={"_brief": reference})
            if result.get("task_id") != record["task_id"]:
                raise ValueError("任务返回的 ID 与下发记录不一致")
            record.update(status="submitted", task_status=result.get("status", "unknown"),
                          pm_agent_id=result.get("pm_agent_id", ""))
            if result.get("status") == "failed":
                record["error"] = result.get("output_data", {}).get("error", "任务创建失败")
        except Exception as exc:
            # A timeout may occur after a task was created. Never retry this revision automatically.
            record.update(status="uncertain", error=str(exc))
        with self.store.lock:
            self._write(conv_id, record)
        return self.get(conv_id, expected_revision)
