"""Durable background brief generation with idempotent starts and atomic proposals."""
import json
import logging
import os
import re
import threading
import time
import uuid

from .brief_review import brief_changes
from .pm_lease import acquire_pm_lease
from .project_brief import ProjectBrief, RevisionConflict, render_markdown

log = logging.getLogger(__name__)
_PROCESS = f"{os.getpid()}:{uuid.uuid4().hex}"
_CAPACITY = threading.BoundedSemaphore(2)
_LIVE = set()
ACTIVE = {"queued", "running"}


class BriefGeneration:
    def __init__(self, chat):
        self.chat = chat
        self.store = chat.briefs

    def _read(self, conv_id):
        # A second process must not declare a live request interrupted. The lease
        # remains held for the lifetime of the process, including model threads.
        try:
            acquire_pm_lease(self.store.root / "generation")
        except ValueError as exc:
            raise RevisionConflict("另一个进程仍持有整理任务，请连接原服务或停止重复实例") from exc
        path = self.store._path(conv_id)
        jobs = json.loads(path.read_text(encoding="utf-8")).get("_generation_jobs", {}) if path.exists() else {}
        changed = False
        for job in jobs.values():
            live_key = (str(self.store.root.resolve()), conv_id, job["id"])
            if job["status"] in ACTIVE and (job["owner"] != _PROCESS or live_key not in _LIVE):
                job.update(status="interrupted", updated_at=time.time(),
                           message="服务曾中断，模型请求结果待核实；未自动重试，重新整理可能再次计费。")
                changed = True
        if changed:
            self.store._write(conv_id, self.store.get(conv_id), generation_jobs=jobs)
        return jobs

    @staticmethod
    def _public(job):
        if job is None:
            return None
        return {key: value for key, value in job.items() if key not in {"owner", "candidate"}}

    def get(self, conv_id: str, request_id: str | None = None) -> dict:
        with self.store.lock:
            jobs = self._read(conv_id)
            job = jobs.get(request_id) if request_id else next(reversed(jobs.values()), None)
            if request_id and not job:
                raise KeyError(request_id)
            return {"job": self._public(job)}

    def candidate_markdown(self, conv_id: str, request_id: str) -> str:
        with self.store.lock:
            job = self._read(conv_id).get(request_id)
            if not job or not job.get("candidate"):
                raise KeyError(request_id)
            return "> 过期 AI 整理结果，仅供比较，未保存为项目描述。\n\n" + render_markdown(
                ProjectBrief.model_validate(job["candidate"]), job["base_revision"])

    def start(self, conv_id: str, request_id: str, expected_revision: int) -> dict:
        if not re.fullmatch(r"[A-Za-z0-9_-]{8,100}", request_id):
            raise ValueError("非法整理请求编号")
        with self.store.lock:
            jobs = self._read(conv_id)
            if request_id in jobs:
                job = jobs[request_id]
                if job["base_revision"] != expected_revision:
                    raise RevisionConflict("同一整理请求不能更换文档版本")
                return {"job": self._public(job)}
            if any(job["status"] in ACTIVE for job in jobs.values()):
                raise RevisionConflict("该对话已有整理任务，请查询状态，不要重复提交")
            prompt, options = self.chat.prepare_project_brief(conv_id, expected_revision)
            if not _CAPACITY.acquire(blocking=False):
                raise RevisionConflict("正在整理的任务已达上限（2 个），请稍后再试")
            job = {"id": request_id, "base_revision": expected_revision, "owner": _PROCESS,
                   "status": "queued", "created_at": time.time(), "updated_at": time.time(),
                   "message": "已登记，等待开始整理。", "proposal_id": None}
            jobs[request_id] = job
            live_key = (str(self.store.root.resolve()), conv_id, request_id)
            _LIVE.add(live_key)
            try:
                self.store._write(conv_id, self.store.get(conv_id), generation_jobs=jobs)
                thread = threading.Thread(target=self._run, args=(conv_id, request_id, prompt, options),
                                          name="brief-generation", daemon=True)
                thread.start()
            except Exception:
                _LIVE.discard(live_key)
                _CAPACITY.release()
                # If the durable queued record exists, never repeat the call on a
                # replay. A later query marks this terminal without charging.
                job.update(status="failed", message="整理任务未能启动，请重新整理。", updated_at=time.time())
                self.store._write(conv_id, self.store.get(conv_id), generation_jobs=jobs)
                raise
            return {"job": self._public(job)}

    def _update(self, conv_id, request_id, **fields):
        jobs = self._read(conv_id)
        job = jobs[request_id]
        if job["owner"] != _PROCESS or job["status"] not in ACTIVE:
            raise RevisionConflict("整理任务归属已变化")
        job.update(fields, updated_at=time.time())
        self.store._write(conv_id, self.store.get(conv_id), generation_jobs=jobs)

    def _finish(self, conv_id, request_id, candidate):
        with self.store.lock:
            jobs = self._read(conv_id)
            job = jobs[request_id]
            if job["owner"] != _PROCESS or job["status"] not in ACTIVE:
                return
            current = self.store.get(conv_id)
            if not self.chat._get_conv_meta(conv_id):
                job.update(status="failed", message="对话已删除，未发布整理结果。")
            elif current["revision"] != job["base_revision"] or current["proposal"]:
                job.update(status="conflict", message="文档或待审阅建议已变化，原稿已保留；可导出本次结果比较。",
                           candidate=candidate)
            else:
                changes = brief_changes(current["document"], candidate)
                if changes:
                    proposal_id = uuid.uuid4().hex
                    current["proposal"] = {"id": proposal_id, "base_revision": job["base_revision"],
                                           "created_at": time.time(), "document": candidate, "changes": changes}
                    job.update(status="review", message="整理完成，请到 AI 建议逐项审阅。", proposal_id=proposal_id)
                else:
                    job.update(status="no_changes", message="整理完成，与已保存文档一致，没有新增建议。")
            job["updated_at"] = time.time()
            # Proposal and completion become visible in the same atomic replace.
            self.store._write(conv_id, current, generation_jobs=jobs)

    def _run(self, conv_id, request_id, prompt, options):
        try:
            with self.store.lock:
                self._update(conv_id, request_id, status="running", message="正在整理，可继续编辑或切换会话。")
            candidate = self.chat.call_project_brief(prompt, options)
            self._finish(conv_id, request_id, candidate)
        except Exception:
            # Provider errors may contain credentials or prompt text. Do not expose
            # their raw message in the durable record or browser.
            log.warning("Brief generation did not complete: %s", request_id)
            try:
                with self.store.lock:
                    self._update(conv_id, request_id, status="failed",
                                 message="整理失败或结果未能保存，原稿已保留；未自动重试，重新整理可能再次计费。")
            except Exception:
                log.error("Brief generation status could not be saved: %s", request_id)
        finally:
            with self.store.lock:
                _LIVE.discard((str(self.store.root.resolve()), conv_id, request_id))
            _CAPACITY.release()
