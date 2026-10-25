"""CapabilityApiService: transport-neutral interaction operations behind the original API."""
from starlette.concurrency import run_in_threadpool
from .logger import get_logger
from .service_context import StationService, StationServiceContext
from .service_runtime import ServiceError
logger = get_logger('station_api')

class CapabilityApiService(StationService):
    """Resolve current components per call and preserve legacy response contracts."""

    def __init__(self, server, context: StationServiceContext):
        super().__init__(server, context, '/CapabilityApiService')

    async def list_all_tools(self, model: str=None) -> dict:
        controller = self.context.controller
        state = controller.state
        db = getattr(controller, 'db', None)
        if not controller.secretary_active or not controller.mcp_gateway:
            return {'tools': [], 'total': 0, 'error': 'Secretary 未激活或 MCP 网关未初始化'}
        tools = await run_in_threadpool(controller.mcp_gateway.list_all_tools, model_type=model)
        return {'tools': tools, 'total': len(tools), 'servers': await run_in_threadpool(controller.mcp_gateway.list_servers)}

    async def call_tool(self, payload: dict) -> dict:
        controller = self.context.controller
        state = controller.state
        db = getattr(controller, 'db', None)
        self.require_secretary()
        mcp_gateway = controller.mcp_gateway
        tool_name = payload.get('tool_name', '')
        arguments = payload.get('arguments', {})
        server_name = payload.get('server_name')
        if not tool_name:
            raise ServiceError(status_code=400, detail='缺少 tool_name')
        result = await run_in_threadpool(mcp_gateway.call_tool, tool_name, arguments, server_name)
        return result

    async def list_mcp_servers(self) -> dict:
        controller = self.context.controller
        state = controller.state
        db = getattr(controller, 'db', None)
        if not controller.secretary_active or not controller.mcp_gateway:
            return {'servers': [], 'total': 0}
        return {'servers': await run_in_threadpool(controller.mcp_gateway.list_servers), 'stats': await run_in_threadpool(controller.mcp_gateway.stats)}

    async def register_mcp_server(self, payload: dict) -> dict:
        controller = self.context.controller
        state = controller.state
        db = getattr(controller, 'db', None)
        self.require_secretary()
        name = payload.get('name', '')
        config = payload.get('config', {})
        if not name:
            raise ServiceError(status_code=400, detail='缺少 name')
        ok = await run_in_threadpool(controller.mcp_gateway.register_server, name, config)
        return {'ok': ok, 'name': name}

    async def unregister_mcp_server(self, name: str) -> dict:
        controller = self.context.controller
        state = controller.state
        db = getattr(controller, 'db', None)
        self.require_secretary()
        await run_in_threadpool(controller.mcp_gateway.unregister_server, name)
        return {'ok': True, 'name': name}

    async def route_dry_run(self, payload: dict) -> dict:
        controller = self.context.controller
        state = controller.state
        db = getattr(controller, 'db', None)
        self.require_secretary()
        model_router = controller.model_router
        if not model_router:
            raise ServiceError(status_code=503, detail='模型路由器未加载')
        text = payload.get('text', payload.get('description', ''))
        skill = payload.get('skill', '')
        project_id = payload.get('project_id', '')
        routing = model_router.route(text=text, skill=skill, project_id=project_id)
        return routing.to_dict()

    async def list_models(self) -> dict:
        controller = self.context.controller
        state = controller.state
        db = getattr(controller, 'db', None)
        if not controller.secretary_active or not controller.model_router:
            return {'models': [], 'message': 'Secretary 未激活或模型路由器未加载'}
        return {'models': controller.model_router.list_models()}

    async def list_skills(self, category: str=None) -> dict:
        """列出所有已注册技能，可按分类过滤。"""
        controller = self.context.controller
        state = controller.state
        db = getattr(controller, 'db', None)
        return [s.to_dict() for s in await run_in_threadpool(controller.skill_registry.list_skills, category)]

    async def skill_stats(self) -> dict:
        """返回技能库统计信息。"""
        controller = self.context.controller
        state = controller.state
        db = getattr(controller, 'db', None)
        return await run_in_threadpool(controller.skill_registry.stats)

    async def scan_skills(self) -> dict:
        """手动触发扫描注册新技能。"""
        controller = self.context.controller
        state = controller.state
        db = getattr(controller, 'db', None)
        result = await run_in_threadpool(controller.skill_registry.scan_and_register)
        await self.context.broadcast(state, 'skills_scanned', {'scanned': len(result), 'details': result})
        return {'ok': True, 'scanned': len(result), 'details': result}

    async def list_skill_market(self) -> dict:
        """浏览第三方技能市场 (可安装插件列表, 含 installed 标记)。"""
        controller = self.context.controller
        state = controller.state
        db = getattr(controller, 'db', None)
        return await run_in_threadpool(controller.skill_market.list_market)

    async def install_skill_from_market(self, payload: dict) -> dict:
        """从市场安装第三方插件 (校验→复制→注册, origin=market)。"""
        controller = self.context.controller
        state = controller.state
        db = getattr(controller, 'db', None)
        skill_id = (payload.get('skill_id') or '').strip()
        if not skill_id:
            raise ServiceError(status_code=400, detail='skill_id 不能为空')
        result = await run_in_threadpool(controller.skill_market.install, skill_id)
        if not result.get('ok'):
            raise ServiceError(status_code=400, detail=result.get('message', '安装失败'))
        await self.context.broadcast(state, 'skill_installed', {'skill_id': skill_id})
        return result

    async def uninstall_skill(self, skill_id: str) -> dict:
        """卸载第三方插件 (仅 origin=market; 内置技能受保护 403)。"""
        controller = self.context.controller
        state = controller.state
        db = getattr(controller, 'db', None)
        result = await run_in_threadpool(controller.skill_market.uninstall, skill_id)
        if not result.get('ok'):
            raise ServiceError(status_code=403, detail=result.get('message', '卸载失败'))
        await self.context.broadcast(state, 'skill_uninstalled', {'skill_id': skill_id})
        return result

    async def download_skill_package(self, role: str, agent_id: str=None) -> dict:
        """Worker 拉取已授权的技能包。"""
        controller = self.context.controller
        state = controller.state
        db = getattr(controller, 'db', None)
        return await run_in_threadpool(controller.skill_registry.build_skill_package, role, agent_id)

    async def get_skills_for_role(self, role: str) -> dict:
        """获取角色可用的技能列表。"""
        controller = self.context.controller
        state = controller.state
        db = getattr(controller, 'db', None)
        return [s.to_dict() for s in await run_in_threadpool(controller.skill_registry.get_skills_for_role, role)]

    async def get_skill_detail(self, skill_id: str) -> dict:
        """获取技能详情及完整内容。"""
        controller = self.context.controller
        state = controller.state
        db = getattr(controller, 'db', None)
        skill = await run_in_threadpool(controller.skill_registry.get_skill, skill_id)
        if not skill:
            raise ServiceError(status_code=404, detail='技能不存在')
        content = await run_in_threadpool(controller.skill_registry.get_skill_content, skill_id)
        assignments = await run_in_threadpool(controller.skill_registry.get_skill_assignments, skill_id)
        return {**skill.to_dict(), 'content': content.get('content', ''), 'reference': content.get('reference', ''), 'assignments': assignments}

    async def assign_skill(self, skill_id: str, payload: dict) -> dict:
        """分配技能给角色/Agent/主机。"""
        controller = self.context.controller
        state = controller.state
        db = getattr(controller, 'db', None)
        assignee_type = payload.get('assignee_type', 'role')
        assignee_id = payload.get('assignee_id', '')
        if not assignee_id:
            raise ServiceError(status_code=400, detail='assignee_id 不能为空')
        await run_in_threadpool(controller.skill_registry.assign_skill, skill_id, assignee_type, assignee_id)
        await self.context.broadcast(state, 'skill_assigned', {'skill_id': skill_id, 'assignee_type': assignee_type, 'assignee_id': assignee_id})
        return {'ok': True}

    async def revoke_skill(self, skill_id: str, assignee_type: str, assignee_id: str) -> dict:
        """撤销技能分配。"""
        controller = self.context.controller
        state = controller.state
        db = getattr(controller, 'db', None)
        await run_in_threadpool(controller.skill_registry.revoke_skill, skill_id, assignee_type, assignee_id)
        await self.context.broadcast(state, 'skill_revoked', {'skill_id': skill_id, 'assignee_type': assignee_type, 'assignee_id': assignee_id})
        return {'ok': True}

    async def list_bot_channels(self) -> dict:
        """列出所有 Bot 通道配置（脱敏）。"""
        controller = self.context.controller
        state = controller.state
        db = getattr(controller, 'db', None)
        return controller.bot_gateway.list_channels()

    async def add_bot_channel(self, payload: dict) -> dict:
        """添加或更新 Bot 通道。"""
        controller = self.context.controller
        state = controller.state
        db = getattr(controller, 'db', None)
        from .bot_gateway import BotChannel
        channel = BotChannel(channel_type=payload.get('channel_type', 'wechat_webhook'), enabled=payload.get('enabled', False), webhook_url=payload.get('webhook_url', ''), bot_token=payload.get('bot_token', ''), chat_id=payload.get('chat_id', ''), webhook_url_base=payload.get('webhook_url_base', ''), min_priority=payload.get('min_priority', 'normal'))
        controller.bot_gateway.add_channel(channel)
        return {'ok': True, 'message': f'通道 {channel.channel_type} 已配置'}

    async def remove_bot_channel(self, channel_type: str) -> dict:
        """移除 Bot 通道。"""
        controller = self.context.controller
        state = controller.state
        db = getattr(controller, 'db', None)
        controller.bot_gateway.remove_channel(channel_type)
        return {'ok': True, 'message': f'通道 {channel_type} 已移除'}

    async def test_bot_channel(self, channel_type: str) -> dict:
        """发送测试消息到指定通道。"""
        controller = self.context.controller
        state = controller.state
        db = getattr(controller, 'db', None)
        result = controller.bot_gateway.test_channel(channel_type)
        if not result.get('ok'):
            raise ServiceError(status_code=400, detail=result.get('error', '测试失败'))
        return result
