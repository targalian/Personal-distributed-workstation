"""Resource contracts, secret-free metadata and scoped execution accounting."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI

from lan_mesh.agent_runtime import AgentRuntime
from lan_mesh.model_resources import ModelResource, ModelResourceManager, _get_usage_context, set_usage_context
from lan_mesh.station_routes_resources import build_resource_routes


def test_resource_api_contract_snapshot():
    ctl=SimpleNamespace(db=None,state=None)
    app=FastAPI();app.include_router(build_resource_routes(ctl))
    try:
        expected=json.loads((Path(__file__).parent/'fixtures/resource_api_contract.json').read_text(encoding='utf-8'))
        assert app.openapi()==expected
    finally:
        ctl.services.close()


@pytest.mark.parametrize('raises', [False,True])
def test_execution_restores_thread_accounting_context(tmp_path, raises):
    runtime=AgentRuntime('accounting',str(tmp_path))
    seen=[]
    def handler(data):
        seen.append(_get_usage_context())
        if raises:raise RuntimeError('failure')
        return {'result':'ok'}
    runtime._handlers['accounting']=handler
    set_usage_context('outer','outer-project')
    try:
        result=runtime.execute({'required_skill':'accounting','parent_task_id':'inner','project_id':'inner-project'})
        assert result['status']==('failed' if raises else 'completed')
        assert seen==[('inner','inner-project')]
        assert _get_usage_context()==('outer','outer-project')
        assert getattr(runtime._local,'current_skill','')==''
    finally:
        set_usage_context()
        runtime.services.close()


def test_resource_metadata_and_repr_never_contain_key():
    manager=ModelResourceManager()
    manager._resources={'pool':ModelResource('pool','test','token_plan',100,api_key='test-secret-value')}
    try:
        assert manager.list_resources()[0]['id']=='pool'
        assert 'test-secret-value' not in repr(manager._resources['pool'])
        assert 'test-secret-value' not in json.dumps(manager.list_resources())
        assert 'test-secret-value' not in json.dumps(manager.services.describe())
    finally:
        manager.services.close()


def test_explicit_runtime_resource_owner_is_isolated(tmp_path):
    class Ledger:
        def __init__(self):self.records=[]
        def record_usage(self, *args):self.records.append(args);return {'tracked':True}
    left,right=Ledger(),Ledger()
    first=AgentRuntime('first',str(tmp_path),resource_manager=left)
    second=AgentRuntime('second',str(tmp_path),resource_manager=right)
    first._record_usage('a',10,20)
    second._record_usage('b',30,40)
    assert left.records==[('a',10,20)]
    assert right.records==[('b',30,40)]


def test_resource_report_target_url_is_business_argument():
    manager=ModelResourceManager()
    try:
        assert manager.set_report_target('http://example.test',token='private-token') is False
        assert manager._secretary_url=='http://example.test'
        assert manager._ws_url.startswith('ws://example.test/ws/worker')
    finally:
        manager.services.close()
