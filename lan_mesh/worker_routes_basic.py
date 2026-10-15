"""
Worker 基础路由 — 本机信息/共享文件/任务执行 (iter-31 拆分产物)

始终可用 (Worker 独立进程与 Station 内嵌 Worker 共用):
  - 本机配置信息 (/info)
  - 共享文件夹列表/下载/上传 (/shared*)
  - Secretary 分发子任务执行 (/tasks/execute) 与 CLI Agent 状态 (/agents/cli-status)

路由函数名/端点路径/行为与原 api.py create_worker_router 逐字一致。
"""
from typing import Any, Callable, Optional
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import FileResponse

from .shared_folder import SharedFolderManager
from .execution_routes import build_execution_routes


def build_basic_routes(
    collect_info_fn: Callable[[], Any],
    shared_folder: SharedFolderManager,
    agent_runtime: Optional[Any] = None,
    receipt_path: Optional[Path] = None,
) -> APIRouter:
    """本机信息 / 共享文件 / 任务执行端点。"""
    router = APIRouter()

    @router.get("/info")
    async def get_info():
        """返回本机完整配置信息。"""
        return collect_info_fn().to_dict()

    # ── 任务执行端点 ─────────────────────────────────────

    def execute_task(payload: dict):
        if not agent_runtime:
            return {"status": "failed", "error": "Agent 运行时未初始化", "output": {}}
        return agent_runtime.execute(payload)

    router.include_router(build_execution_routes(
        execute_task, receipt_path or Path.home() / ".lan_mesh" / "worker-executions.sqlite3",
        synchronous=True))

    @router.get("/agents/cli-status")
    async def cli_agent_status():
        """查询本机可用的 CLI Agent 后端状态。"""
        if not agent_runtime:
            return {"agents": [], "preferred": None, "error": "Agent 运行时未初始化"}
        return agent_runtime.list_cli_agents()

    @router.get("/shared")
    async def list_shared():
        """列出共享文件夹内容。"""
        return {
            "folder": str(shared_folder.path),
            "files": shared_folder.list_files(),
            "file_count": shared_folder.file_count(),
        }

    @router.get("/shared/{file_path:path}")
    async def download_shared(file_path: str):
        """下载共享文件。"""
        try:
            full = shared_folder.get_file(file_path)
            return FileResponse(
                path=str(full),
                filename=full.name,
                media_type="application/octet-stream",
            )
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail=f"文件不存在: {file_path}")
        except ValueError as e:
            raise HTTPException(status_code=403, detail=str(e))

    @router.post("/shared")
    async def upload_shared(file: UploadFile = File(...)):
        """上传文件到共享目录。"""
        data = await file.read()
        dest = shared_folder.save_upload(file.filename or "upload.bin", data)
        return {
            "ok": True,
            "filename": dest.name,
            "path": str(dest),
            "size": len(data),
        }

    return router
