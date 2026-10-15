"""Manual browser integration check with temporary storage and a simulated LLM.

Run from repository root: python test_bug/project_brief_browser.py
Requires dev dependencies + playwright, and a local Chrome/Chromium executable.
Set BRIEF_BROWSER_EXECUTABLE to override /usr/bin/google-chrome.
Screenshots are written to the gitignored temp_resault/ directory.
"""
import os
import json
import re
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import urlparse
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
from playwright.sync_api import sync_playwright
from fastapi import FastAPI
from fastapi.testclient import TestClient
from lan_mesh.chat_handler import ChatHandler
from lan_mesh.station_routes_briefs import build_brief_routes
from lan_mesh.station_routes_chat import build_chat_routes
from lan_mesh.station_routes_tasks import build_task_routes
from lan_mesh.station_routes_pm import build_pm_routes
from lan_mesh.database import Database
from lan_mesh.protocol import PMAgent, Task
from tests.test_project_brief import document

root=PROJECT_ROOT
folder=tempfile.TemporaryDirectory(prefix='brief-browser-')
controller=SimpleNamespace(model_router=None,_default_model='',secretary_active=True)
runtime=Mock()
runtime._call_llm_with_routing.return_value={'content':json.dumps({'reply':'已整理','project_brief':document()},ensure_ascii=False)}
chat=ChatHandler(runtime,controller,shared_folder=SimpleNamespace(path=folder.name))
controller.chat_handler=chat
controller.state=SimpleNamespace(ws_clients=set())
controller.db=Database(str(Path(folder.name)/'tasks.sqlite3'))
controller.discovery=Mock()
controller.bot_gateway=Mock()
chat._build_status_context=lambda: ''
chat._auto_title=Mock()
cid=chat._active_conv_id
other=chat.create_conversation('第二个项目')['id']
app=FastAPI();app.include_router(build_brief_routes(controller));app.include_router(build_chat_routes(controller));app.include_router(build_task_routes(controller));app.include_router(build_pm_routes(controller));client=TestClient(app)
html=(root/'lan_mesh/web/templates/dashboard.html').read_text()
html=re.sub(r'<script>.*?</script>','',html,flags=re.S)
html=html.replace('<script src="/static/project-brief.js"></script>', '''<script>
function apiFetch(url,opts){return fetch(url,opts)}
function showToast(msg){window.lastToast=msg}
document.querySelectorAll('.panel').forEach(p=>p.style.display=p.id==='panel-chat'?'block':'none');
</script><script src="/static/project-brief.js"></script>''')
errors=[]


def verify_execution_inspection(page, output):
    """Click the actual task UI against the inspection API and temporary startup DB."""
    from lan_mesh.execution_receipts import ExecutionReceipts
    from lan_mesh.pm_inspection import inspect_pm_start
    controller.state.device_id='browser-node'
    task=Task(task_id='inspection-task',name='执行结果待核实演示')
    controller.db.save_task(task)
    controller.db.bind_task_pm(PMAgent(pm_id='inspection-pm',task_id=task.task_id,
        device_id='browser-node',hostname='<img src=x onerror=alert(1)>原节点',ip='127.0.0.1',api_port=12345,status='starting'))
    controller.db.note_pm_start_issue('inspection-pm','节点掉线 <img src=x onerror=alert(1)>；请核实执行结果')
    path=Path(folder.name)/'browser-starts.sqlite3'
    store=ExecutionReceipts(path,owner='old-browser-process')
    store.claim({'subtask_id':'inspection-pm','pm_id':'inspection-pm','parent_task_id':task.task_id})
    store.finish('inspection-pm',{'status':'completed','output':{'ok':True}})
    live={}
    controller._local_inspect_pm_start=lambda pm_id,task_id: inspect_pm_start(path,pm_id,task_id,'browser-node',live)
    snapshot=controller.db.get_task(task.task_id).to_dict()
    page.evaluate('(task)=>{allTasks=[task];renderTasks();showTaskDetail(task.task_id)}',snapshot)
    assert '结果待核实' in page.locator('#task-grid').inner_text()
    assert '执行结果待核实' in page.locator('.task-execution-check').inner_text()
    page.locator('[data-execution-check]').click()
    page.wait_for_function("document.querySelector('.task-execution-result').textContent.includes('原节点有启动记录')")
    assert '不支持 PM 重启续跑' in page.locator('.task-execution-result').inner_text()
    assert page.locator('#modal img').count()==0
    page.screenshot(path=str(output/'task_inspection_desktop.png'),full_page=True)
    live.update(pm_id='inspection-pm',task_id=task.task_id,device_id='browser-node',running=True)
    page.locator('[data-execution-check]').click()
    page.wait_for_function("document.querySelector('.task-execution-result').textContent.includes('仍在运行')")
    live['running']=False
    page.locator('[data-execution-check]').click()
    page.wait_for_function("document.querySelector('.task-execution-result').textContent.includes('PM 已停止')")
    page.set_viewport_size({'width':390,'height':844})
    page.locator('[data-execution-check]').scroll_into_view_if_needed()
    assert page.locator('#modal').evaluate('(el)=>el.scrollWidth<=el.clientWidth+1')
    page.screenshot(path=str(output/'task_inspection_mobile.png'),full_page=True)
    # Server errors stay visible and permit another query.
    endpoint='**/api/tasks/inspection-task/execution-check'
    page.route(endpoint,lambda r:r.fulfill(status=503,json={'detail':'暂时无法连接'}),times=1)
    page.locator('[data-execution-check]').click()
    page.wait_for_function("document.querySelector('.task-execution-result').textContent.includes('核实失败')")
    assert page.locator('[data-execution-check]').is_enabled()
    # A slow result cannot modify a different task opened while it was in flight.
    held=[]
    page.route(endpoint,lambda r:held.append(r),times=1)
    page.locator('[data-execution-check]').click()
    page.wait_for_function("document.querySelector('[data-execution-check]').disabled")
    assert held
    other_task=Task(task_id='different-inspection-task',name='另一项任务',pm_agent_id='different-pm')
    page.evaluate('(task)=>{allTasks.push(task);showTaskDetail(task.task_id)}',other_task.to_dict())
    response=client.get('/api/tasks/inspection-task/execution-check')
    held.pop().fulfill(status=response.status_code,json=response.json())
    page.wait_for_load_state('networkidle')
    assert page.locator('.task-execution-result').inner_text()==''
    assert page.locator('[data-execution-check]').is_enabled()
    assert controller.db.get_task(task.task_id).to_dict()==snapshot
    page.evaluate('closeOverlayForce()')
    page.set_viewport_size({'width':1680,'height':1100})


def verify_task_control(page, output):
    """Verify accepted stop intent versus actual PM acknowledgment through the real API."""
    from lan_mesh.pm_agent import ProjectManagerAgent
    from lan_mesh.pm_control import control_pm, request_task_control
    page.on('dialog',lambda dialog:dialog.accept())
    holder=SimpleNamespace(pm=None)
    controller._queue_ws_broadcast=Mock()
    controller._pm_worker_map={}
    controller._local_control_pm=lambda payload:control_pm(lambda:holder.pm,'browser-node',payload)
    controller.cancel_task=lambda task_id:request_task_control(controller,task_id,'cancel')
    controller.pause_task=lambda task_id:request_task_control(controller,task_id,'pause')
    def task_list_route(r):
        response=client.get('/api/tasks')
        r.fulfill(status=response.status_code,json=response.json())
    page.route('**/api/tasks',task_list_route)
    def prepare(task_id,pm_id):
        task=Task(task_id=task_id,name='停止确认演示')
        controller.db.save_task(task)
        controller.db.bind_task_pm(PMAgent(pm_id=pm_id,task_id=task_id,device_id='browser-node',
            hostname='原节点',ip='127.0.0.1',api_port=12345,status='starting'))
        return controller.db.get_task(task_id)
    task=prepare('control-task','control-pm')
    page.evaluate('(task)=>{allTasks=[task];showTaskDetail(task.task_id)}',task.to_dict())
    page.locator('#modal button',has_text='取消任务').click()
    page.wait_for_function("allTasks.some(t=>t.task_id==='control-task'&&t.output_data._pm_control)")
    page.evaluate("showTaskDetail('control-task')")
    assert '取消已登记 · 停止待核实' in page.locator('#modal').inner_text()
    assert page.locator('[data-control-retry]').is_visible()
    assert page.locator('#task-grid .card',has_text='停止确认演示').get_by_text('重新提交').count()==0
    page.screenshot(path=str(output/'task_control_unknown_desktop.png'),full_page=True)
    page.set_viewport_size({'width':390,'height':844})
    page.locator('[data-control-retry]').scroll_into_view_if_needed()
    assert page.locator('#modal').evaluate('(el)=>el.scrollWidth<=el.clientWidth+1')
    page.screenshot(path=str(output/'task_control_unknown_mobile.png'),full_page=True)
    assert client.delete('/api/tasks/control-task').status_code==409
    pm=ProjectManagerAgent('control-pm',Mock(),'http://unused','browser-node')
    pm._state.task={'task_id':'control-task'}
    pm.running=True
    holder.pm=pm
    request_id=controller.db.get_task('control-task').output_data['_pm_control']['request_id']
    page.locator('[data-control-retry]').click()
    page.wait_for_function("document.querySelector('#modal').textContent.includes('已取消 · PM 已确认')")
    assert page.locator('[data-control-retry]').count()==0
    assert '在途模型和工具仍需核实' in page.locator('#modal').inner_text()
    assert not pm.running and pm._state.stop_event.is_set()
    assert controller.db.get_task('control-task').output_data['_pm_control']['request_id']==request_id
    page.set_viewport_size({'width':1680,'height':1100})
    page.screenshot(path=str(output/'task_control_confirmed_desktop.png'),full_page=True)
    # The pause button uses the same acknowledgment contract.
    task=prepare('pause-control-task','pause-control-pm')
    pm=ProjectManagerAgent('pause-control-pm',Mock(),'http://unused','browser-node')
    pm._state.task={'task_id':task.task_id}
    pm.running=True
    holder.pm=pm
    page.evaluate('(task)=>{allTasks.push(task);showTaskDetail(task.task_id)}',task.to_dict())
    page.locator('#modal button',has_text='暂停').click()
    page.wait_for_function("allTasks.some(t=>t.task_id==='pause-control-task'&&t.output_data._pm_control?.state==='confirmed')")
    page.evaluate("showTaskDetail('pause-control-task')")
    assert '已暂停 · PM 已确认' in page.locator('#modal').inner_text()
    assert pm._state.stop_reason=='paused'
    page.evaluate('closeOverlayForce()')
    page.unroute('**/api/tasks',task_list_route)


def verify_restart_recovery(page, output):
    """Keep legacy interrupted tasks protected and reject a stale resubmit button."""
    task=Task(task_id='recovery-task',name='重启待核实演示')
    controller.db.save_task(task)
    controller.db.bind_task_pm(PMAgent(pm_id='recovery-pm',task_id=task.task_id,device_id='remote',
        hostname='远端继续运行的节点',ip='127.0.0.1',api_port=12345,status='executing'))
    controller.db.update_task_status(task.task_id,'interrupted')
    legacy=controller.db.get_task(task.task_id).to_dict()
    page.evaluate('(task)=>{allTasks=[task];renderTasks();showTaskDetail(task.task_id)}',legacy)
    assert page.locator('#task-grid .card',has_text=task.name).get_by_text('重新提交').count()==0
    assert '原节点执行情况仍需核实' in page.locator('.task-execution-check').inner_text()
    assert client.delete('/api/tasks/recovery-task').status_code==409
    controller.db.mark_tasks_for_recovery()
    recovered=controller.db.get_task(task.task_id).to_dict()
    page.evaluate('(task)=>{allTasks=[task];renderTasks();showTaskDetail(task.task_id)}',recovered)
    assert '原节点可能仍在运行' in page.locator('.task-execution-check').inner_text()
    page.wait_for_function("document.querySelectorAll('#toast-container .toast').length===0")
    page.screenshot(path=str(output/'task_recovery_desktop.png'),full_page=True)
    page.set_viewport_size({'width':390,'height':844})
    assert page.locator('#modal').evaluate('(el)=>el.scrollWidth<=el.clientWidth+1')
    page.screenshot(path=str(output/'task_recovery_mobile.png'),full_page=True)
    # An old tab with cached failure must re-read the current task before cloning it.
    attempted=[]
    def forbid_submit(r):
        if r.request.method=='POST':attempted.append(r.request.post_data)
        r.fulfill(status=409,json={'detail':'unexpected submission'})
    page.route('**/api/tasks',forbid_submit)
    page.evaluate('(task)=>{allTasks=[{...task,status:"failed",output_data:{}}]}',recovered)
    page.evaluate("resubmitTask('recovery-task')")
    assert not attempted
    assert controller.db.get_task(task.task_id).pm_agent_id=='recovery-pm'
    page.unroute('**/api/tasks',forbid_submit)
    # Only an original-PM runtime callback clears the pending execution observation.
    response=client.post('/api/pm/recovery-pm/status',json={'status':'executing'})
    assert response.status_code==200
    current=controller.db.get_task(task.task_id).to_dict()
    assert '_pm_recovery' not in current['output_data']
    page.evaluate('(task)=>{allTasks=[task];showTaskDetail(task.task_id)}',current)
    assert '秘书重新激活' not in page.locator('.task-execution-check').inner_text()
    assert current['status']=='running'
    page.evaluate('closeOverlayForce()')
    page.set_viewport_size({'width':1680,'height':1100})


def verify_task_resubmission(page, output):
    """Use the real resubmission API/SQLite; simulate only PM execution."""
    controller.project_manager=None
    controller.state.device_name='browser-node'
    controller.state.api_port=12345
    controller.chat_runtime=runtime
    controller._local_pm_agent=None
    controller._pm_worker_map={}
    controller._queue_ws_broadcast=Mock()
    controller._auto_attach_pm_thread=Mock()
    started=[]
    def start(task_id, secretary_url, task_data, pm_id):
        started.append(task_id)
        return {'ok':True,'pm_id':pm_id,'task_id':task_id}
    controller._local_start_pm=start
    source=Task(task_id='resubmit-source',name='重新提交并保留已确认需求',status='failed',
        description='沿用已经确认的项目描述',input_data={'_brief':{'revision':7,'task_id':'resubmit-source'},
        '_priority':'high','files':['input.csv']})
    controller.db.save_task(source)
    def tasks_route(r):
        response=client.get('/api/tasks')
        r.fulfill(status=response.status_code,json=response.json())
    page.route('**/api/tasks',tasks_route)
    page.locator('.tabs [data-tab="workspace"]').click()
    page.locator('[data-wsview="tasks"]').click()
    page.wait_for_load_state('networkidle')
    page.evaluate('(task)=>{allTasks=[task];renderTasks()}',source.to_dict())
    page.locator('#task-grid .card',has_text=source.name).locator('.card-title').click()
    # Server completes the operation, but the browser never receives its response.
    endpoint='**/api/tasks/resubmit-source/resubmit'
    def lose_response(r):
        response=client.post('/api/tasks/resubmit-source/resubmit',json=json.loads(r.request.post_data))
        assert response.status_code==200
        r.abort('failed')
    page.route(endpoint,lose_response,times=1)
    page.locator('#task-grid').get_by_role('button',name='🔄 重新提交',exact=True).click()
    page.wait_for_function("!resubmittingTasks.has('resubmit-source')")
    assert len(started)==1
    # Repeated browser actions find the same persisted replacement.
    page.evaluate("Promise.all([resubmitTask('resubmit-source'),resubmitTask('resubmit-source')])")
    assert len(started)==1
    current=client.get('/api/tasks/resubmit-source').json()
    replacement_id=current['resubmission']['replacement_task_id']
    replacement=client.get('/api/tasks/'+replacement_id).json()
    assert replacement['input_data']==source.input_data
    assert replacement['resubmission']['source_task_id']==source.task_id
    assert page.locator('#task-grid .card',has_text=source.name).get_by_role('button',name='🔄 重新提交',exact=True).count()==0
    page.evaluate("showTaskDetail('resubmit-source')")
    assert replacement_id in page.locator('.task-provenance').inner_text()
    page.locator('[data-resubmit-existing]').click()
    page.wait_for_function("!resubmittingTasks.has('resubmit-source')")
    assert len(started)==1
    page.wait_for_function("document.querySelectorAll('#toast-container .toast').length===0")
    page.screenshot(path=str(output/'task_resubmission_desktop.png'),full_page=True)
    page.locator('[data-replacement-task]').click()
    page.wait_for_function("document.querySelector('.task-provenance').textContent.includes('来源任务')")
    assert source.task_id in page.locator('.task-provenance').inner_text()
    page.set_viewport_size({'width':390,'height':844})
    assert page.locator('#modal').evaluate('(el)=>el.scrollWidth<=el.clientWidth+1')
    page.screenshot(path=str(output/'task_resubmission_mobile.png'),full_page=True)
    page.evaluate('closeOverlayForce()')
    # The source changes after the browser GET. The server must reject the POST.
    raced=Task(task_id='resubmit-race',name='请求途中状态变化',status='failed')
    controller.db.save_task(raced)
    page.evaluate('(task)=>{allTasks=[task];renderTasks()}',raced.to_dict())
    rejected=[]
    def change_before_post(r):
        controller.db.update_task_status(raced.task_id,'running')
        response=client.post('/api/tasks/resubmit-race/resubmit',json=json.loads(r.request.post_data))
        rejected.append(response.status_code)
        r.fulfill(status=response.status_code,json=response.json())
    page.route('**/api/tasks/resubmit-race/resubmit',change_before_post,times=1)
    page.evaluate("resubmitTask('resubmit-race')")
    assert rejected==[409] and len(started)==1
    assert '重新提交失败' in page.locator('#toast-container').inner_text()
    page.unroute('**/api/tasks',tasks_route)
    page.set_viewport_size({'width':1680,'height':1100})


def verify_pm_checkpoint(page, output):
    """Show only durable recovery summaries through the actual inspection API."""
    from lan_mesh.execution_receipts import ExecutionReceipts
    from lan_mesh.pm_checkpoint import PMCheckpoints
    from lan_mesh.pm_inspection import inspect_pm_start
    from lan_mesh.pm_state import PMState
    task=Task(task_id='checkpoint-task',name='PM进程退出后的执行现场')
    controller.db.save_task(task)
    controller.db.bind_task_pm(PMAgent(pm_id='checkpoint-pm',task_id=task.task_id,
        device_id=controller.state.device_id,hostname='原执行节点',ip='127.0.0.1',api_port=12345))
    path=Path(folder.name)/'checkpoint-starts.sqlite3'
    ExecutionReceipts(path,owner='gone').claim({'subtask_id':'checkpoint-pm','pm_id':'checkpoint-pm','parent_task_id':task.task_id})
    st=PMState()
    st.task=task.to_dict()
    st.plan={'decomposition':[{'name':'A'},{'name':'B'},{'name':'C'}]}
    st.phase='monitoring'
    st.subtask_outputs={'A':{'private':'不应出现在查询响应的原始产物'}}
    st.execution_attempts={'B':{'phase':'active'}}
    PMCheckpoints(path,owner='gone').save('checkpoint-pm',task.task_id,controller.state.device_id,st.snapshot(),0)
    live={}
    controller._local_inspect_pm_start=lambda pm_id,task_id:inspect_pm_start(path,pm_id,task_id,controller.state.device_id,live)
    current=controller.db.get_task(task.task_id).to_dict()
    page.evaluate('(task)=>{allTasks=[task];showTaskDetail(task.task_id)}',current)
    page.locator('[data-execution-check]').click()
    page.wait_for_function("document.querySelector('.task-execution-result').textContent.includes('执行现场 v1')")
    text=page.locator('.task-execution-result').inner_text()
    assert '已完成 1/3' in text and '执行待核实 1' in text and '未派发 1' in text
    assert '不支持 PM 重启续跑' in text
    assert '原始产物' not in client.get('/api/tasks/checkpoint-task/execution-check').text
    page.wait_for_function("document.querySelectorAll('#toast-container .toast').length===0")
    page.screenshot(path=str(output/'pm_checkpoint_desktop.png'),full_page=True)
    page.set_viewport_size({'width':390,'height':844})
    assert page.locator('#modal').evaluate('(el)=>el.scrollWidth<=el.clientWidth+1')
    page.screenshot(path=str(output/'pm_checkpoint_mobile.png'),full_page=True)
    live.update(pm_id='checkpoint-pm',task_id=task.task_id,device_id=controller.state.device_id,
                running=False,checkpoint_error=True)
    page.locator('[data-execution-check]').click()
    page.wait_for_function("document.querySelector('.task-execution-result').textContent.includes('检查点写入失败')")
    # Querying an older node without a checkpoint remains useful, without claiming safety.
    controller._local_inspect_pm_start=lambda pm_id,task_id:inspect_pm_start(Path(folder.name)/'missing-checkpoint.sqlite3',pm_id,task_id,controller.state.device_id,{})
    page.locator('[data-execution-check]').click()
    page.wait_for_function("document.querySelector('.task-execution-result').textContent.includes('未找到执行现场记录')")
    assert controller.db.get_task(task.task_id).to_dict()==current
    page.evaluate('closeOverlayForce()')
    page.set_viewport_size({'width':1680,'height':1100})


def verify_recovery_assessment(page, output):
    """Query persisted result evidence through the real recovery API, without execution."""
    from lan_mesh.pm_checkpoint import PMCheckpoints
    from lan_mesh.pm_recovery import assess_pm_recovery
    from lan_mesh.pm_state import PMState
    task=Task(task_id='recovery-assessment-task',name='恢复前逐项核实原执行')
    controller.db.save_task(task)
    controller.db.bind_task_pm(PMAgent(pm_id='assessment-pm',task_id=task.task_id,
        device_id=controller.state.device_id,hostname='原节点',ip='127.0.0.1',api_port=12345))
    path=Path(folder.name)/'assessment.sqlite3'
    st=PMState();st.task=task.to_dict();st.phase='monitoring'
    name='B <img src=x onerror=alert(1)>'
    st.plan={'pattern':'orchestrator','decomposition':[{'name':'A'},{'name':name,'depends_on':['A']},{'name':'C','depends_on':[name]}]}
    st.subtask_outputs={'A':{'private':'内部输出不可出现在摘要'}}
    identity={'subtask_id':'original-execution-B','parent_task_id':task.task_id,'pm_id':'assessment-pm','reporter_id':'worker','name':name}
    st.execution_attempts={name:{**identity,'phase':'processing','station':{},
        'received_result':{**identity,'task_name':name,'status':'completed','output':{'summary':'原始产物'}}}}
    store=PMCheckpoints(path,owner='old-process')
    store.save('assessment-pm',task.task_id,controller.state.device_id,st.snapshot(),0)
    controller._local_assess_pm_recovery=lambda pm_id,task_id,**kwargs:assess_pm_recovery(path,pm_id,task_id,controller.state.device_id,lambda:{},**kwargs)
    current=controller.db.get_task(task.task_id).to_dict();before=path.read_bytes()
    page.evaluate('(task)=>{allTasks=[task];showTaskDetail(task.task_id)}',current)
    page.locator('[data-recovery-check]').click()
    page.wait_for_function("document.querySelector('.task-recovery-result').textContent.includes('执行证据已收集')")
    result=page.locator('.task-recovery-result').inner_text()
    assert '尚需 PM 质量检查' in result and '检查点未登记执行请求' in result
    assert '原始产物' not in result and '内部输出' not in result
    assert page.locator('#modal img').count()==0 and path.read_bytes()==before
    page.wait_for_function("document.querySelectorAll('#toast-container .toast').length===0")
    page.screenshot(path=str(output/'pm_recovery_assessment_desktop.png'),full_page=True)
    page.set_viewport_size({'width':390,'height':844})
    assert page.locator('#modal').evaluate('(el)=>el.scrollWidth<=el.clientWidth+1')
    page.screenshot(path=str(output/'pm_recovery_assessment_mobile.png'),full_page=True)
    endpoint='**/api/tasks/recovery-assessment-task/recovery-check'
    page.route(endpoint,lambda r:r.fulfill(status=503,json={'detail':'暂时无法查询原节点'}),times=1)
    page.locator('[data-recovery-check]').click()
    page.wait_for_function("document.querySelector('.task-recovery-result').textContent.includes('恢复核实失败')")
    assert page.locator('[data-recovery-check]').is_enabled()
    # Continue a budget-limited page using the same checkpoint revision.
    st.execution_attempts[name].pop('received_result')
    st.execution_attempts[name]['station']={'ip':'127.0.0.1','api_port':12345,'device_id':'executor'}
    store.save('assessment-pm',task.task_id,controller.state.device_id,st.snapshot(),1)
    def paged_assess(pm_id,task_id,**kwargs):
        return assess_pm_recovery(path,pm_id,task_id,controller.state.device_id,lambda:{},budget=0,**kwargs)
    controller._local_assess_pm_recovery=paged_assess
    page.locator('[data-recovery-check]').click()
    page.wait_for_function("!document.querySelector('[data-recovery-next]').hidden")
    # The next page is still at the same revision; query only changes the read budget.
    import lan_mesh.pm_recovery as recovery_module
    original_get=recovery_module.http_get
    recovery_module.http_get=lambda url,**kwargs:Mock(status_code=200,json=lambda:{'device_id':'executor'} if url.endswith('/info') else {**identity,'status':'completed','output':{'summary':'private'}})
    controller._local_assess_pm_recovery=lambda pm_id,task_id,**kwargs:assess_pm_recovery(path,pm_id,task_id,controller.state.device_id,lambda:{},**kwargs)
    try:
        page.locator('[data-recovery-next]').click()
        page.wait_for_function("document.querySelector('.task-recovery-result').textContent.includes('original-execution-B')")
        assert 'A：成功输出已保存' in page.locator('.task-recovery-result').inner_text()
        assert 'C：检查点未登记执行请求' in page.locator('.task-recovery-result').inner_text()
        assert page.locator('[data-recovery-next]').is_hidden()
    finally:
        recovery_module.http_get=original_get
    before=path.read_bytes()
    # A delayed answer must not replace a different task's result panel.
    controller._local_assess_pm_recovery=paged_assess
    held=[]
    page.route(endpoint,lambda r:held.append(r),times=1)
    page.locator('[data-recovery-check]').click()
    page.wait_for_function("document.querySelector('[data-recovery-check]').disabled")
    other=Task(task_id='assessment-other',name='另一个任务',pm_agent_id='another-pm')
    page.evaluate('(task)=>{allTasks.push(task);showTaskDetail(task.task_id)}',other.to_dict())
    response=client.get('/api/tasks/recovery-assessment-task/recovery-check')
    held.pop().fulfill(status=response.status_code,json=response.json())
    page.wait_for_load_state('networkidle')
    assert page.locator('.task-recovery-result').inner_text()==''
    assert path.read_bytes()==before and controller.db.get_task(task.task_id).to_dict()==current
    page.evaluate('closeOverlayForce()');page.set_viewport_size({'width':1680,'height':1100})


def verify_pm_resume(page, output):
    """Authorize and resume a saved dependency chain through actual task APIs."""
    import threading
    import lan_mesh.pm_agent as pm_module
    from lan_mesh.pm_agent import ProjectManagerAgent
    from lan_mesh.pm_checkpoint import PMCheckpoints
    from lan_mesh.pm_lease import lease_identity
    from lan_mesh.pm_recovery import assess_pm_recovery
    from lan_mesh.pm_resume import resume_pm_once
    from lan_mesh.pm_state import PMState
    from lan_mesh.task_resume import resume_authorization
    task=Task(task_id='resume-browser-task',name='恢复原任务：继续未完成步骤')
    controller.db.save_task(task)
    controller.db.bind_task_pm(PMAgent(pm_id='resume-browser-pm',task_id=task.task_id,
        device_id=controller.state.device_id,hostname='原节点',ip='127.0.0.1',api_port=12345))
    task=controller.db.get_task(task.task_id)
    path=Path(folder.name)/'resume-browser.sqlite3'
    st=PMState();st.task=task.to_dict();st.phase='monitoring'
    st.plan={'pattern':'orchestrator','decomposition':[{'name':'A'},{'name':'B','depends_on':['A']}]}
    st.subtask_outputs={'A':{'summary':'原有输出 A'}}
    PMCheckpoints(path,owner='exited-browser-process',lease=lease_identity(path)).save(
        'resume-browser-pm',task.task_id,controller.state.device_id,{**st.snapshot(),'secretary_url':'http://browser-secretary'},0)
    active=[None];entered=threading.Event();release=threading.Event();executed=[]
    runtime=Mock()
    def execute(payload):
        executed.append(payload['name']);entered.set()
        assert release.wait(15)
        return {'status':'completed','output':{'summary':'B 续跑完成'}}
    runtime.execute.side_effect=execute
    runtime._call_llm_with_routing.return_value={'content':'恢复后的完整交付物'}
    old_init=ProjectManagerAgent.__init__;old_post=pm_module.http_post
    def init(self,*args,**kwargs):
        old_init(self,*args,**kwargs)
        self._planner.load_skill=Mock()
        self._dispatcher.get_available_stations=lambda:[]
        self._distribute_artifacts=Mock();self._record_task_memory=Mock();self._monitor.poll_interval=0.01
    ProjectManagerAgent.__init__=init
    pm_module.http_post=lambda url,**kwargs:client.post('/api/'+url.split('/api/',1)[1],json=kwargs.get('json',{}))
    controller._local_assess_pm_recovery=lambda pm_id,task_id,**kwargs:assess_pm_recovery(
        path,pm_id,task_id,controller.state.device_id,lambda:active[0].get_status() if active[0] else {},**kwargs)
    controller._local_resume_pm=lambda payload:resume_pm_once(payload=payload,receipt_path=path,runtime=runtime,
        device_id=controller.state.device_id,device_name='原节点',current=lambda:active[0],
        assign=lambda pm:active.__setitem__(0,pm),authorization=lambda tid,rid:resume_authorization(controller,tid,rid))
    try:
        page.evaluate('(task)=>{allTasks=[task];showTaskDetail(task.task_id)}',task.to_dict())
        page.locator('[data-recovery-check]').click()
        page.wait_for_function("!document.querySelector('[data-resume-task]').hidden")
        assert '可申请恢复' in page.locator('.task-recovery-result').inner_text()
        endpoint='**/api/tasks/resume-browser-task/resume'
        page.route(endpoint,lambda r:r.fulfill(status=503,json={'detail':'原节点连接中断'}),times=1)
        page.locator('[data-resume-task]').click()
        page.wait_for_function("document.querySelector('.task-recovery-result').textContent.includes('恢复未获确认')")
        assert page.locator('[data-resume-task]').is_enabled() and not executed
        held=[]
        page.route(endpoint,lambda r:held.append(r),times=1)
        page.locator('[data-resume-task]').click()
        page.wait_for_function("document.querySelector('[data-resume-task]').disabled")
        assert page.locator('[data-recovery-check]').is_disabled()
        route=held.pop();payload=route.request.post_data_json
        assert payload=={'expected_revision':1}
        response=client.post('/api/tasks/resume-browser-task/resume',json=payload)
        assert response.status_code==200 and response.json()['ok']
        assert entered.wait(3) and executed==['B']
        route.fulfill(status=response.status_code,json=response.json())
        page.wait_for_function("document.querySelector('.task-recovery-result').textContent.includes('已接管原执行现场')")
        assert page.locator('[data-resume-task]').is_hidden()
        repeat=client.post('/api/tasks/resume-browser-task/resume',json=payload).json()
        assert repeat['ok'] and repeat['duplicate'] and executed==['B']
        page.screenshot(path=str(output/'pm_resume_desktop.png'),full_page=True)
        page.set_viewport_size({'width':390,'height':844})
        assert page.locator('#modal').evaluate('(el)=>el.scrollWidth<=el.clientWidth+1')
        page.screenshot(path=str(output/'pm_resume_mobile.png'),full_page=True)
        release.set()
        active[0]._thread.join(timeout=4);active[0]._progress_thread.join(timeout=4)
        assert not active[0].running and controller.db.get_task(task.task_id).status=='completed'
        assert active[0]._state.start_time==st.start_time and active[0]._state.subtask_outputs['A']==st.subtask_outputs['A']
        # A late resume response cannot replace another task's panel.
        page.evaluate('(task)=>{allTasks=[task];showTaskDetail(task.task_id)}',task.to_dict())
        page.route(endpoint,lambda r:held.append(r),times=1)
        page.evaluate("()=>{const p=document.querySelector('.task-execution-check');p.querySelector('[data-resume-task]').hidden=false;resumeOriginalTask('resume-browser-task',p,1)}")
        page.wait_for_function("document.querySelector('[data-resume-task]').disabled")
        other=Task(task_id='resume-other-task',name='另一个任务',pm_agent_id='other')
        page.evaluate('(task)=>{allTasks.push(task);showTaskDetail(task.task_id)}',other.to_dict())
        held.pop().fulfill(status=200,json=repeat)
        page.wait_for_load_state('networkidle')
        assert page.locator('.task-recovery-result').inner_text()==''
    finally:
        release.set()
        if active[0]:
            active[0].stop()
            for thread in (active[0]._thread,active[0]._progress_thread):
                if thread:thread.join(timeout=4)
        ProjectManagerAgent.__init__=old_init;pm_module.http_post=old_post
    page.evaluate('closeOverlayForce()');page.set_viewport_size({'width':1680,'height':1100})


def verify_pm_clarification(page, output):
    """Recover the same question, lose a saved reply ACK and finish via the actual UI/API."""
    import time
    import lan_mesh.pm_agent as pm_module
    from lan_mesh.pm_agent import ProjectManagerAgent
    from lan_mesh.pm_checkpoint import PMCheckpoints
    from lan_mesh.pm_clarification import inspect_question, route_answer
    from lan_mesh.pm_lease import lease_identity
    from lan_mesh.pm_planner import PMPlanner
    from lan_mesh.pm_recovery import assess_pm_recovery
    from lan_mesh.pm_resume import resume_pm_once
    from lan_mesh.pm_state import PMState
    from lan_mesh.task_resume import resume_authorization
    task = Task(task_id='question-browser-task', name='重启后继续需求澄清', description='做工具')
    pm_id = 'question-browser-pm'
    controller.db.save_task(task)
    controller.db.bind_task_pm(PMAgent(pm_id=pm_id, task_id=task.task_id,
        device_id=controller.state.device_id, hostname='原节点', ip='127.0.0.1', api_port=12345))
    task = controller.db.get_task(task.task_id)
    path = Path(folder.name)/'question-browser.sqlite3'
    st = PMState();st.task=task.to_dict();st.phase='awaiting_input'
    st.refinement={'format':1,'task':task.to_dict(),'description':'做工具','round':0,'context':[],'complete':False}
    question = PMPlanner._generate_refinement_question('做工具',[],0)
    st.clarification_request={'format':1,'question_id':'browser-original-question','question':question,'options':[],
        'asked_at':time.time()-10,'timeout':120,'deadline':time.time()+120,
        'continuation':{'kind':'refinement','round':0},'consumed':False}
    PMCheckpoints(path,owner='exited-question-process',lease=lease_identity(path)).save(
        pm_id,task.task_id,controller.state.device_id,{**st.snapshot(),'secretary_url':'http://browser-secretary'},0)
    active=[None]
    runtime=Mock()
    runtime.execute.return_value={'status':'completed','output':{'summary':'两次回答后的产物'}}
    runtime._call_llm_with_routing.return_value={'content':'澄清后完整交付'}
    original_init=ProjectManagerAgent.__init__;original_post=pm_module.http_post
    def init(self,*args,**kwargs):
        original_init(self,*args,**kwargs)
        self._planner.load_skill=Mock()
        self._planner.analyze_with_skill=lambda task:{'pattern':'single','decomposition':[{'name':'工具'}]}
        self._dispatcher.get_available_stations=lambda:[]
        self._distribute_artifacts=Mock();self._record_task_memory=Mock();self._monitor.poll_interval=.01
    ProjectManagerAgent.__init__=init
    pm_module.http_post=lambda url,**kwargs:client.post('/api/'+url.split('/api/',1)[1],json=kwargs.get('json',{}))
    controller._local_assess_pm_recovery=lambda pid,tid,**kwargs:assess_pm_recovery(
        path,pid,tid,controller.state.device_id,lambda:active[0].get_status() if active[0] else {},**kwargs)
    controller._local_resume_pm=lambda payload:resume_pm_once(payload=payload,receipt_path=path,runtime=runtime,
        device_id=controller.state.device_id,device_name='原节点',current=lambda:active[0],
        assign=lambda pm:active.__setitem__(0,pm),authorization=lambda tid,rid:resume_authorization(controller,tid,rid))
    controller._local_pm_question=lambda pid,tid:inspect_question(path,pid,tid,controller.state.device_id,lambda:active[0])
    controller._local_inject_input=lambda payload:active[0].receive_input(payload) if active[0] else {'ok':False,'message':'请先恢复原任务'}
    controller.inject_input_to_pm=lambda pid,payload:route_answer(controller,pid,payload)
    endpoint='**/api/pm/'+pm_id+'/inject-input'
    def current():
        response=client.get('/api/pm/'+pm_id+'/clarification')
        assert response.status_code==200,response.text
        return response.json()
    def await_second():
        end=time.monotonic()+4
        while time.monotonic()<end:
            q=current()
            if q['question_id']!='browser-original-question' and q['can_reply']:return q
            time.sleep(.01)
        raise AssertionError('Second question did not become ready')
    try:
        page.evaluate('(task)=>{allTasks=[task];showTaskDetail(task.task_id)}',task.to_dict())
        page.locator('[data-question-check]').click()
        page.wait_for_function("document.querySelector('.task-question-result').textContent.includes('先核实恢复条件')")
        assert page.locator('[data-question-send]').is_hidden() and active[0] is None
        page.locator('[data-recovery-check]').click()
        page.wait_for_function("!document.querySelector('[data-resume-task]').hidden")
        page.locator('[data-resume-task]').click()
        page.wait_for_function("document.querySelector('.task-recovery-result').textContent.includes('已接管原执行现场')")
        page.locator('[data-question-check]').click()
        page.wait_for_function("!document.querySelector('[data-question-send]').hidden")
        assert current()['deadline']==st.clarification_request['deadline']
        reply=page.locator('[data-question-answer]')
        reply.fill('使用 Python')
        page.route(endpoint,lambda r:r.fulfill(status=503,json={'detail':'节点暂时断开'}),times=1)
        page.locator('[data-question-send]').click()
        page.wait_for_function("document.querySelector('.task-question-result').textContent.includes('回复未获确认')")
        assert reply.input_value()=='使用 Python' and not active[0]._state.clarification_response
        saved=[]
        def lose_ack(r):
            response=client.post('/api/pm/'+pm_id+'/inject-input',json=r.request.post_data_json)
            assert response.json()['ok'],response.text
            saved.append(response.json())
            r.fulfill(status=503,json={'detail':'已保存但确认丢失'})
        page.route(endpoint,lose_ack,times=1)
        page.locator('[data-question-send]').click()
        page.wait_for_function("document.querySelector('.task-question-result').textContent.includes('已保存但确认丢失')")
        second=await_second()
        assert reply.input_value()=='使用 Python' and saved
        assert reply.get_attribute('data-question-id')=='browser-original-question'
        # Retry the same old reply, not the new question now waiting on the PM.
        page.locator('[data-question-send]').click()
        page.wait_for_function("document.querySelector('.task-question-result').textContent.includes('未重复应用')")
        assert reply.input_value()=='' and not active[0]._state.clarification_response
        reply.fill('尚未发送的旧草稿')
        page.locator('[data-question-check]').click()
        page.wait_for_function("document.querySelector('.task-question-result').textContent.includes('问题已变化')")
        assert reply.get_attribute('data-question-id')=='browser-original-question'
        assert reply.input_value()=='尚未发送的旧草稿'
        reply.fill('')
        page.locator('[data-question-check]').click()
        page.wait_for_function("!document.querySelector('[data-question-send]').hidden")
        assert reply.get_attribute('data-question-id')==second['question_id']
        page.screenshot(path=str(output/'pm_clarification_desktop.png'),full_page=True)
        page.set_viewport_size({'width':390,'height':844})
        assert page.locator('#modal').evaluate('(el)=>el.scrollWidth<=el.clientWidth+1')
        page.screenshot(path=str(output/'pm_clarification_mobile.png'),full_page=True)
        # Thread draft stays attached to its captured question when a later WS event arrives.
        page.evaluate('closeOverlayForce()');page.set_viewport_size({'width':1680,'height':1100})
        conv_id=chat.create_conversation('澄清线程验收')['id']
        chat.attach_pm_thread(conv_id,pm_id,task_name=task.name,agent_name='澄清 PM')
        chat.notify_pm_clarification(pm_id,second['question'],question_id=second['question_id'])
        page.locator('.tabs [data-tab="chat"]').click()
        page.wait_for_load_state('networkidle')
        page.evaluate('async args=>{_activeConvId=args.cid;_pmThreads=[{pm_id:args.pid,task_name:"澄清任务",status:"awaiting_input"}];_openThreadId=args.pid;await openPMThread(args.pid)}',{'cid':conv_id,'pid':pm_id})
        thread=page.locator('#thread-input-'+pm_id)
        thread.fill('需要测试')
        page.screenshot(path=str(output/'pm_clarification_thread_desktop.png'),full_page=True)
        page.set_viewport_size({'width':390,'height':844})
        thread.scroll_into_view_if_needed()
        assert page.locator('#thread-panel-'+pm_id).evaluate('(el)=>el.scrollWidth<=el.clientWidth+1')
        assert page.locator('.thread-input-bar').evaluate('(el)=>el.scrollWidth<=el.clientWidth+1')
        page.screenshot(path=str(output/'pm_clarification_thread_mobile.png'),full_page=True)
        page.set_viewport_size({'width':1680,'height':1100})
        page.evaluate('args=>onPMThreadStatusChange({pm_id:args.pid,status:"awaiting_input",clarification_id:"late-other-question",clarification_question:"迟到的其他问题",clarification_options:["别的选项"]})',{'pid':pm_id})
        assert thread.input_value()=='需要测试' and thread.get_attribute('data-question-id')==second['question_id']
        thread_endpoint='**/api/pm-threads/'+pm_id+'/messages'
        page.route(thread_endpoint,lambda r:r.fulfill(status=503,json={'detail':'线程网络失败'}),times=1)
        thread.press('Enter')
        page.wait_for_function("document.querySelector('#thread-msgs-question-browser-pm').textContent.includes('线程网络失败')")
        assert thread.input_value()=='需要测试' and not active[0]._state.clarification_response
        thread.press('Enter')
        page.wait_for_function("document.querySelector('#thread-input-question-browser-pm').value===''")
        active[0]._thread.join(timeout=4);active[0]._progress_thread.join(timeout=4)
        assert not active[0].running and controller.db.get_task(task.task_id).status=='completed'
        assert runtime.execute.call_count==1
        desc=active[0]._state.task['description']
        assert desc.count('A1: 使用 Python')==1 and desc.count('A2: 需要测试')==1
        # Late query results cannot populate another task's detail panel.
        page.evaluate('(task)=>{allTasks=[task];showTaskDetail(task.task_id)}',task.to_dict())
        held=[]
        page.route('**/api/pm/'+pm_id+'/clarification',lambda r:held.append(r),times=1)
        page.locator('[data-question-check]').click()
        page.wait_for_function("document.querySelector('[data-question-check]').disabled")
        other=Task(task_id='question-other',name='另一个任务',pm_agent_id='other')
        page.evaluate('(task)=>{allTasks.push(task);showTaskDetail(task.task_id)}',other.to_dict())
        held.pop().fulfill(status=200,json=current())
        page.wait_for_load_state('networkidle')
        assert page.locator('.task-question-result').inner_text()==''
    finally:
        if active[0]:
            active[0].stop()
            for thread in (active[0]._thread,active[0]._progress_thread):
                if thread:thread.join(timeout=4)
        ProjectManagerAgent.__init__=original_init;pm_module.http_post=original_post
    page.evaluate('closeOverlayForce();closePMThread()');page.set_viewport_size({'width':1680,'height':1100})


def accept_all_suggestions(page):
    """Review and explicitly accept a candidate without relying on automatic writes."""
    page.locator('#project-brief-panel [data-tab="review"]').click()
    page.wait_for_selector('[data-action="review-all"]')
    page.locator('[data-action="review-all"]').click()
    page.locator('[data-action="review-apply"]').click()


def verify_handoff(page, handoff_id, other_id, output):
    """Use real brief/task APIs and SQLite, with task execution replaced by a fixture."""
    def submit(**kw):
        task=Task(task_id=kw['task_id'], name=kw['name'], description=kw['description'],
                  input_data=kw['input_data'], status='running', pm_agent_id='pm-browser')
        controller.db.save_task(task)
        return task.to_dict()
    controller.submit_task_from_chat=Mock(side_effect=submit)
    chat.briefs.save(handoff_id, document(), 0)
    chat.briefs.propose(handoff_id, {**document(), 'title':'未采纳建议'}, 1)
    page.reload()
    page.evaluate('(id)=>ProjectBriefUI.open(id)',handoff_id)
    page.locator('[data-action="dispatch"]').click()
    page.wait_for_selector('[data-action="handoff-view"]')
    record=chat.brief_handoffs.get(handoff_id,1)
    task_id=record['task_id']
    assert record['title']=='协作工作台'
    assert controller.db.get_task(task_id).input_data['_brief']['revision']==1
    page.locator('[data-action="dispatch"]').click()
    page.wait_for_function("document.querySelector('#project-brief-panel').getAttribute('aria-busy')==='false'")
    assert controller.submit_task_from_chat.call_count==1
    page.locator('[data-action="handoff-view"]').click()
    page.wait_for_selector('.brief-content pre')
    assert '协作工作台' in page.locator('.brief-content pre').inner_text()
    assert '未采纳建议' not in page.locator('.brief-content pre').inner_text()
    page.screenshot(path=str(output/'project_brief_handoffs_desktop.png'),full_page=True)
    # Real delivery/accept/reject routes persist results in SQLite.
    delivery={'task_id':task_id,'task_name':'协作工作台','deliverable':'<img src=x onerror=alert(1)>交付文件与验证结果',
              'summary':'模拟执行的交付摘要'}
    assert client.post('/api/pm/pm-browser/deliver',json=delivery).status_code==200
    page.locator('[data-action="handoff-task"]').click()
    page.wait_for_function("document.querySelector('.brief-content').textContent.includes('验收状态：待验收')")
    assert '交付文件与验证结果' in page.locator('.brief-content pre').inner_text()
    assert page.locator('.brief-content img').count()==0
    assert '需求依据：v1' in page.locator('.brief-content').inner_text()
    assert client.post(f'/api/tasks/{task_id}/accept').status_code==200
    assert client.post('/api/pm/pm-browser/deliver',json=delivery).json()['duplicate'] is True
    assert client.post('/api/pm/pm-browser/deliver',json={**delivery,'deliverable':'冲突的新产物'}).status_code==409
    page.locator('[data-action="handoffs-refresh"]').click()
    page.locator('[data-action="handoff-task"]').click()
    page.wait_for_function("document.querySelector('.brief-content').textContent.includes('验收状态：已验收')")
    assert client.post(f'/api/tasks/{task_id}/reject',json={'reason':'需要补充测试证据'}).status_code==200
    assert client.post('/api/pm/pm-browser/deliver',json=delivery).json()['duplicate'] is True
    page.locator('[data-action="handoffs-refresh"]').click()
    page.locator('[data-action="handoff-task"]').click()
    page.wait_for_function("document.querySelector('.brief-content').textContent.includes('退回原因：需要补充测试证据')")
    page.screenshot(path=str(output/'project_brief_delivery_desktop.png'),full_page=True)
    page.set_viewport_size({'width':390,'height':844})
    page.locator('#project-brief-panel').scroll_into_view_if_needed()
    assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
    assert page.locator('.brief-content').evaluate('(e)=>e.scrollWidth<=e.clientWidth')
    page.screenshot(path=str(output/'project_brief_handoffs_mobile.png'),full_page=True)
    page.set_viewport_size({'width':1680,'height':1100})
    # Editing a draft disables dispatch; saved changes leave prior evidence intact.
    page.locator('[data-tab="document"]').click()
    page.locator('[data-field="title"]').fill('改稿后的项目')
    assert page.locator('[data-action="dispatch"]').is_disabled()
    page.locator('[data-action="save"]').click()
    page.wait_for_function("document.querySelector('.brief-status').textContent.includes('v2 · 已同步')")
    assert chat.brief_handoffs.get(handoff_id,1)['title']=='协作工作台'
    # A concurrent save forces review of the current version before dispatch.
    chat.briefs.save(handoff_id,{**document(),'title':'其他页面的新版本'},2)
    page.locator('[data-action="dispatch"]').click()
    page.wait_for_function("document.querySelector('.brief-status').textContent.includes('重新审阅')")
    assert controller.submit_task_from_chat.call_count==1
    page.reload()
    page.evaluate('(id)=>ProjectBriefUI.open(id)',handoff_id)
    page.locator('[data-tab="handoffs"]').click()
    page.wait_for_selector('[data-action="handoff-view"]')
    assert task_id in page.locator('.brief-content').inner_text()
    # Deleted task records are not presented as a stale execution status.
    controller.db.delete_task(task_id)
    page.locator('[data-action="handoffs-refresh"]').click()
    page.wait_for_function("document.querySelector('.brief-content').textContent.includes('记录不可用（尚未创建或已删除）')")
    page.locator('[data-action="handoff-task"]').click()
    page.wait_for_function("document.querySelector('.brief-status').textContent.includes('任务记录不可用')")
    page.evaluate('(id)=>ProjectBriefUI.open(id)',other_id)
    page.locator('[data-tab="handoffs"]').click()
    page.wait_for_selector('.brief-empty')
    assert page.locator('[data-action="handoff-view"]').count()==0
    assert not errors,errors


def verify_revision_history(page, revision_id, output):
    """Check persisted history, version comparison, restore and concurrent edits in the UI."""
    page.reload()
    page.evaluate('(id)=>ProjectBriefUI.open(id)',revision_id)
    page.locator('[data-tab="history"]').click()
    page.wait_for_selector('[data-history-choice] option[value="1"]',state='attached')
    page.locator('[data-history-choice]').select_option('1')
    page.locator('[data-action="history-view"]').click()
    page.wait_for_selector('.brief-diff')
    assert '第二版内容' in page.locator('.brief-diff').inner_text()
    assert '协作工作台' in page.locator('.brief-diff').inner_text()
    assert page.locator('[data-action="history-restore"]').is_enabled()
    page.screenshot(path=str(output/'project_brief_revisions_desktop.png'),full_page=True)
    page.set_viewport_size({'width':390,'height':844})
    page.locator('#project-brief-panel').scroll_into_view_if_needed()
    assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
    page.screenshot(path=str(output/'project_brief_revisions_mobile.png'),full_page=True)
    page.set_viewport_size({'width':1680,'height':1100})
    page.locator('[data-action="history-restore"]').click()
    page.wait_for_function("document.querySelector('.brief-status').textContent.includes('v3 · 已同步')")
    assert chat.briefs.get(revision_id)['document']['title']=='协作工作台'
    assert chat.briefs.revision(revision_id,2)['document']['title']=='第二版内容'
    page.wait_for_selector('[data-history-choice] option[value="3"]',state='attached')
    assert '恢复历史' in page.locator('[data-history-choice] option[value="3"]').inner_text()
    page.locator('[data-history-choice]').select_option('2')
    page.locator('[data-action="history-view"]').click()
    page.wait_for_function("document.querySelector('[data-action=history-restore]').disabled===false")
    # Locally dirty drafts cannot be overwritten by restore.
    page.locator('[data-tab="document"]').click()
    page.locator('[data-field="title"]').fill('本地草稿')
    page.locator('[data-tab="history"]').click()
    assert page.locator('[data-action="history-restore"]').is_disabled()
    page.locator('[data-action="undo"]').click()
    assert page.locator('[data-action="history-restore"]').is_enabled()
    # An external save after viewing a diff is caught by the server revision guard.
    saved=chat.briefs.get(revision_id)
    saved['document']['title']='并发保存的新内容'
    chat.briefs.save(revision_id,saved['document'],saved['revision'])
    page.locator('[data-action="history-restore"]').click()
    page.wait_for_function("document.querySelector('.brief-status').textContent.includes('版本冲突')")
    assert chat.briefs.get(revision_id)['document']['title']=='并发保存的新内容'
    # Reload and compare survive a fresh page lifecycle.
    page.reload()
    page.evaluate('(id)=>ProjectBriefUI.open(id)',revision_id)
    page.locator('[data-tab="history"]').click()
    page.wait_for_selector('[data-history-choice] option[value="4"]',state='attached')
    assert page.locator('[data-history-choice] option').count()==4


def verify_ai_review(page, review_id, other_id, output):
    """Verify selective adoption, escaping, stale review guards and draft preservation."""
    page.evaluate('(id)=>ProjectBriefUI.open(id)',review_id)
    page.locator('[data-action="generate"]').click()
    page.wait_for_selector('[data-review-index]')
    assert chat.briefs.get(review_id)['revision']==1
    assert page.locator('[data-action="review-apply"]').is_disabled()
    assert '<img src=x onerror=' in page.locator('.brief-diff').first.inner_text()
    assert page.locator('.brief-diff img').count()==0
    # The pending proposal survives a full page reload and conversation switches.
    page.reload()
    page.evaluate('(id)=>ProjectBriefUI.open(id)',review_id)
    page.locator('[data-tab="review"]').click()
    page.wait_for_selector('[data-review-index]')
    page.locator('.brief-change').filter(has_text='项目背景与问题').locator('input').check()
    page.evaluate('(id)=>ProjectBriefUI.open(id)',other_id)
    assert page.locator('[data-review-index]').count()==0
    page.evaluate('(id)=>ProjectBriefUI.open(id)',review_id)
    assert page.locator('[data-review-index]:checked').count()==1
    page.screenshot(path=str(output/'project_brief_review_desktop.png'),full_page=True)
    page.set_viewport_size({'width':390,'height':844})
    page.locator('#project-brief-panel').scroll_into_view_if_needed()
    assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
    page.screenshot(path=str(output/'project_brief_review_mobile.png'),full_page=True)
    page.set_viewport_size({'width':1680,'height':1100})
    page.locator('[data-action="review-apply"]').click()
    page.wait_for_function("document.querySelector('.brief-status').textContent.includes('v2 · 已同步')")
    saved=chat.briefs.get(review_id)
    assert saved['document']['title']=='协作工作台'
    assert saved['document']['sections']['background']=='建议的新背景'
    assert saved['document']['nodes'][0]['owner']==''
    assert saved['proposal'] is None
    # Accepted changes are a single undo step, kept separate from the saved version.
    page.locator('[data-action="undo"]').click()
    assert '未保存' in page.locator('.brief-status').inner_text()
    assert chat.briefs.get(review_id)['revision']==2
    page.locator('[data-action="redo"]').click()
    assert '已同步' in page.locator('.brief-status').inner_text()
    page.locator('[data-action="generate"]').click()
    page.wait_for_selector('[data-review-index]')
    page.locator('[data-tab="document"]').click()
    page.locator('[data-field="title"]').fill('尚未保存的手工编辑')
    page.locator('[data-tab="review"]').click()
    assert page.locator('[data-action="review-apply"]').is_disabled()
    page.locator('[data-action="review-discard"]').click()
    page.wait_for_function("!document.querySelector('[data-action=review-discard]')")
    page.locator('[data-tab="document"]').click()
    assert page.locator('[data-field="title"]').input_value()=='尚未保存的手工编辑'
    page.locator('[data-action="undo"]').click()
    page.locator('[data-action="generate"]').click()
    page.wait_for_selector('[data-review-index]')
    # Another page saves while the proposal is being reviewed; no stale acceptance.
    saved=chat.briefs.get(review_id)
    saved['document']['title']='其他页面已保存'
    chat.briefs.save(review_id,saved['document'],saved['revision'])
    page.locator('[data-action="review-all"]').click()
    page.locator('[data-action="review-apply"]').click()
    page.wait_for_function("document.querySelector('.brief-status').textContent.includes('版本冲突')")
    assert chat.briefs.get(review_id)['document']['title']=='其他页面已保存'
    page.locator('[data-action="reload"]').click()
    page.wait_for_selector('.brief-review-warning')
    assert '过期' in page.locator('.brief-review-warning').inner_text()
    assert page.locator('[data-action="review-apply"]').is_disabled()
    page.locator('[data-action="review-discard"]').click()
    page.wait_for_function("!document.querySelector('[data-action=review-discard]')")
    assert chat.briefs.get(review_id)['proposal'] is None


def verify_document_history(page, history_id, other_id):
    """Exercise grouped typing, save boundaries, conflict handling and conversation isolation."""
    page.evaluate('(id)=>ProjectBriefUI.open(id)', history_id)
    title = page.locator('[data-field="title"]')
    undo = page.locator('[data-action="undo"]')
    redo = page.locator('[data-action="redo"]')
    status = page.locator('.brief-status')
    original = title.input_value()
    assert undo.is_disabled() and redo.is_disabled()
    title.click()
    title.press('End')
    title.press_sequentially(' grouped edit')
    assert undo.is_enabled()
    title.press('Control+z')
    assert title.input_value() == original
    assert '已同步' in status.inner_text()
    title.press('Control+Shift+z')
    assert title.input_value() == original + ' grouped edit'
    page.locator('[data-action="save"]').click()
    page.wait_for_function("document.querySelector('.brief-status').textContent.includes('v2 · 已同步')")
    assert undo.is_enabled()
    undo.click()
    assert title.input_value() == original
    assert '未保存' in status.inner_text()
    assert chat.briefs.get(history_id)['document']['title'] == original + ' grouped edit'
    redo.click()
    assert '已同步' in status.inner_text()
    # A clean cached conversation retains its history when the server is unchanged.
    page.evaluate('(id)=>ProjectBriefUI.open(id)', other_id)
    assert page.locator('[data-action="undo"]').is_disabled()
    page.evaluate('(id)=>ProjectBriefUI.open(id)', history_id)
    assert undo.is_enabled()
    undo.click()
    title.fill('新分支')
    assert redo.is_disabled()
    # Sections stay open and keep focus when undone by a keyboard shortcut.
    section = page.locator('[data-section="risks"]')
    section.evaluate('(el)=>el.closest("details").open=true')
    before = section.input_value()
    section.fill('新的风险说明')
    section.press('Control+z')
    assert section.input_value() == before
    assert section.evaluate('(el)=>el===document.activeElement')
    section.press('Control+y')
    assert section.input_value() == '新的风险说明'
    # A conflicting save must keep both the local draft and its undo history.
    saved = chat.briefs.get(history_id)
    chat.briefs.save(history_id, saved['document'], saved['revision'])
    page.locator('[data-action="save"]').click()
    page.wait_for_function("document.querySelector('.brief-status').textContent.includes('版本冲突')")
    assert undo.is_enabled()
    undo.click()
    assert section.input_value() == before
    page.locator('[data-action="reload"]').click()
    page.wait_for_function("document.querySelector('.brief-status').textContent.includes('v3 · 已同步')")
    assert undo.is_disabled() and redo.is_disabled()
    # A server-side (AI/other-page) revision starts a new history boundary.
    title.fill('本地已保存')
    page.locator('[data-action="save"]').click()
    page.wait_for_function("document.querySelector('.brief-status').textContent.includes('v4 · 已同步')")
    saved = chat.briefs.get(history_id)
    saved['document']['title'] = '外部更新'
    chat.briefs.save(history_id, saved['document'], saved['revision'])
    page.evaluate('(id)=>ProjectBriefUI.refresh(id)', history_id)
    assert title.input_value() == '外部更新'
    assert undo.is_disabled() and redo.is_disabled()
    # Undo in the chat composer must not consume the document's history.
    title.fill('编辑范围隔离')
    composer = page.locator('#chat-input')
    composer.fill('只撤销聊天输入')
    composer.press('Control+z')
    assert title.input_value() == '编辑范围隔离'
    undo.click()
    assert '已同步' in status.inner_text()


def verify_graph_history(page):
    """Check drag checkpoints, subtree recovery, no-op behavior and the history bound."""
    undo = page.locator('[data-action="undo"]')
    redo = page.locator('[data-action="redo"]')
    page.locator('#project-brief-panel [data-tab="graph"]').click()
    page.locator('[data-action="fit"]').click()
    page.locator('.brief-canvas').scroll_into_view_if_needed()
    node = page.locator('[data-node="module"]')
    old_position = node.get_attribute('transform')
    rect = node.locator('rect').bounding_box()
    page.mouse.move(rect['x']+15, rect['y']+5)
    page.mouse.down()
    page.mouse.move(rect['x']+65, rect['y']+25, steps=5)
    page.mouse.up()
    moved_position = node.get_attribute('transform')
    assert moved_position != old_position and undo.is_enabled(), (old_position,moved_position,rect)
    undo.click()
    assert node.get_attribute('transform') == old_position
    assert undo.is_disabled() and redo.is_enabled()
    # A click without a drag is not an edit and must not erase the redo stack.
    node.click(position={'x':15, 'y':5})
    assert undo.is_disabled() and redo.is_enabled()
    redo.click()
    assert node.get_attribute('transform') == moved_position
    node.focus()
    page.keyboard.press('Control+z')
    assert node.get_attribute('transform') == old_position
    assert node.evaluate('(el)=>el===document.activeElement')
    page.keyboard.press('Control+Shift+z')
    assert node.get_attribute('transform') == moved_position
    page.locator('[data-action="delete"]').click()
    assert page.locator('[data-node]').count() == 1
    undo.click()
    assert page.locator('[data-node]').count() == 4
    redo.click()
    assert page.locator('[data-node]').count() == 1
    undo.click()
    page.locator('[data-node="detail"]').click()
    page.locator('[data-node-field="owner"]').fill('Boss')
    page.locator('[data-node-field="owner"]').press('Tab')
    undo.click()
    assert page.locator('[data-node-field="owner"]').input_value() == ''
    redo.click()
    assert page.locator('[data-node-field="owner"]').input_value() == 'Boss'
    # A saved API response uses schema field order; undoing a later edit is still clean.
    page.locator('[data-action="add"]').click()
    page.locator('[data-action="save"]').click()
    page.wait_for_function("document.querySelector('.brief-status').textContent.includes('v6 · 已同步')")
    page.locator('[data-action="add"]').click()
    undo.click()
    assert '已同步' in page.locator('.brief-status').inner_text()
    page.locator('#project-brief-panel [data-tab="document"]').click()
    title = page.locator('[data-field="title"]')
    for i in range(55):
        title.fill(f'edit-{i}')
        title.press('Tab')
    for _ in range(50):
        undo.click()
    assert undo.is_disabled()
    assert title.input_value() == 'edit-4'


with sync_playwright() as p:
    browser=p.chromium.launch(executable_path=os.environ.get('BRIEF_BROWSER_EXECUTABLE', '/usr/bin/google-chrome'),headless=True,args=['--no-sandbox','--disable-dev-shm-usage'])
    page=browser.new_page(viewport={'width':1680,'height':1100},device_scale_factor=1)
    page.on('pageerror',lambda e:errors.append(str(e)))
    page.on('dialog',lambda d:d.accept())
    sockets=[]
    def route(r):
        req=r.request;path=urlparse(req.url).path
        if path=='/':r.fulfill(status=200,body=html,content_type='text/html');return
        if path.startswith('/static/'):
            file=root/'lan_mesh/web'/path.lstrip('/')
            r.fulfill(status=200,body=file.read_bytes(),content_type='text/css' if path.endswith('.css') else 'application/javascript' if path.endswith('.js') else 'application/json');return
        if path=='/api/station/roles':r.fulfill(status=200,json={'secretary':'active'});return
        if path.startswith(('/api/conversations', '/api/tasks/', '/api/pm/', '/api/pm-threads/')) or path=='/api/secretary/chat':
            response=client.request(req.method,path+('?' + urlparse(req.url).query if urlparse(req.url).query else ''),content=req.post_data,headers={'content-type':'application/json'})
            if path=='/api/secretary/chat' and response.status_code==200:
                for ws in sockets:ws.send(json.dumps({'type':'chat_reply','data':response.json()}))
            r.fulfill(status=response.status_code,body=response.content,headers=dict(response.headers));return
        r.fulfill(status=200,body='{}',content_type='application/json')
    page.route('**/*',route)
    page.goto('http://brief.test/')
    page.evaluate('(id)=>ProjectBriefUI.open(id)',cid)
    page.locator('[data-field="title"]').fill('Boss 的协作项目')
    page.locator('[data-section="background"]').fill('项目背景：从聊天形成可编辑的项目描述。')
    page.locator('[data-action="save"]').click()
    page.wait_for_function("document.querySelector('.brief-status').textContent.includes('v1')")
    page.locator('[data-action="generate"]').click()
    page.wait_for_selector('[data-review-index]')
    assert chat.briefs.get(cid)['revision']==1
    accept_all_suggestions(page)
    page.wait_for_function("document.querySelector('.brief-status').textContent.includes('v2')")
    page.locator('[data-tab="graph"]').click()
    page.locator('[data-action="layout"]').click()
    assert page.locator('[data-node]').count()==4
    # Three nested levels and server-backed save.
    assert len(page.locator('[data-node="detail"]').all())==1
    rect=page.locator('[data-node="module"] rect').bounding_box()
    page.mouse.move(rect['x']+15,rect['y']+5);page.mouse.down();page.mouse.move(rect['x']+100,rect['y']+20,steps=5);page.mouse.up()
    page.locator('[data-action="save"]').click()
    page.wait_for_function("document.querySelector('.brief-status').textContent.includes('v3')")
    saved=chat.briefs.get(cid)
    assert saved['document']['nodes'][0]['x']>40
    # Editing titles is reflected in Markdown and survives reload.
    page.locator('[data-node="detail"]').click()
    page.locator('[data-node-field="title"]').fill('原子保存草稿')
    page.locator('[data-node-field="title"]').press('Tab')
    page.locator('[data-action="child"]').click()
    assert '最多三层' in page.locator('.brief-status').inner_text()
    assert page.locator('[data-node]').count()==4
    page.locator('[data-action="save"]').click()
    page.wait_for_function("document.querySelector('.brief-status').textContent.includes('v4')")
    # Select and add a dependency, then reject reverse edge (cycle).
    page.locator('[data-action="connect"]').click()
    page.locator('[data-node="module"]').click(position={'x':15,'y':15})
    page.locator('[data-node="export"]').click(position={'x':15,'y':15})
    page.locator('[data-node="export"]').click(position={'x':15,'y':15})
    page.locator('[data-node="module"]').click(position={'x':15,'y':15})
    assert '循环' in page.locator('.brief-status').inner_text()
    page.locator('[data-action="connect"]').click()
    page.locator('[data-action="undo"]').click()
    page.locator('[data-action="redo"]').click()
    # Drafts survive switches, including server-side version conflicts.
    page.locator('[data-tab="document"]').click()
    page.locator('[data-field="title"]').fill('未保存的修改')
    page.evaluate('(id)=>ProjectBriefUI.open(id)',other)
    page.evaluate('(id)=>ProjectBriefUI.open(id)',cid)
    assert page.locator('[data-field="title"]').input_value()=='未保存的修改'
    value=chat.briefs.get(cid);chat.briefs.save(cid,value['document'],value['revision'])
    page.locator('[data-action="save"]').click()
    page.wait_for_function("document.querySelector('.brief-status').textContent.includes('版本冲突')")
    assert page.locator('[data-field="title"]').input_value()=='未保存的修改'
    with page.expect_download() as info:page.locator('[data-action="export"]').click()
    dl=info.value;assert dl.suggested_filename=='项目描述.md'
    assert '未保存的修改' in Path(dl.path()).read_text()
    # Load latest, inspect all three layers, take desktop + mobile evidence.
    page.locator('[data-action="reload"]').click()
    page.wait_for_function("document.querySelector('.brief-status').textContent.includes('v5') && document.querySelector('.brief-status').textContent.includes('已同步')")
    assert page.locator('[data-field=title]').input_value()=='协作工作台'
    page.locator('[data-tab="graph"]').click()
    page.locator('[data-action="fit"]').click()
    output=root/'temp_resault';output.mkdir(exist_ok=True)
    page.screenshot(path=str(output/'project_brief_desktop.png'),full_page=True)
    page.set_viewport_size({'width':390,'height':844})
    page.locator('#project-brief-panel').scroll_into_view_if_needed()
    assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
    page.screenshot(path=str(output/'project_brief_mobile.png'),full_page=True)
    assert not errors,errors
    # Integration smoke: original dashboard script, actual chat endpoint and WS reply.
    html=(root/'lan_mesh/web/templates/dashboard.html').read_text()
    page=browser.new_page(viewport={'width':1680,'height':1100})
    page.on('pageerror',lambda e:errors.append(str(e)))
    page.route('**/*',route)
    page.route_web_socket('**/ws',lambda ws:sockets.append(ws))
    page.goto('http://brief.test/')
    page.locator('.tabs [data-tab="chat"]').click()
    page.wait_for_selector('[data-field="title"]')
    page.evaluate('(id)=>switchConversation(id)',cid)
    page.wait_for_function("document.querySelector('[data-field=title]').value==='协作工作台'")
    page.locator('#chat-input').fill('帮我做一个需求编辑器，请整理项目描述')
    page.locator('#chat-send-btn').click()
    page.wait_for_function("document.querySelector('.brief-status').textContent.includes('有 AI 建议待审阅')")
    assert chat.briefs.get(cid)['revision']==5
    assert '修改建议已生成' in page.locator('#chat-messages').inner_text()
    accept_all_suggestions(page)
    page.wait_for_function("document.querySelector('.brief-status').textContent.includes('v6')")
    page.locator('#project-brief-panel [data-tab="graph"]').click()
    page.locator('[data-action="layout"]').click()
    page.locator('[data-action="save"]').click()
    page.wait_for_function("document.querySelector('.brief-status').textContent.includes('v7')")
    page.locator('[data-action="fit"]').click()
    page.screenshot(path=str(output/'project_brief_dashboard.png'),full_page=True)

    # Delay responses to reproduce typing during reload and switching during save.
    page.locator('#project-brief-panel [data-tab="document"]').click()
    held=[]
    def hold_response(r):
        req=r.request
        response=client.request(req.method,urlparse(req.url).path,content=req.post_data,
                                headers={'content-type':'application/json'})
        held.append((r,response))

    def release_response():
        r,response=held.pop()
        r.fulfill(status=response.status_code,body=response.content,headers=dict(response.headers))

    brief_url=f'**/api/conversations/{cid}/brief'
    page.route(brief_url,hold_response,times=1)
    page.evaluate('(id)=>{window.pendingBriefOpen=ProjectBriefUI.open(id)}',cid)
    page.locator('[data-field="title"]').fill('加载中输入的修改')
    assert held
    release_response()
    page.evaluate('window.pendingBriefOpen')
    assert page.locator('[data-field="title"]').input_value()=='加载中输入的修改'

    page.route(brief_url,hold_response,times=1)
    page.locator('[data-action="save"]').click()
    page.evaluate('(id)=>ProjectBriefUI.open(id)',other)
    assert held
    release_response()
    page.wait_for_function("document.querySelector('#project-brief-panel').getAttribute('aria-busy')==='false'")
    page.evaluate('(id)=>ProjectBriefUI.open(id)',cid)
    assert page.locator('[data-field="title"]').input_value()=='加载中输入的修改'
    assert 'v8 · 已同步' in page.locator('.brief-status').inner_text()
    assert not errors,errors
    # UI-040: terminal diagnostics remain readable and escaped on desktop/mobile.
    failure_reason='模型调用失败；已用尽重试。<img src=x onerror=alert(1)>\n'+'trace_'*40
    failed_task=Task(name='失败诊断演示',status='failed',subtasks=[
        {'name':'生成实现','status':'failed','error':failure_reason},
        {'name':'运行验证','status':'blocked','error':'任务因生成实现失败而停止；尚未执行。'},
    ])
    page.evaluate('(task)=>{allTasks=[task];showTaskDetail(task.task_id)}',failed_task.to_dict())
    assert '[失败]' in page.locator('#modal').inner_text()
    assert '[已阻塞]' in page.locator('#modal').inner_text()
    assert failure_reason in page.locator('.task-step-error').first.inner_text()
    assert page.locator('#modal img').count()==0
    page.screenshot(path=str(output/'task_failure_desktop.png'),full_page=True)
    page.set_viewport_size({'width':390,'height':844})
    assert page.locator('.task-step-error').first.is_visible()
    assert page.locator('#modal').evaluate('(el)=>el.scrollWidth<=el.clientWidth+1')
    page.screenshot(path=str(output/'task_failure_mobile.png'),full_page=True)
    page.evaluate('closeOverlayForce()')
    page.set_viewport_size({'width':1680,'height':1100})
    assert not errors,errors
    verify_execution_inspection(page,output)
    verify_task_control(page,output)
    verify_restart_recovery(page,output)
    verify_task_resubmission(page,output)
    verify_pm_checkpoint(page,output)
    verify_recovery_assessment(page,output)
    verify_pm_resume(page,output)
    verify_pm_clarification(page,output)
    assert not errors,errors
    # UI-036: test unified editing history against isolated conversations and the real API.
    history_id=chat.create_conversation('编辑历史测试')['id']
    untouched_id=chat.create_conversation('无编辑历史')['id']
    history_document=document()
    history_document['nodes'][-1].update(x=650,y=60)
    chat.briefs.save(history_id,history_document,0)
    page=browser.new_page(viewport={'width':1680,'height':1100})
    page.on('pageerror',lambda e:errors.append(str(e)))
    page.on('dialog',lambda d:d.accept())
    # Use the minimal editor shell to keep dashboard refreshes out of history assertions.
    html=re.sub(r'<script>.*?</script>','',html,flags=re.S)
    html=html.replace('<script src="/static/project-brief.js"></script>', '''<script>
function apiFetch(url,opts){return fetch(url,opts)}
function showToast(msg){window.lastToast=msg}
document.querySelectorAll('.panel').forEach(p=>p.style.display=p.id==='panel-chat'?'block':'none');
</script><script src="/static/project-brief.js"></script>''')
    page.route('**/*',route)
    page.goto('http://brief.test/')
    verify_document_history(page,history_id,untouched_id)
    verify_graph_history(page)
    page.screenshot(path=str(output/'project_brief_history_desktop.png'),full_page=True)
    page.set_viewport_size({'width':390,'height':844})
    page.locator('#project-brief-panel').scroll_into_view_if_needed()
    assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
    assert page.locator('[data-action="redo"]').is_visible()
    page.screenshot(path=str(output/'project_brief_history_mobile.png'),full_page=True)
    assert not errors,errors
    review_id=chat.create_conversation('AI审阅测试')['id']
    chat.briefs.save(review_id,document(),0)
    candidate=document()
    candidate['title']='<img src=x onerror=alert(1)>'
    candidate['sections']['background']='建议的新背景'
    candidate['nodes'][0]['owner']='AI 建议负责人'
    runtime._call_llm_with_routing.return_value={'content':json.dumps({'reply':'待审阅','project_brief':candidate},ensure_ascii=False)}
    page.set_viewport_size({'width':1680,'height':1100})
    verify_ai_review(page,review_id,untouched_id,output)
    assert not errors,errors
    revision_id=chat.create_conversation('持久化版本历史测试')['id']
    chat.briefs.save(revision_id,document(),0)
    chat.briefs.save(revision_id,{**document(),'title':'第二版内容'},1)
    verify_revision_history(page,revision_id,output)
    handoff_id=chat.create_conversation('需求下发追溯')['id']
    verify_handoff(page,handoff_id,untouched_id,output)
    assert not errors,errors
    browser.close()
print('PASS: generation, nested DAG, drag, editing, cycle guard, grouped typing, document/graph undo/redo, save history, clean state, 50-step bound, isolation, conflict reload, external revision, export, mobile, dashboard chat + WebSocket, delayed load/save; zero page errors')
print('PASS: AI review, selective adoption, saved document protection, proposal persistence, draft-preserving discard, stale proposal guard, undo after acceptance, escaped HTML, desktop/mobile diff; zero page errors')
print('PASS: revision history, persisted snapshots, comparison, restore as new version, dirty draft protection, concurrent save rejection, reload and desktop/mobile history; zero page errors')

print('PASS: confirmed handoff, deduplication, frozen revision, task/delivery/acceptance APIs, dirty and stale guards, persistence, missing task, escaping, conversation isolation, desktop/mobile; simulated execution')

print('PASS: original-node inspection, unknown/running/inactive evidence, retry after error, stale response isolation, read-only task state, escaped output and desktop/mobile; temporary SQLite and simulated PM state')

print("PASS: cancel/pause intent versus PM confirmation, explicit retry, retained task on unknown stop, original request ID, disabled resubmit, desktop/mobile; real API and cooperative PM instance")

print("PASS: restart recovery, legacy interrupted protection, stale resubmit rejection, original callback clearing, desktop/mobile; real task/PM API and temporary SQLite")
print("PASS: resubmission preserves inputs, lost response and repeated clicks reuse one replacement, source links, server race rejection, desktop/mobile; real API/SQLite, simulated PM")
print("PASS: persistent PM checkpoint evidence, completed/uncertain/unstarted counts, no raw outputs, write failure and legacy missing records, desktop/mobile; real API/SQLite")
print("PASS: recovery evidence versus saved output, original execution identity, read-only API/checkpoint, escaping, query retry and late-result isolation, desktop/mobile")

print("PASS: explicit PM recovery, version-bound authorization, original outputs and deadline, retry/deduplication, actual delivery, desktop/mobile and late-response isolation; simulated executor")

print("PASS: clarification recovery keeps original question/deadline, saved reply ACK loss and retry, cross-question draft isolation, PM thread failure retention, actual delivery and late-response guard; real API/SQLite and simulated executor")
