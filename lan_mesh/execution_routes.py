"""Shared Station/Worker task execution endpoints with durable receipts and polling."""
import re
import threading
from pathlib import Path
from typing import Callable

from fastapi import APIRouter, HTTPException
from starlette.concurrency import run_in_threadpool

from .execution_receipts import ExecutionReceipts, read_execution_receipt
from .logger import get_logger

logger = get_logger("execution")


def normalize_result(result) -> dict:
    """Treat runtime/model errors as failed results on every execution path."""
    if not isinstance(result, dict):
        return {"status": "failed", "output": {}, "error": "执行器返回格式错误"}
    output = result.get("output", {})
    error = result.get("error", "")
    if isinstance(output, dict):
        error = error or output.get("error") or output.get("_error")
        for field in ("code", "summary"):
            text = output.get(field, "")
            if isinstance(text, str) and text.lstrip().startswith(("[未配置", "[LLM 调用失败", "[模型调用失败")):
                error = error or text
    status = "completed" if result.get("status") == "completed" and not error else "failed"
    return {"status": status, "output": output,
            "error": str(error or ("执行失败" if status == "failed" else ""))}


def build_execution_routes(execute: Callable, receipt_path: Path | Callable, synchronous: bool = False) -> APIRouter:
    """Commit the claim before execution. Replays only read the existing receipt."""
    router = APIRouter()
    store = None
    store_lock = threading.Lock()

    def get_store():
        nonlocal store
        with store_lock:
            if store is None:
                store = ExecutionReceipts(receipt_path() if callable(receipt_path) else receipt_path)
            return store

    def run(payload):
        try:
            result = normalize_result(execute(payload))
        except Exception as exc:
            result = {"status": "failed", "output": {}, "error": str(exc)}
        try:
            get_store().finish(payload["subtask_id"], result)
        except Exception:
            logger.exception("执行结果回执写入失败: %s", payload["subtask_id"])

    @router.post("/tasks/execute")
    async def execute_task(payload: dict):
        execution_id = payload.get("subtask_id")
        if not isinstance(execution_id, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", execution_id):
            raise HTTPException(status_code=400, detail="subtask_id 必须为 1–128 位字母、数字、点、下划线或短横线")
        if not isinstance(payload.get("name"), str) or not payload["name"].strip():
            raise HTTPException(status_code=400, detail="缺少必填字段: name")
        if not isinstance(payload.get("input_data"), dict):
            raise HTTPException(status_code=400, detail="缺少必填字段: input_data")
        try:
            claimed, receipt = get_store().claim(payload)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if claimed:
            if synchronous:
                await run_in_threadpool(run, payload)
                receipt = get_store().get(execution_id)
            else:
                try:
                    threading.Thread(target=run, args=(payload,), daemon=True,
                                     name=f"execution-{execution_id[:16]}").start()
                except Exception as exc:
                    get_store().finish(execution_id, {"status": "failed", "output": {}, "error": str(exc)})
                    receipt = get_store().get(execution_id)
        return {**receipt, "duplicate": not claimed}

    @router.get("/tasks/executions/{execution_id}")
    def execution_status(execution_id: str):
        receipt = read_execution_receipt(receipt_path() if callable(receipt_path) else receipt_path, execution_id)
        if receipt is None:
            raise HTTPException(status_code=404, detail="未找到执行回执；不能据此认定原请求未执行")
        return receipt

    return router
