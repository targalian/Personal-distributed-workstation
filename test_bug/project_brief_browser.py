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
from tests.test_project_brief import document

root=PROJECT_ROOT
folder=tempfile.TemporaryDirectory(prefix='brief-browser-')
controller=SimpleNamespace(model_router=None,_default_model='',secretary_active=True)
runtime=Mock()
runtime._call_llm_with_routing.return_value={'content':json.dumps({'reply':'已整理','project_brief':document()},ensure_ascii=False)}
chat=ChatHandler(runtime,controller,shared_folder=SimpleNamespace(path=folder.name))
controller.chat_handler=chat
controller.state=SimpleNamespace(ws_clients=set())
chat._build_status_context=lambda: ''
chat._auto_title=Mock()
cid=chat._active_conv_id
other=chat.create_conversation('第二个项目')['id']
app=FastAPI();app.include_router(build_brief_routes(controller));app.include_router(build_chat_routes(controller));client=TestClient(app)
html=(root/'lan_mesh/web/templates/dashboard.html').read_text()
html=re.sub(r'<script>.*?</script>','',html,flags=re.S)
html=html.replace('<script src="/static/project-brief.js"></script>', '''<script>
function apiFetch(url,opts){return fetch(url,opts)}
function showToast(msg){window.lastToast=msg}
document.querySelectorAll('.panel').forEach(p=>p.style.display=p.id==='panel-chat'?'block':'none');
</script><script src="/static/project-brief.js"></script>''')
errors=[]


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
        if path.startswith('/api/conversations') or path=='/api/secretary/chat':
            response=client.request(req.method,path,content=req.post_data,headers={'content-type':'application/json'})
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
    page.wait_for_function("document.querySelector('.brief-status').textContent.includes('v6')")
    assert '项目描述草稿已更新至 v6' in page.locator('#chat-messages').inner_text()
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
    browser.close()
print('PASS: generation, nested DAG, drag, editing, cycle guard, grouped typing, document/graph undo/redo, save history, clean state, 50-step bound, isolation, conflict reload, external revision, export, mobile, dashboard chat + WebSocket, delayed load/save; zero page errors')
