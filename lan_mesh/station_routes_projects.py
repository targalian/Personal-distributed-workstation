"""
Station 项目与能力路由 — 项目管理/MCP 工具/模型路由/技能库/Bot 通道 (P1 #2 拆分产物)

Secretary 激活后可用 (部分查询端点未激活时返回空):
  - 项目 CRUD 与用量查询
  - MCP 工具网关 (列表/调用/服务器注册)
  - 模型路由 (dry-run/模型列表)
  - 技能库管理 (Skill Registry: 列表/扫描/下载/分配)
  - Bot 通道管理 (列表/添加/移除/测试)
"""
from fastapi import APIRouter, HTTPException

from .logger import get_logger
from .station_routes_common import _broadcast, check_secretary

logger = get_logger("station_api")


def build_project_routes(controller) -> APIRouter:
    """Secretary 项目/工具/技能域路由。"""
    router = APIRouter()

    # 便捷别名
    db = controller.db
    state = controller.state

    # ── 项目管理 ──

    @router.post("/api/projects")
    async def create_project(payload: dict):
        check_secretary(controller)
        project_manager = controller.project_manager
        project = project_manager.create_project(
            name=payload.get("name", ""),
            description=payload.get("description", ""),
            budget_limit_usd=payload.get("budget_limit_usd", 10.0),
            allowed_models=payload.get("allowed_models", []),
            routing_strategy=payload.get("routing_strategy", "balanced"),
            workspace_base=payload.get("workspace_base", ""),
        )
        await _broadcast(state, "project_created", project.to_dict())
        return project.to_dict()

    @router.get("/api/projects")
    async def list_projects(status: str = None):
        if not controller.secretary_active:
            return {"projects": [], "total": 0}
        project_manager = controller.project_manager
        projects = project_manager.list_projects(status=status)
        return {"projects": [p.to_dict() for p in projects], "total": len(projects)}

    @router.get("/api/projects/{project_id}")
    async def get_project(project_id: str):
        check_secretary(controller)
        project_manager = controller.project_manager
        status_info = project_manager.get_project_status(project_id)
        if not status_info:
            raise HTTPException(status_code=404, detail="项目不存在")
        return status_info

    @router.put("/api/projects/{project_id}")
    async def update_project(project_id: str, payload: dict):
        check_secretary(controller)
        project_manager = controller.project_manager
        project = project_manager.update_project(
            project_id,
            name=payload.get("name"),
            description=payload.get("description"),
            budget_limit_usd=payload.get("budget_limit_usd"),
            allowed_models=payload.get("allowed_models"),
            routing_strategy=payload.get("routing_strategy"),
            status=payload.get("status"),
        )
        if not project:
            raise HTTPException(status_code=404, detail="项目不存在")
        await _broadcast(state, "project_updated", project.to_dict())
        return project.to_dict()

    @router.delete("/api/projects/{project_id}")
    async def archive_project(project_id: str):
        check_secretary(controller)
        project_manager = controller.project_manager
        if not project_manager.archive_project(project_id):
            raise HTTPException(status_code=404, detail="项目不存在")
        await _broadcast(state, "project_archived", {"project_id": project_id})
        return {"ok": True, "project_id": project_id}

    @router.get("/api/projects/{project_id}/usage")
    async def get_usage(project_id: str, limit: int = 100):
        if not controller.secretary_active:
            return {"records": [], "total": 0}
        records = db.get_usage_log(project_id, limit=limit)
        return {"records": records, "total": len(records), "project_id": project_id}

    # ── MCP 工具网关 ──

    @router.get("/tools/list")
    async def list_all_tools(model: str = None):
        if not controller.secretary_active or not controller.mcp_gateway:
            return {"tools": [], "total": 0, "error": "Secretary 未激活或 MCP 网关未初始化"}
        tools = controller.mcp_gateway.list_all_tools(model_type=model)
        return {
            "tools": tools,
            "total": len(tools),
            "servers": controller.mcp_gateway.list_servers(),
        }

    @router.post("/tools/call")
    async def call_tool(payload: dict):
        check_secretary(controller)
        mcp_gateway = controller.mcp_gateway
        tool_name = payload.get("tool_name", "")
        arguments = payload.get("arguments", {})
        server_name = payload.get("server_name")
        if not tool_name:
            raise HTTPException(status_code=400, detail="缺少 tool_name")
        result = mcp_gateway.call_tool(tool_name, arguments, server_name)
        return result

    @router.get("/tools/servers")
    async def list_mcp_servers():
        if not controller.secretary_active or not controller.mcp_gateway:
            return {"servers": [], "total": 0}
        return {"servers": controller.mcp_gateway.list_servers(), "stats": controller.mcp_gateway.stats()}

    @router.post("/tools/servers")
    async def register_mcp_server(payload: dict):
        check_secretary(controller)
        name = payload.get("name", "")
        config = payload.get("config", {})
        if not name:
            raise HTTPException(status_code=400, detail="缺少 name")
        ok = controller.mcp_gateway.register_server(name, config)
        return {"ok": ok, "name": name}

    @router.delete("/tools/servers/{name}")
    async def unregister_mcp_server(name: str):
        check_secretary(controller)
        controller.mcp_gateway.unregister_server(name)
        return {"ok": True, "name": name}

    # ── 模型路由 ──

    @router.post("/api/route/dry-run")
    async def route_dry_run(payload: dict):
        check_secretary(controller)
        model_router = controller.model_router
        if not model_router:
            raise HTTPException(status_code=503, detail="模型路由器未加载")
        text = payload.get("text", payload.get("description", ""))
        skill = payload.get("skill", "")
        project_id = payload.get("project_id", "")
        routing = model_router.route(text=text, skill=skill, project_id=project_id)
        return routing.to_dict()

    @router.get("/api/models")
    async def list_models():
        if not controller.secretary_active or not controller.model_router:
            return {"models": [], "message": "Secretary 未激活或模型路由器未加载"}
        return {"models": controller.model_router.list_models()}

    # ════════════════════════════════════════════════════════════
    #  技能库管理 (Skill Registry)
    # ════════════════════════════════════════════════════════════

    @router.get("/api/station/skills")
    async def list_skills(category: str = None):
        """列出所有已注册技能，可按分类过滤。"""
        return [s.to_dict() for s in controller.skill_registry.list_skills(category)]

    @router.get("/api/station/skills/stats")
    async def skill_stats():
        """返回技能库统计信息。"""
        return controller.skill_registry.stats()

    @router.get("/api/station/skills/scan")
    async def scan_skills():
        """手动触发扫描注册新技能。"""
        result = controller.skill_registry.scan_and_register()
        await _broadcast(state, "skills_scanned", {"scanned": len(result), "details": result})
        return {"ok": True, "scanned": len(result), "details": result}

    # ── 技能市场 (iter-61 F5.3 插件系统: 第三方 Skill 浏览/安装/卸载) ──
    # 注意: market 路由必须定义在 /{skill_id} 之前, 防 "market" 被路径参数捕获

    @router.get("/api/station/skills/market")
    async def list_skill_market():
        """浏览第三方技能市场 (可安装插件列表, 含 installed 标记)。"""
        return controller.skill_market.list_market()

    @router.post("/api/station/skills/market/install")
    async def install_skill_from_market(payload: dict):
        """从市场安装第三方插件 (校验→复制→注册, origin=market)。"""
        skill_id = (payload.get("skill_id") or "").strip()
        if not skill_id:
            raise HTTPException(status_code=400, detail="skill_id 不能为空")
        result = controller.skill_market.install(skill_id)
        if not result.get("ok"):
            raise HTTPException(status_code=400, detail=result.get("message", "安装失败"))
        await _broadcast(state, "skill_installed", {"skill_id": skill_id})
        return result

    @router.delete("/api/station/skills/{skill_id}")
    async def uninstall_skill(skill_id: str):
        """卸载第三方插件 (仅 origin=market; 内置技能受保护 403)。"""
        result = controller.skill_market.uninstall(skill_id)
        if not result.get("ok"):
            raise HTTPException(status_code=403, detail=result.get("message", "卸载失败"))
        await _broadcast(state, "skill_uninstalled", {"skill_id": skill_id})
        return result

    @router.get("/api/station/skills/download")
    async def download_skill_package(role: str, agent_id: str = None):
        """Worker 拉取已授权的技能包。"""
        return controller.skill_registry.build_skill_package(role, agent_id)

    @router.get("/api/station/skills/role/{role}")
    async def get_skills_for_role(role: str):
        """获取角色可用的技能列表。"""
        return [s.to_dict() for s in controller.skill_registry.get_skills_for_role(role)]

    @router.get("/api/station/skills/{skill_id}")
    async def get_skill_detail(skill_id: str):
        """获取技能详情及完整内容。"""
        skill = controller.skill_registry.get_skill(skill_id)
        if not skill:
            raise HTTPException(status_code=404, detail="技能不存在")
        content = controller.skill_registry.get_skill_content(skill_id)
        assignments = controller.skill_registry.get_skill_assignments(skill_id)
        return {**skill.to_dict(), "content": content.get("content", ""),
                "reference": content.get("reference", ""), "assignments": assignments}

    @router.post("/api/station/skills/{skill_id}/assign")
    async def assign_skill(skill_id: str, payload: dict):
        """分配技能给角色/Agent/主机。"""
        assignee_type = payload.get("assignee_type", "role")
        assignee_id = payload.get("assignee_id", "")
        if not assignee_id:
            raise HTTPException(status_code=400, detail="assignee_id 不能为空")
        controller.skill_registry.assign_skill(skill_id, assignee_type, assignee_id)
        await _broadcast(state, "skill_assigned", {
            "skill_id": skill_id, "assignee_type": assignee_type, "assignee_id": assignee_id
        })
        return {"ok": True}

    @router.delete("/api/station/skills/{skill_id}/assign")
    async def revoke_skill(skill_id: str, assignee_type: str, assignee_id: str):
        """撤销技能分配。"""
        controller.skill_registry.revoke_skill(skill_id, assignee_type, assignee_id)
        await _broadcast(state, "skill_revoked", {
            "skill_id": skill_id, "assignee_type": assignee_type, "assignee_id": assignee_id
        })
        return {"ok": True}

    # ════════════════════════════════════════════════════════════
    #  Bot 通道管理 (手机消息通道)
    # ════════════════════════════════════════════════════════════

    @router.get("/api/station/bot/channels")
    async def list_bot_channels():
        """列出所有 Bot 通道配置（脱敏）。"""
        return controller.bot_gateway.list_channels()

    @router.post("/api/station/bot/channels")
    async def add_bot_channel(payload: dict):
        """添加或更新 Bot 通道。"""
        from .bot_gateway import BotChannel
        channel = BotChannel(
            channel_type=payload.get("channel_type", "wechat_webhook"),
            enabled=payload.get("enabled", False),
            webhook_url=payload.get("webhook_url", ""),
            bot_token=payload.get("bot_token", ""),
            chat_id=payload.get("chat_id", ""),
            webhook_url_base=payload.get("webhook_url_base", ""),
            min_priority=payload.get("min_priority", "normal"),
        )
        controller.bot_gateway.add_channel(channel)
        return {"ok": True, "message": f"通道 {channel.channel_type} 已配置"}

    @router.delete("/api/station/bot/channels/{channel_type}")
    async def remove_bot_channel(channel_type: str):
        """移除 Bot 通道。"""
        controller.bot_gateway.remove_channel(channel_type)
        return {"ok": True, "message": f"通道 {channel_type} 已移除"}

    @router.post("/api/station/bot/test/{channel_type}")
    async def test_bot_channel(channel_type: str):
        """发送测试消息到指定通道。"""
        result = controller.bot_gateway.test_channel(channel_type)
        if not result.get("ok"):
            raise HTTPException(status_code=400, detail=result.get("error", "测试失败"))
        return result

    return router
