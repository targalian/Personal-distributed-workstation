"""AgentService: Yomk service for the Station task domain."""

import time


from .protocol import AgentCard
from .logger import get_logger
from .service_context import StationService, StationServiceContext
from .service_runtime import ServiceError
from ._vendor.yomkserverpy.YomkServerPy import YomkServer

logger = get_logger("task_service")


class AgentService(StationService):
    """Task domain operations, callable independently of the HTTP layer."""

    def __init__(self, server: YomkServer, context: StationServiceContext):
        super().__init__(server, context, "/AgentService")

    async def register_agent(self, payload: dict) -> dict:
        controller = self.context.controller
        db = controller.db
        state = controller.state
        discovery = getattr(controller, "discovery", None)
        self.require_secretary()
        card = AgentCard.from_dict(payload)
        card.last_seen = time.time()
        dev = discovery.find_device(card.device_id) if discovery else None
        if dev:
            card.ip = dev.get("ip", card.ip)
        db.upsert_agent(card)
        await self.context.broadcast(state, "agent_registered", card.to_dict())
        return {"ok": True, "agent_id": card.agent_id}

    async def list_agents(self, status: str = None) -> dict:
        controller = self.context.controller
        db = controller.db
        self.require_secretary()
        agents = db.list_agents(status=status)
        return {
            "agents": [a.to_dict() for a in agents],
            "total": len(agents),
            "idle": sum(1 for a in agents if a.status == "idle"),
            "busy": sum(1 for a in agents if a.status == "busy"),
        }

    async def get_agent(self, agent_id: str) -> dict:
        controller = self.context.controller
        db = controller.db
        self.require_secretary()
        agent = db.get_agent(agent_id)
        if not agent:
            raise ServiceError(status_code=404, detail="Agent 不存在")
        return agent.to_dict()
