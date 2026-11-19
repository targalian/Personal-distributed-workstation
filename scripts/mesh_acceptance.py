#!/usr/bin/env python3
"""Isolated deterministic durable-delivery protocol acceptance."""
from __future__ import annotations
import argparse
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import time
import uuid
from urllib.parse import urlsplit
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import requests
from lan_mesh.delivery_protocol import seal_payload, build_result
VERSION = json.loads((ROOT / 'VERSION.json').read_text()).get('version', 'unknown')
TEXT = '长结果边界✓' * 50000

def safe_endpoint(value: str) -> None:
    """Require an explicit isolated HTTP origin."""
    p = urlsplit(value)
    if (p.scheme != 'http' or not p.hostname or not p.port or p.path or p.query
            or p.fragment or p.username is not None or p.password is not None
            or p.port in {80,443,45454,45460,45470,8080,18765,18121}):
        raise ValueError('isolated HTTP origin with explicit port required')

def run_identity(value: str) -> str:
    if not re.fullmatch(r'[a-f0-9]{32}', value):
        raise ValueError('run ID must be 32 lowercase hexadecimal characters')
    return value

def token_env(name: str) -> str:
    token = os.environ.get(name, '')
    if len(token) < 32 or not token.isascii() or any(c.isspace() for c in token):
        raise ValueError('token environment must contain at least 32 ASCII nonspace characters')
    return token

def new_directory(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise ValueError('new absolute data directory required')
    path.mkdir(mode=0o700, exist_ok=False)
    (path / 'owner.lock').touch(exist_ok=False)
    return path

def envelope(run_id: str, reply_url: str, case: str = 'success') -> dict:
    run_identity(run_id); safe_endpoint(reply_url)
    if case not in {'success', 'cancel'}:
        raise ValueError('unknown case')
    return seal_payload(dict(pm_id=f'acceptance-{run_id}', parent_task_id=f'parent-{run_id}',
        subtask_id=f'subtask-{run_id}-{case}', attempt_id=f'attempt-{run_id}-{case}',
        name=f'mesh-acceptance-{case}', reporter_id=f'fixture-{case}', required_skill='acceptance',
        input_data={'requirement':'fixed acceptance probe'}, reply_url=reply_url, agent_spec={}))

class Transport:
    """Owner-local authenticated transport; never follows redirects."""
    def __init__(self, origins: list[str], run_id: str, token: str):
        for origin in origins: safe_endpoint(origin)
        self.origins = origins
        self.session = requests.Session(); self.session.trust_env = False
        self.headers = {'X-Lan-Mesh-Run':run_id, 'X-Lan-Mesh-Token':token}

    def request(self, origin: str, path: str, body: dict | None = None) -> dict:
        if origin not in self.origins or path not in {'/status','/pause','/resume',
                '/mesh-delivery/task','/mesh-delivery/cancel','/mesh-delivery/result',
                '/dispatch','/dispatch-cancel'}:
            raise ValueError('destination outside run allowlist')
        response = self.session.request('GET' if body is None else 'POST', origin + path,
            json=body, headers=self.headers, timeout=10, allow_redirects=False)
        if response.status_code != 200: raise ValueError('HTTP request rejected')
        return response.json()

def classification(statuses: list[dict], endpoints: list[str]) -> str:
    try:
        if any(ipaddress.ip_address(urlsplit(x).hostname).is_loopback for x in endpoints):
            return 'simulated'
    except ValueError:
        return 'unknown'
    ids = [s.get('identity') for s in statuses]
    return 'physical_protocol_test' if all(ids) and len(set(ids)) == 2 else 'unknown'

def wait_for(probe, predicate, timeout: float = 20) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = probe()
        if predicate(result): return result
        time.sleep(.1)
    raise RuntimeError('acceptance condition timed out')

def checks_run(t: Transport, args: argparse.Namespace, report: dict) -> None:
    def status(url):
        s = t.request(url, '/status')
        if s['run_id'] != args.run_id: raise ValueError('foreign status')
        return s
    statuses = [status(x) for x in (args.origin,args.peer)]
    report.update(classification=classification(statuses,[args.origin,args.peer]), identities=statuses)
    c = report['checks']
    t.request(args.peer, '/pause', {})
    task = envelope(args.run_id,args.origin)
    queued = t.request(args.origin,'/dispatch',{})
    dispatch = wait_for(lambda: status(args.origin),lambda s:s['outbox'].get('acked') == 1)
    c['durable_dispatch'] = queued.get('status') == 'queued' and dispatch['send_attempts'] >= 1
    a = t.request(args.peer,'/mesh-delivery/task',task)
    b = t.request(args.peer,'/mesh-delivery/task',task)
    c['duplicate_ack'] = all(x.get('ok') is True and x.get('ack_id') == 'task:'+task['attempt_id']
        and x.get('ack_sha256') == task['payload_sha256'] for x in (a,b))
    pending = wait_for(lambda: status(args.peer),lambda s:s['executions'].get('completed') == 1
        and s['outbox'].get('pending') == 1 and s['send_attempts'] >= 1)
    c['application_outage_simulation'] = pending['outbox'].get('pending') == 1 and status(args.origin)['applied'] == 0
    t.request(args.peer,'/resume',{})
    done = wait_for(lambda: status(args.origin),lambda s:s['applied'] == 1)
    remote = wait_for(lambda: status(args.peer),lambda s:s['outbox'].get('acked') == 1)
    c['persistent_retry'] = remote['retries'] >= 1
    c['once_and_sha'] = remote['calls'] == 1 and done['sha256'] == hashlib.sha256(TEXT.encode()).hexdigest()
    result = build_result(task, {'status':'completed','output':{'text':TEXT},
        'usage':{'input_tokens':0,'output_tokens':0}})
    ack = t.request(args.origin,'/mesh-delivery/result',result)
    c['duplicate_result'] = (ack.get('ack_id') == result['result_id']
        and ack.get('ack_sha256') == result['payload_sha256'] and ack.get('ok') is True)
    cancel = envelope(args.run_id,args.origin,'cancel')
    t.request(args.origin,'/dispatch-cancel',{})
    wait_for(lambda: status(args.origin),lambda s:s['outbox'].get('acked') == 2)
    t.request(args.peer,'/mesh-delivery/task',cancel)
    remote = wait_for(lambda: status(args.peer),lambda s:s['outbox'].get('acked') == 2)
    done = wait_for(lambda: status(args.origin),lambda s:s['inbox'].get('processed') == 2)
    c['cancel_before_task'] = remote['executions'].get('cancelled') == 1 and remote['calls'] == 1 and done['applied'] == 1
    report['final_evidence'] = {'remote':remote,'origin':done}

def execute(args: argparse.Namespace) -> int:
    safe_endpoint(args.origin); safe_endpoint(args.peer); run_identity(args.run_id)
    if args.origin == args.peer: raise ValueError('distinct endpoints required')
    token = token_env(args.token_env)
    directory = new_directory(args.data_dir)
    children = []
    t = Transport([args.origin,args.peer],args.run_id,token)
    report = dict(run_id=args.run_id,version=VERSION,checks={},fixture=True,token_usage=0)
    try:
        if args.self_test:
            for index, url in enumerate((args.origin,args.peer)):
                if urlsplit(url).hostname != '127.0.0.1': raise ValueError('self-test requires loopback')
                command = [sys.executable,str(ROOT/'scripts/mesh_acceptance_peer.py'),'--execute',
                    '--run-id',args.run_id,'--token-env',args.token_env,'--data-dir',str(directory/str(index)),
                    '--origin',args.origin,'--peer',args.peer,'--role','origin' if index == 0 else 'remote']
                children.append(subprocess.Popen(command,env=os.environ.copy(),stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL))
            for url in (args.origin,args.peer):
                def ready():
                    try: return t.request(url,'/status')
                    except (requests.RequestException,ValueError): return {}
                wait_for(ready,lambda s:s.get('run_id') == args.run_id)
        checks_run(t,args,report)
        report['ok'] = bool(report['checks']) and all(report['checks'].values())
    except Exception as exc:
        report.update(ok=False,error=type(exc).__name__)
    finally:
        for child in children:
            child.terminate()
        for child in children:
            try: child.wait(timeout=10)
            except subprocess.TimeoutExpired: child.kill(); child.wait(timeout=5)
        t.session.close()
        (directory/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print(json.dumps(report,ensure_ascii=False))
    return 0 if report['ok'] else 1

def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument('--execute',action='store_true'); p.add_argument('--self-test',action='store_true')
    p.add_argument('--run-id'); p.add_argument('--token-env',default='MESH_ACCEPTANCE_TOKEN')
    p.add_argument('--origin',default='http://127.0.0.1:19082'); p.add_argument('--peer',default='http://127.0.0.1:19081')
    p.add_argument('--data-dir')
    args = p.parse_args()
    if not args.execute:
        print(json.dumps({'mode':'plan','run_id':args.run_id or uuid.uuid4().hex,
            'prepare':'Set the same secret environment variable on runner and both peers; use --run-id, --token-env, --execute and distinct new absolute --data-dir paths. Peers need --origin, --peer and --role origin/remote. Self-test requires --execute --self-test.'}))
        return 0
    return execute(args)
if __name__ == '__main__':
    raise SystemExit(main())
