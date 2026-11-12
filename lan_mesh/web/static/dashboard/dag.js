// ── Secretary / workspace adapters for the shared task-flow workbench ──
let _chatDagOpen=false;
let _chatDagContext=null;
function dagFrame(id){return document.getElementById(id);}
function dagCanLeave(id){return dagFrame(id)?.contentWindow?.DagWorkbench?.canLeave()!==false;}
function dagSend(id,data){dagFrame(id)?.contentWindow?.postMessage(data,location.origin);}
function refreshDagTaskSelect(){
  const frame=dagFrame('workspace-dag-frame');
  if(frame&&!frame.getAttribute('src'))frame.src='/static/dag/index.html';
}
async function openWorkspaceDag(taskId){
  if(!dagCanLeave('workspace-dag-frame'))return;
  const frame=dagFrame('workspace-dag-frame');
  if(frame.contentWindow?.DagWorkbench)await frame.contentWindow.DagWorkbench.setContext({task_id:taskId});
  else frame.src='/static/dag/index.html?'+new URLSearchParams({task_id:taskId});
  switchTab('workspace');switchWsView('dag');
}
async function toggleChatDag(force,taskId=''){
  const open=typeof force==='boolean'?force:!_chatDagOpen;
  if(!open&&!dagCanLeave('chat-dag-frame'))return;
  _chatDagOpen=open;
  const layout=document.querySelector('.chat-layout');layout.classList.toggle('with-dag',open);
  document.getElementById('chat-dag-panel').hidden=!open;
  document.getElementById('chat-dag-toggle').setAttribute('aria-expanded',String(open));
  if(open)await syncChatDag(taskId);else{document.getElementById('chat-dag-panel').classList.remove('dag-expanded');document.getElementById('chat-dag-toggle').focus();}
}
async function syncChatDag(taskId=''){
  if(!_chatDagOpen)return;
  const frame=dagFrame('chat-dag-frame'),meta=_convList.find(c=>c.id===_activeConvId);
  if(!_activeConvId){frame.removeAttribute('src');_chatDagContext=null;return;}
  const value={conv_id:_activeConvId,project_id:meta?.project_id||'',task_id:taskId};
  const key=JSON.stringify(value);if(key===_chatDagContext)return;
  _chatDagContext=key;
  if(frame.contentWindow?.DagWorkbench)await frame.contentWindow.DagWorkbench.setContext(value);
  else frame.src='/static/dag/index.html?'+new URLSearchParams(value);
}
function chatDagRefresh(taskId){
  for(const id of ['chat-dag-frame','workspace-dag-frame'])dagSend(id,{type:'dag-refresh',task_id:taskId});
}
function chatDraftContext(data){
  const input=document.getElementById('chat-input');
  const prefix=`关于任务“${data.task_name||data.task_id}”（${data.task_id}）${data.node_name?`的步骤“${data.node_name}”`:''}：\n`;
  if(!input.value.startsWith(prefix))input.value=prefix+input.value;
  input.dispatchEvent(new Event('input'));input.focus();input.setSelectionRange(input.value.length,input.value.length);
  document.getElementById('chat-compose-hint').textContent=data.draft?'图中有未保存修改，建议先保存，再请秘书调整。':'已带入任务和步骤；补充问题后发送。Shift + Enter 换行。';
  if(window.innerWidth<900)document.getElementById('chat-input').scrollIntoView({block:'center',behavior:'smooth'});
}
async function discussDagInChat(data){
  if(!_convList.length)await loadConversations();
  let conv=_convList.find(c=>c.id===_activeConvId);
  if(data.project_id&&conv?.project_id!==data.project_id){
    const match=_convList.find(c=>c.project_id===data.project_id);
    if(!match){showToast('请先打开该项目的秘书对话，再选择对应任务。','info');return;}
    if(!dagCanLeave('chat-dag-frame'))return;
    await switchConversation(match.id);
  }
  if(!_activeConvId){showToast('请先新建或选择一个秘书对话。','info');return;}
  switchTab('chat');await toggleChatDag(true,data.task_id);chatDraftContext(data);
}
window.addEventListener('message',async event=>{
  if(event.origin!==location.origin)return;
  const id=['chat-dag-frame','workspace-dag-frame'].find(id=>dagFrame(id)?.contentWindow===event.source);
  if(!id)return;
  const data=event.data||{};
  if(data.type==='dag-discuss'){
    if(id==='chat-dag-frame')chatDraftContext(data);else await discussDagInChat(data);
  }
  if(data.type==='dag-saved')refreshTasks();
  if(data.type==='dag-expand'){
    const target=id==='chat-dag-frame'?document.getElementById('chat-dag-panel'):document.getElementById('ws-dag');
    target.classList.toggle('dag-expanded');
    dagSend(id,{type:'dag-expanded',expanded:target.classList.contains('dag-expanded')});
  }
});
