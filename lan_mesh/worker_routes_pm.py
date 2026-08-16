"""
Worker 角色与 PM 路由 — 角色管理/PM 生命周期/子 Agent 管理 (iter-31 拆分产物)

始终可用 (Worker 独立进程与 Station 内嵌 Worker 共用), 依赖 role_manager
(WorkerAgent 实例, 未提供时各端点 503):
  - 远程 Secretary 子进程管理 (/role/start-secretary 等)
  - PM Agent 生命周期 (/role/start-pm, stop/cancel/pause, pm-status)
  - 子 Agent 创建/进度上报/prompt 更新/列表/反向注入 (/pm/*)

路由函数名/端点路径/行为与原 api.py create_worker_router 逐字一致。
"""
from typing import Any, Optional

from fastapi import APIRouter, HTTPException


def build_pm_routes(role_manager: Optional[Any] = None) -> APIRouter:
    """角色管理 / PM 生命周期 / 子 Agent 管理端点。"""
    router = APIRouter()

    # ── 角色管理端点 (远程 Secretary 分配) ──────────────────

    @router.post("/role/start-secretary")
    async def start_secretary(payload: dict = None):
        """在本机启动 Secretary 子进程 (由 Station Director 远程调用)。"""
        if not role_manager:
            raise HTTPException(status_code=503, detail="角色管理未初始化")
        port = (payload or {}).get("port")
        result = role_manager.start_secretary(port)
        if not result.get("ok"):
            raise HTTPException(status_code=409, detail=result.get("message", "启动失败"))
        return result

    @router.post("/role/stop-secretary")
    async def stop_secretary():
        """停止本机的 Secretary 子进程。"""
        if not role_manager:
            raise HTTPException(status_code=503, detail="角色管理未初始化")
        result = role_manager.stop_secretary()
        return result

    @router.get("/role/status")
    async def role_status():
        """查询本机 Secretary 运行状态。"""
        if not role_manager:
            return {"running": False, "role_manager": "unavailable"}
        return role_manager.get_secretary_status()

    # ── PM Agent 端点 (Secretary 远程激活 PM) ───────────────────

    @router.post("/role/start-pm")
    async def start_pm(payload: dict):
        """在本 Worker 上启动 PM Agent 接管任务。

        请求体:
            {
                "task_id": "task-xxx",
                "secretary_url": "http://192.168.1.100:45470",
                "task_data": {}  // 可选, 任务详情
            }
        """
        if not role_manager:
            raise HTTPException(status_code=503, detail="角色管理未初始化")
        task_id = payload.get("task_id", "")
        secretary_url = payload.get("secretary_url", "")
        task_data = payload.get("task_data")
        if not task_id or not secretary_url:
            raise HTTPException(status_code=400, detail="缺少 task_id 或 secretary_url")
        result = role_manager.start_pm(task_id, secretary_url, task_data)
        if not result.get("ok"):
            raise HTTPException(status_code=409, detail=result.get("message", "启动失败"))
        return result

    @router.post("/role/stop-pm")
    async def stop_pm():
        """停止本 Worker 上的 PM Agent。"""
        if not role_manager:
            raise HTTPException(status_code=503, detail="角色管理未初始化")
        return role_manager.stop_pm()

    @router.get("/role/pm-status")
    async def pm_status():
        """查询本 Worker 上的 PM Agent 运行状态。"""
        if not role_manager:
            return {"running": False, "role_manager": "unavailable"}
        return role_manager.get_pm_status()

    # ── PM 子 Agent 端点 (PM 调用, 在目标 Worker 创建子 Agent) ──

    @router.post("/pm/create-subagent")
    async def create_subagent(payload: dict):
        """在本 Worker 上为 PM 创建子 Agent。

        请求体:
            {
                "agent_name": "code-reviewer-01",
                "skills": ["code_review"],
                "task_description": "审查 auth 模块代码",
                "system_prompt": "PM 生成的定制 prompt (可选)",
                "preferred_agent_id": "sub-abc123 (可选, PM预生成)"
            }
        """
        if not role_manager:
            raise HTTPException(status_code=503, detail="角色管理未初始化")
        agent_name = payload.get("agent_name", "")
        skills = payload.get("skills", [])
        task_desc = payload.get("task_description", "")
        system_prompt = payload.get("system_prompt", "")
        preferred_id = payload.get("preferred_agent_id", "")
        if not agent_name:
            raise HTTPException(status_code=400, detail="缺少 agent_name")
        return role_manager.create_subagent(agent_name, skills, task_desc, system_prompt, preferred_id)

    @router.post("/pm/progress-report")
    async def pm_progress_report(payload: dict):
        """子 Agent 向 PM 上报进度 (Worker 转发给 PM Agent)。

        请求体:
            {
                "pm_id": "pm-xxx",
                "reporter_id": "sub-xxx",
                "task_name": "代码审查",
                "progress": 0.8,
                "status": "in_progress",
                "message": "已完成 80%"
            }
        """
        if not role_manager:
            raise HTTPException(status_code=503, detail="角色管理未初始化")
        return role_manager.forward_progress_report(payload)

    @router.get("/pm/subagents")
    async def list_subagents():
        """列出本 Worker 上的所有子 Agent 状态。"""
        if not role_manager:
            return {"sub_agents": [], "total": 0}
        return role_manager.get_subagent_status()

    @router.post("/pm/update-prompt")
    async def update_subagent_prompt(payload: dict):
        """动态更新子 Agent 的 system prompt (优化2)。

        请求体:
            {
                "agent_id": "sub-abc123",
                "system_prompt": "新的 prompt 内容"
            }
        """
        if not role_manager:
            raise HTTPException(status_code=503, detail="角色管理未初始化")
        agent_id = payload.get("agent_id", "")
        new_prompt = payload.get("system_prompt", "")
        if not agent_id:
            raise HTTPException(status_code=400, detail="缺少 agent_id")
        return role_manager.update_subagent_prompt(agent_id, new_prompt)

    # ── 优化7: 反向沟通通道 ──

    @router.post("/pm/inject-input")
    async def pm_inject_input(payload: dict):
        """向 PM Agent 注入来自 Secretary/Boss 的回复 (反向沟通通道)。

        请求体:
            {
                "response": "Boss的回复文本",
                "choice": "选中的选项",
                "task_name": "请求决策"
            }
        """
        if not role_manager:
            raise HTTPException(status_code=503, detail="角色管理未初始化")
        result = role_manager.inject_pm_input(payload)
        if not result.get("ok"):
            raise HTTPException(status_code=409, detail=result.get("message", "注入失败"))
        return result

    # ── 优化8: PM 控制端点 ──

    @router.post("/role/cancel-pm")
    async def cancel_pm():
        """取消本 Worker 上的 PM Agent 任务。"""
        if not role_manager:
            raise HTTPException(status_code=503, detail="角色管理未初始化")
        return role_manager.cancel_pm()

    @router.post("/role/pause-pm")
    async def pause_pm():
        """暂停本 Worker 上的 PM Agent 任务。"""
        if not role_manager:
            raise HTTPException(status_code=503, detail="角色管理未初始化")
        return role_manager.pause_pm()

    return router
