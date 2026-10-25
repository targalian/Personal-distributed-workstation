"""Interaction contracts, common ingress and real project domain persistence."""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lan_mesh.bot_gateway import BotGateway
from lan_mesh.conversation_service import ChatRequest, respond_to_chat
from lan_mesh.database import Database
from lan_mesh.project import ProjectManager, MODEL_PRICING
from lan_mesh.project_policy import MODEL_PRICING as POLICY_PRICING
from lan_mesh.service_runtime import ServiceError
from lan_mesh.station_routes_chat import build_chat_routes
from lan_mesh.station_routes_projects import build_project_routes


@pytest.fixture
def controller(tmp_path):
    ctl = SimpleNamespace(db=Database(str(tmp_path/'interaction.db')),
                          state=SimpleNamespace(ws_clients=set()), secretary_active=True)
    ctl.project_manager = ProjectManager(ctl.db)
    ctl.chat_handler = Mock(controller=ctl)
    ctl.chat_handler.chat.return_value = {'reply': 'shared reply'}
    yield ctl
    for owner in (ctl, ctl.project_manager):
        runtime = vars(owner).get('services')
        if runtime:
            runtime.close()


def make_app(ctl: object) -> FastAPI:
    app = FastAPI()
    app.include_router(build_chat_routes(ctl))
    app.include_router(build_project_routes(ctl))
    return app


def test_complete_interaction_openapi_contract(controller):
    expected = json.loads((Path(__file__).parent/'fixtures/interaction_api_contract.json').read_text(encoding='utf-8'))
    assert make_app(controller).openapi() == expected


def test_web_and_bot_share_native_conversation_ingress(controller):
    app = make_app(controller)
    with TestClient(app) as client:
        assert client.post('/api/secretary/chat', json={'message': 'web'}).json() == {'reply': 'shared reply'}
        assert client.post('/api/conversations/c1/messages', json={'message': 'thread', 'pm_thread_id': 'pm1'}).status_code == 200
        bot = BotGateway()
        bot.set_chat_handler(controller.chat_handler)
        assert bot._handle_natural_language('bot', 'chat') == 'shared reply'
    assert controller.chat_handler.chat.call_args_list[-1].args == ('bot',)
    topology = controller.services.describe()
    assert {'service': '/ConversationService', 'operations': ['/respond']} in topology
    assert controller.chat_handler.chat.call_args_list[1].kwargs == {'conv_id': 'c1', 'pm_thread_id': 'pm1'}


def test_shared_ingress_rejects_stale_secretary_and_invalid_message(controller):
    with pytest.raises(ServiceError) as error:
        respond_to_chat(controller, ChatRequest(123))
    assert error.value.status_code == 400
    controller.secretary_active = False
    with pytest.raises(ServiceError) as error:
        respond_to_chat(controller, ChatRequest('hello'))
    assert error.value.status_code == 503
    controller.chat_handler.chat.assert_not_called()


def test_project_budget_and_blueprint_share_one_repository(controller):
    manager = controller.project_manager
    project = manager.create_project('project', budget_limit_usd=1)
    assert manager.check_budget(project.project_id)
    manager.update_project_blueprint(project.project_id, {'goal': 'deliver'}, [{'name': 'build', 'status': 'in_progress'}], [])
    assert manager.get_project(project.project_id).charter == {'goal': 'deliver'}
    manager.record_usage(project.project_id, 'task', model='deepseek-chat', input_tokens=1000000)
    assert not manager.check_budget(project.project_id)
    assert MODEL_PRICING is POLICY_PRICING
    assert {item['service'] for item in manager.services.describe()} >= {
        '/ProjectLifecycleService', '/ProjectBudgetService', '/ProjectBlueprintService'}
