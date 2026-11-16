"""Pure safety tests plus explicitly selected real subprocess acceptance."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import pytest
from scripts import mesh_acceptance as runner
from scripts.mesh_acceptance_peer import validate, authorized
RUN = 'a'*32
ORIGIN = 'http://127.0.0.1:19082'

@pytest.mark.parametrize('url',['http://localhost','http://localhost:45454','http://localhost:45460',
    'http://localhost:45470','http://localhost:8080','http://localhost:18765',
    'http://user@localhost:19081','http://localhost:19081?q=1','http://localhost:19081#x',
    'http://localhost:19081/path','https://localhost:19081'])
def test_reject_unsafe_origin(url):
    with pytest.raises(ValueError): runner.safe_endpoint(url)

@pytest.mark.parametrize('field,value',[('agent_spec',{'tools':['shell']}),('reply_url','http://evil:19089'),
    ('required_skill','shell'),('input_data',{'requirement':'arbitrary'}),('pm_id','foreign')])
def test_fixture_allowlist(field,value):
    body = runner.envelope(RUN,ORIGIN); body[field] = value
    with pytest.raises(ValueError): validate(body,'task',RUN,ORIGIN)

def test_valid_fixture():
    validate(runner.envelope(RUN,ORIGIN),'task',RUN,ORIGIN)

def test_wrong_run():
    with pytest.raises(ValueError): validate(runner.envelope(RUN,ORIGIN),'task','b'*32,ORIGIN)

def test_auth():
    assert not authorized({},RUN,'secret')
    assert not authorized({'X-Lan-Mesh-Run':'b'*32,'X-Lan-Mesh-Token':'secret'},RUN,'secret')
    assert authorized({'X-Lan-Mesh-Run':RUN,'X-Lan-Mesh-Token':'secret'},RUN,'secret')

def test_token_missing(monkeypatch):
    monkeypatch.delenv('FIXTURE_TOKEN',raising=False)
    with pytest.raises(ValueError): runner.token_env('FIXTURE_TOKEN')

def test_exclusive_directory(tmp_path):
    with pytest.raises(FileExistsError): runner.new_directory(str(tmp_path))

def test_relative_directory():
    with pytest.raises(ValueError): runner.new_directory('relative')

def test_transport_allowlist():
    t = runner.Transport([ORIGIN],RUN,'x'*32)
    try:
        with pytest.raises(ValueError): t.request('http://evil:19083','/status')
    finally: t.session.close()

def test_redirect_rejected(monkeypatch):
    t = runner.Transport([ORIGIN],RUN,'x'*32)
    monkeypatch.setattr(t.session,'request',lambda *a,**k:argparse.Namespace(status_code=302))
    with pytest.raises(ValueError): t.request(ORIGIN,'/status')
    t.session.close()

def test_loopback_not_physical():
    assert runner.classification([{'identity':'a'},{'identity':'b'}],[ORIGIN,ORIGIN]) == 'simulated'

def test_plan_no_writes_or_network(monkeypatch,capsys):
    monkeypatch.setattr(sys,'argv',['runner'])
    monkeypatch.setattr(Path,'mkdir',lambda *a,**k:pytest.fail('write'))
    monkeypatch.setattr(runner.requests.Session,'request',lambda *a,**k:pytest.fail('network'))
    assert runner.main() == 0
    assert json.loads(capsys.readouterr().out)['mode'] == 'plan'

def test_loopback_full_acceptance(tmp_path):
    import secrets
    import socket
    sockets = [socket.socket(), socket.socket()]
    try:
        for sock in sockets: sock.bind(('127.0.0.1', 0))
        origins = [f'http://127.0.0.1:{sock.getsockname()[1]}' for sock in sockets]
    finally:
        for sock in sockets: sock.close()
    env = os.environ.copy(); env['FIXTURE_TOKEN'] = secrets.token_hex(32)
    result = subprocess.run([sys.executable,str(runner.ROOT/'scripts/mesh_acceptance.py'),
        '--execute','--self-test','--run-id',RUN,'--token-env','FIXTURE_TOKEN',
        '--origin',origins[0],'--peer',origins[1],
        '--data-dir',str(tmp_path/'run')],env=env,capture_output=True,text=True,timeout=100)
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads((tmp_path/'run/report.json').read_text())
    assert report['ok'] and all(report['checks'].values())
    assert report['classification'] == 'simulated'
    assert env['FIXTURE_TOKEN'] not in result.stdout + result.stderr
