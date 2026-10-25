"""ProjectApiService: transport-neutral interaction operations behind the original API."""

from starlette.concurrency import run_in_threadpool
from .logger import get_logger
from .service_context import StationService, StationServiceContext
from .service_runtime import ServiceError

logger = get_logger("station_api")

class ProjectApiService(StationService):
    """Resolve current components per call and preserve legacy response contracts."""

    def __init__(self, server, context: StationServiceContext):
        super().__init__(server, context, "/ProjectApiService")

    async def create_project(self, payload: dict) -> dict:
        controller = self.context.controller
        state = controller.state
        db = getattr(controller, 'db', None)
        self.require_secretary()
        project_manager = controller.project_manager
        project = await run_in_threadpool(project_manager.create_project, name=payload.get('name', ''), description=payload.get('description', ''), budget_limit_usd=payload.get('budget_limit_usd', 10.0), allowed_models=payload.get('allowed_models', []), routing_strategy=payload.get('routing_strategy', 'balanced'), workspace_base=payload.get('workspace_base', ''), charter=payload.get('charter', {}), roadmap=payload.get('roadmap', []), decisions=payload.get('decisions', []))
        await self.context.broadcast(state, 'project_created', project.to_dict())
        return project.to_dict()

    async def list_projects(self, status: str=None) -> dict:
        controller = self.context.controller
        state = controller.state
        db = getattr(controller, 'db', None)
        if not controller.secretary_active:
            return {'projects': [], 'total': 0}
        project_manager = controller.project_manager
        projects = await run_in_threadpool(project_manager.list_projects, status=status)
        return {'projects': [p.to_dict() for p in projects], 'total': len(projects)}

    async def get_project(self, project_id: str) -> dict:
        controller = self.context.controller
        state = controller.state
        db = getattr(controller, 'db', None)
        self.require_secretary()
        project_manager = controller.project_manager
        status_info = await run_in_threadpool(project_manager.get_project_status, project_id)
        if not status_info:
            raise ServiceError(status_code=404, detail='项目不存在')
        return status_info

    async def update_project(self, project_id: str, payload: dict) -> dict:
        controller = self.context.controller
        state = controller.state
        db = getattr(controller, 'db', None)
        self.require_secretary()
        project_manager = controller.project_manager
        project = await run_in_threadpool(project_manager.update_project, project_id, name=payload.get('name'), description=payload.get('description'), budget_limit_usd=payload.get('budget_limit_usd'), allowed_models=payload.get('allowed_models'), routing_strategy=payload.get('routing_strategy'), status=payload.get('status'))
        if not project:
            raise ServiceError(status_code=404, detail='项目不存在')
        await self.context.broadcast(state, 'project_updated', project.to_dict())
        return project.to_dict()

    async def get_project_blueprint(self, project_id: str) -> dict:
        controller = self.context.controller
        state = controller.state
        db = getattr(controller, 'db', None)
        self.require_secretary()
        project = await run_in_threadpool(controller.project_manager.get_project, project_id)
        if not project:
            raise ServiceError(status_code=404, detail='项目不存在')
        return {'project_id': project.project_id, 'name': project.name, 'charter': project.charter, 'roadmap': project.roadmap, 'decisions': project.decisions, 'updated_at': project.updated_at}

    async def update_project_blueprint(self, project_id: str, payload: dict) -> dict:
        controller = self.context.controller
        state = controller.state
        db = getattr(controller, 'db', None)
        self.require_secretary()
        charter = payload.get('charter', {})
        roadmap = payload.get('roadmap', [])
        decisions = payload.get('decisions', [])
        try:
            project = await run_in_threadpool(controller.project_manager.update_project_blueprint, project_id, charter, roadmap, decisions)
        except ValueError as exc:
            raise ServiceError(status_code=400, detail=str(exc))
        if not project:
            raise ServiceError(status_code=404, detail='项目不存在')
        print(f'[Station] 项目蓝图已更新: {project_id}')
        await self.context.broadcast(state, 'project_blueprint_updated', {'project_id': project.project_id, 'charter': project.charter, 'roadmap': project.roadmap, 'decisions': project.decisions, 'updated_at': project.updated_at})
        return {'project_id': project.project_id, 'name': project.name, 'charter': project.charter, 'roadmap': project.roadmap, 'decisions': project.decisions, 'updated_at': project.updated_at}

    async def archive_project(self, project_id: str) -> dict:
        controller = self.context.controller
        state = controller.state
        db = getattr(controller, 'db', None)
        self.require_secretary()
        project_manager = controller.project_manager
        if not await run_in_threadpool(project_manager.archive_project, project_id):
            raise ServiceError(status_code=404, detail='项目不存在')
        await self.context.broadcast(state, 'project_archived', {'project_id': project_id})
        return {'ok': True, 'project_id': project_id}

    async def get_usage(self, project_id: str, limit: int=100) -> dict:
        controller = self.context.controller
        state = controller.state
        db = getattr(controller, 'db', None)
        if not controller.secretary_active:
            return {'records': [], 'total': 0}
        records = db.get_usage_log(project_id, limit=limit)
        return {'records': records, 'total': len(records), 'project_id': project_id}
