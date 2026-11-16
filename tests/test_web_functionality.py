"""Authenticated HTTP and WebSocket workflows using real routes and isolated SQLite."""

import hashlib
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from lan_mesh.config import AppConfig, UserAccount
from lan_mesh.database import Database
from lan_mesh.project import ProjectManager
from lan_mesh.protocol import HostRecord
from lan_mesh.shared_folder import SharedFolderManager
from lan_mesh.skill_registry import SkillRegistry
from lan_mesh.station_controller import StationController
from lan_mesh.station_director import StationDirector
from lan_mesh import station_routes_common as auth


def create_test_station(directory):
    """Assemble production routing with local state and no network/model producers."""
    ctl = StationController.__new__(StationController)
    ctl.cfg = AppConfig(); ctl.db = Database(str(directory / 'web.db'))
    ctl.cfg.security.users = [UserAccount(name=role, role=role, token=f'qa-{role}')
                              for role in ('boss', 'operator', 'viewer')]
    shared = SharedFolderManager(str(directory / 'shared'))
    ctl.state = SimpleNamespace(device_id='qa-station', device_name='QA Station', role='station',
        api_port=0, ws_clients=set(), shared_folder=shared, cloud_sync=None)
    ctl._mesh_auth_enabled = True; ctl._mesh_token = 'qa-mesh'
    ctl._threads = []; ctl._running = False; ctl._start_timestamp = time.time()
    ctl.discovery = Mock(); ctl.discovery.list_devices.return_value = []
    ctl.secretary_active = True; ctl.secretary_host_id = 'qa-station'; ctl.secretary_host_port = 0
    ctl._local_pm_agent = None; ctl._local_sub_agents = {}
    ctl.station_director = StationDirector(ctl.db, ctl.discovery, shared)
    ctl.workstation_optimizer = Mock(); ctl.shadow_dev_manager = Mock(); ctl.bot_gateway = Mock()
    ctl.project_manager = ProjectManager(ctl.db); ctl.model_router = None
    ctl.mcp_gateway = None; ctl.chat_handler = None; ctl.chat_runtime = None
    ctl.skill_registry = SkillRegistry(ctl.db, str(directory / 'skills'))
    ctl.skill_registry.scan_and_register()
    ctl.db.upsert_host(HostRecord(device_id='qa-station', device_name='QA Station',
        role='station', ip='127.0.0.1', online=True, api_port=0))
    ctl.activate_secretary = lambda: {'ok': True, 'already_active': True}
    return ctl


@pytest.fixture
def client(tmp_path, monkeypatch):
    ctl = create_test_station(tmp_path)
    monkeypatch.setattr(auth, '_mesh_auth_enabled', True)
    monkeypatch.setattr(auth, '_mesh_auth_token', 'qa-mesh')
    users = {name: {'name': name, 'role': name, 'token_hash': hashlib.sha256(
        f'qa-{name}'.encode()).hexdigest()} for name in ('boss', 'operator', 'viewer')}
    monkeypatch.setattr(auth, '_users', users)
    monkeypatch.setattr(auth, '_user_tokens', {entry['token_hash']: entry for entry in users.values()})
    with TestClient(ctl._create_app()) as client:
        yield client


def test_anonymous_bootstrap_does_not_grant_access_or_pretend_hosts_are_empty(client):
    bootstrap = client.get('/api/station/auth-token').json()
    assert bootstrap['role'] == '' and bootstrap['mesh_token'] == ''
    assert client.get('/api/hosts').status_code == 401
    assert client.get('/api/station/fleet').status_code == 401


@pytest.mark.parametrize('role', ['boss', 'operator', 'viewer'])
def test_personal_identities_see_real_hosts_and_secretary_status(client, role):
    headers = {'Authorization': f'Bearer qa-{role}'}
    bootstrap = client.get('/api/station/auth-token', headers=headers).json()
    assert bootstrap['role'] == role
    assert bool(bootstrap['mesh_token']) == (role == 'boss')
    hosts = client.get('/api/hosts', headers=headers)
    assert hosts.status_code == 200 and len(hosts.json()['hosts']) == 1
    assert client.get('/api/station/roles', headers=headers).json()['secretary'] == 'active'
    response = client.post('/api/station/activate-secretary', headers=headers)
    assert response.status_code == (200 if role == 'boss' else 403)


@pytest.mark.parametrize('protocols', [None, ['lan-mesh', 'token.revoked']])
def test_websocket_rejects_unauthenticated_connections_before_sending_hosts(client, protocols):
    with pytest.raises(WebSocketDisconnect) as error:
        with client.websocket_connect('/ws', subprotocols=protocols) as socket:
            socket.receive_json()
    assert error.value.code == 4401


@pytest.mark.parametrize('role', ['boss', 'operator', 'viewer'])
def test_websocket_authentication_uses_subprotocol_without_credentials_in_url(client, role):
    with client.websocket_connect('/ws', subprotocols=['lan-mesh', f'token.qa-{role}']) as socket:
        assert socket.accepted_subprotocol == 'lan-mesh'
        event = socket.receive_json()
        assert event['type'] == 'hosts' and event['data'][0]['device_id'] == 'qa-station'


def test_personal_tokens_use_the_authenticated_rate_limit_bucket(client, monkeypatch):
    seen = []
    monkeypatch.setattr(auth._rate_limiter, 'is_allowed',
                        lambda ip, trusted=False: seen.append(trusted) or True)
    assert client.get('/api/hosts', headers={'Authorization': 'Bearer qa-viewer'}).status_code == 200
    assert seen[-1] is True
    assert client.get('/api/hosts', headers={'Authorization': 'Bearer revoked'}).status_code == 403
    assert seen[-1] is False


def test_secretary_election_and_failover_cannot_initialize_concurrently():
    ctl = StationController.__new__(StationController)
    ctl.secretary_active = False
    ctl.state = SimpleNamespace(device_id='z')
    active = 0; maximum = 0
    guard = threading.Lock()
    def find_peer():
        nonlocal active, maximum
        with guard:
            active += 1; maximum = max(maximum, active)
        time.sleep(.04)
        with guard:
            active -= 1
        return SimpleNamespace(device_id='a', device_name='peer', ip='127.0.0.1', api_port=1)
    ctl._find_existing_secretary_host = find_peer
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: ctl.activate_secretary(), range(2)))
    assert all(result['conflict'] for result in results)
    assert maximum == 1


def test_secretary_activation_waits_for_deactivation_cleanup(monkeypatch):
    from lan_mesh import application_lifecycle
    entered, release, peer_entered, starting = (threading.Event() for _ in range(4))
    ctl = StationController.__new__(StationController)
    ctl.secretary_active = True; ctl.state = SimpleNamespace(device_id='z'); ctl.bot_gateway = None
    def close(owner):
        entered.set(); assert release.wait(3)
    def peer():
        peer_entered.set()
        return SimpleNamespace(device_id='a', device_name='peer', ip='127.0.0.1', api_port=1)
    def activate():
        starting.set(); return ctl.activate_secretary()
    monkeypatch.setattr(application_lifecycle, 'close_secretary_components', close)
    ctl._find_existing_secretary_host = peer
    with ThreadPoolExecutor(max_workers=2) as pool:
        stopped = pool.submit(ctl.deactivate_secretary)
        try:
            assert entered.wait(2)
            activated = pool.submit(activate)
            assert starting.wait(2)
            assert not peer_entered.wait(.1)
        finally:
            release.set()
        assert stopped.result()['ok']
        assert activated.result()['conflict']
