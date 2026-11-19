#!/usr/bin/env python3
"""Authenticated fixture peer using production execution, inbox and outbox pumps."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import secrets
import socket
import sys
from types import SimpleNamespace
from urllib.parse import urlsplit
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from scripts.mesh_acceptance import Transport, TEXT, envelope, new_directory, run_identity, token_env
from lan_mesh.database import Database
from lan_mesh.mesh_delivery_service import call_delivery, delivery_repository, bind_pm_delivery, DurableDeliveryService
from lan_mesh.service_runtime import get_service_runtime
from lan_mesh.pm_agent import ProjectManagerAgent
from lan_mesh.delivery_protocol import build_result
from lan_mesh.application_lifecycle import stop_signal, join_threads, close_component_runtime
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import uvicorn

def identity() -> tuple[str,str]:
    path = Path('/etc/machine-id')
    raw = path.read_text().strip() if path.exists() else ''
    source = '/etc/machine-id' if raw else 'hostname'
    return hashlib.sha256((raw or socket.gethostname()).encode()).hexdigest(), source

def validate(body: dict, kind: str, run_id: str, origin: str) -> None:
    for case in ('success','cancel'):
        task = envelope(run_id,origin,case)
        if kind == 'result':
            result = {'status':'completed','output':{'text':TEXT},'usage':{'input_tokens':0,'output_tokens':0}} if case == 'success' else {'status':'cancelled','output':{}}
            task = build_result(task,result)
        if body == task: return
    raise ValueError('only exact run fixture messages accepted')

class FixtureDatabase(Database):
    """Keep fixture data inside its exclusive directory, without global backups."""
    def backup(self, keep: int = 3) -> str:
        return ''


class Owner:
    def __init__(self, directory: Path, args: argparse.Namespace, token: str):
        self.db = FixtureDatabase(str(directory/'delivery.db'))
        self.state = SimpleNamespace(api_port=urlsplit(args.origin if args.role == 'origin' else args.peer).port,pm_agent=None,sub_agents={})
        self._threads = []; self.calls = 0; self.seen = []; self.paused = args.role == 'remote'
        self.transport = Transport([args.origin,args.peer],args.run_id,token)
        runtime = get_service_runtime(self)
        service = runtime.ensure('/DurableDeliveryService',lambda server:DurableDeliveryService(server,self),list(DurableDeliveryService.OPERATIONS))
        service._send = self.send
        if args.role == 'origin': self.bind(args)

    def bind(self, args: argparse.Namespace) -> None:
        task = envelope(args.run_id,args.origin)
        pm = ProjectManagerAgent(task['pm_id'],SimpleNamespace(),args.origin,'origin')
        bind_pm_delivery(self,pm)
        pm._state.task = {'task_id':task['parent_task_id']}
        pm._state.active_attempts[task['name']] = task
        pm._running = True
        def receive(report):
            self.seen.append(report)
            pm._state.subtask_outputs[report['task_name']] = report['output']
        pm._monitor = SimpleNamespace(receive_progress_report=receive)
        pm._persist_snapshot = lambda phase:pm.persist_delivery_state()
        self.state.pm_agent = pm
        pm.persist_delivery_state()

    def create_subagent(self, **spec: object) -> None:
        self.state.sub_agents[spec['preferred_agent_id']] = {'runtime':SimpleNamespace(execute=self.execute,cancel=lambda:None),'status':'idle'}

    def execute(self, payload: dict) -> dict:
        self.calls += 1
        return {'status':'completed','output':{'text':TEXT},'usage':{'input_tokens':0,'output_tokens':0}}

    def send(self, item: dict) -> None:
        repo = delivery_repository(self)
        try:
            if self.paused: raise ValueError('application_outage_simulation')
            origin = self.transport.origins[0 if item['kind'] == 'result' else 1]
            path = '/mesh-delivery/' + item['kind']
            if item['destination'] != origin + path: raise ValueError('destination rejected')
            payload = json.loads(item['payload_json'])
            ack = self.transport.request(origin,path,payload)
            if not (ack.get('ok') is True and ack.get('ack_id') == item['delivery_id'] and ack.get('ack_sha256') == payload['payload_sha256']):
                raise ValueError('invalid acknowledgement')
            repo.acknowledge(item['delivery_id'])
        except Exception:
            repo.fail(item['delivery_id'],'fixture transport pending',1)

    def close(self) -> None:
        stop_signal(self).set(); join_threads(self._threads,10)
        close_component_runtime(self); self.transport.session.close()

def authorized(headers: object, run_id: str, token: str) -> bool:
    return headers.get('X-Lan-Mesh-Run') == run_id and secrets.compare_digest(headers.get('X-Lan-Mesh-Token',''),token)

def build_app(owner: Owner, args: argparse.Namespace, token: str) -> FastAPI:
    app = FastAPI()
    @app.middleware('http')
    async def guard(request: Request, call_next):
        if not authorized(request.headers,args.run_id,token):
            return JSONResponse({'error':'unauthorized'},status_code=401)
        return await call_next(request)
    @app.get('/status')
    def status():
        digest, source = identity()
        rows = delivery_repository(owner).list_outbox()
        return dict(call_delivery(owner,'inspect'),run_id=args.run_id,identity=digest,identity_source=source,
            retries=sum(max(0, row['attempts'] - 1) for row in rows),
            send_attempts=sum(row['attempts'] for row in rows),
            calls=owner.calls,applied=len(owner.seen),sha256=hashlib.sha256(owner.seen[0]['output']['text'].encode()).hexdigest() if owner.seen else None)
    @app.post('/dispatch')
    def dispatch():
        if args.role != 'origin': return JSONResponse({},status_code=404)
        return call_delivery(owner,'queue_task',payload=envelope(args.run_id,args.origin),destination=args.peer)
    @app.post('/dispatch-cancel')
    def dispatch_cancel():
        if args.role != 'origin': return JSONResponse({},status_code=404)
        call_delivery(owner,'queue_cancel',payload=envelope(args.run_id,args.origin,'cancel'),destination=args.peer)
        return {'ok':True}
    @app.post('/{action}')
    def control(action: str):
        if action not in {'pause','resume'}: return JSONResponse({},status_code=404)
        owner.paused = action == 'pause'
        return {'ok':True}
    @app.post('/mesh-delivery/{kind}')
    async def delivery(kind: str, request: Request):
        if kind not in ({'result'} if args.role == 'origin' else {'task','cancel'}):
            return JSONResponse({},status_code=404)
        try:
            body = await request.json()
            validate(body,kind,args.run_id,args.origin)
            return call_delivery(owner,'accept_'+kind,payload=body)
        except (ValueError,TypeError): return JSONResponse({},status_code=400)
    return app

def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument('--execute',action='store_true')
    for name in ('run-id','token-env','data-dir','origin','peer','role'): p.add_argument('--'+name,required=True)
    args = p.parse_args()
    if not args.execute or args.role not in {'origin','remote'}: p.error('explicit execution and valid role required')
    run_identity(args.run_id); token = token_env(args.token_env)
    transport = Transport([args.origin,args.peer],args.run_id,token); transport.session.close()
    directory = new_directory(args.data_dir)
    owner = Owner(directory,args,token)
    try:
        call_delivery(owner,'start')
        url = urlsplit(args.origin if args.role == 'origin' else args.peer)
        uvicorn.run(build_app(owner,args,token),host=url.hostname,port=url.port,log_level='error',access_log=False)
    finally: owner.close()
    return 0
if __name__ == '__main__': raise SystemExit(main())
