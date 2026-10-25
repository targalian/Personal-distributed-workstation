"""Dashboard source/markup compatibility and release version resolution."""

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from lan_mesh.dashboard_assets import load_dashboard_script
import lan_mesh.version as version

ROOT=Path(__file__).resolve().parents[1]


def test_dashboard_markup_and_business_sources_are_preserved():
    expected=json.loads((Path(__file__).parent/'fixtures/dashboard_source_contract.json').read_text(encoding='utf-8'))
    html=(ROOT/'lan_mesh/web/templates/dashboard.html').read_text(encoding='utf-8')
    tags='<script src="/static/mesh_transport.js"></script>\n<script src="/static/dashboard.js"></script>'
    assert tags in html
    assert hashlib.sha256(html.replace(tags,'').encode()).hexdigest()==expected['html_structure_sha256']
    script=load_dashboard_script()
    business=script[script.index('// ── Toast 通知'):]
    assert hashlib.sha256(business.encode()).hexdigest()==expected['domain_source_sha256']


def test_assembled_dashboard_is_valid_javascript():
    node=shutil.which('node')
    if not node:pytest.skip('Node.js is required for frontend build validation')
    result=subprocess.run([node,'--check'],input=load_dashboard_script(),text=True,encoding='utf-8',capture_output=True,timeout=10)
    assert result.returncode==0,result.stderr


def test_source_version_uses_manifest(tmp_path,monkeypatch):
    manifest=tmp_path/'VERSION.json';manifest.write_text('{"version":"1.2.3"}')
    monkeypatch.setattr(version,'VERSION_FILE',manifest)
    assert version.get_version()=='1.2.3'


def test_installed_version_uses_wheel_metadata(tmp_path,monkeypatch):
    monkeypatch.setattr(version,'VERSION_FILE',tmp_path/'missing.json')
    monkeypatch.setattr(version.metadata,'version',lambda name: '4.5.6' if name=='lan-mesh' else '')
    assert version.get_version()=='4.5.6'


def test_production_station_serves_bundle_before_static_mount(tmp_path):
    from types import SimpleNamespace
    from unittest.mock import Mock
    from fastapi.testclient import TestClient
    from lan_mesh.config import AppConfig
    from lan_mesh.database import Database
    from lan_mesh.shared_folder import SharedFolderManager
    from lan_mesh.station_controller import StationController
    ctl=StationController.__new__(StationController)
    ctl.cfg=AppConfig();ctl.db=Database(str(tmp_path/'assets.db'))
    ctl.state=SimpleNamespace(device_id='asset-station',device_name='Assets',role='station',
        api_port=0,ws_clients=set(),shared_folder=SharedFolderManager(str(tmp_path)),cloud_sync=None)
    ctl._mesh_auth_enabled=False;ctl._mesh_token='';ctl._threads=[];ctl._running=False
    ctl.discovery=None;ctl.secretary_active=False;ctl._local_pm_agent=None;ctl._local_sub_agents={}
    ctl.station_director=Mock();ctl.workstation_optimizer=Mock();ctl.shadow_dev_manager=Mock()
    ctl.bot_gateway=Mock();ctl.skill_registry=Mock();ctl.project_manager=None;ctl.model_router=None
    ctl.mcp_gateway=None;ctl.chat_handler=None;ctl.chat_runtime=None
    app=ctl._create_app()
    with TestClient(app) as client:
        assert app.version==version.__version__
        assert 'src="/static/dashboard.js"' in client.get('/').text
        response=client.get('/static/dashboard.js')
        assert response.status_code==200
        assert response.headers['content-type'].startswith('application/javascript')
        assert 'function apiFetch' in response.text and 'function renderTasks' in response.text
        assert client.get('/static/mesh_transport.js').status_code==200
    assert ctl.services.closed
