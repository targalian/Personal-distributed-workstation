/* Shared dashboard/React workbench using the same authenticated transport. */
'use strict';
const mesh=MeshTransport.create(), $=id=>document.getElementById(id);
let taskId='', browserId='', busy=false;
const labels={running:'执行中',paused:'已暂停',completed:'已完成',awaiting_input:'等待回复',pending:'排队中',human:'人工接管',pausing:'暂停未确认',automatic:'自动控制'};
async function request(url,body){const r=await mesh.request(url,body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});const d=await r.json();if(!r.ok)throw Error(d.detail||d.message||r.statusText);return d;}
function rows(id,values){$(id).replaceChildren(...values.map(value=>{const div=document.createElement('div');div.className='row';div.textContent=value;return div;}));}
async function action(fn){if(busy)return;busy=true;$('error').hidden=true;try{await fn();}catch(e){$('error').textContent=e.message;$('error').hidden=false;}finally{busy=false;}}
async function load(){if(!taskId){$('detail').hidden=true;return;}const selected=taskId;const d=await request('/api/collaboration/'+encodeURIComponent(selected));if(selected!==taskId)return;const t=d.task;const control=d.takeover.state;$('detail').hidden=false;$('artifact-manager').href='/static/artifacts/index.html?'+new URLSearchParams({task_id:taskId,project_id:t.project_id||''});$('task-name').textContent=t.name;$('task-state').textContent=labels[t.status]||t.status;$('ownership').textContent='控制状态：'+(labels[control]||control)+' · PM：'+(d.pm?.agent_name||t.pm_agent_id||'尚未分配');const details=typeof d.takeover.details==='string'?JSON.parse(d.takeover.details):{};$('pause-detail').textContent=control==='pausing'?'尚未确认所有执行退出。请稍后点击“重新确认暂停”。失联节点仍可能在执行。':control==='human'?'自动执行已退出。结束接管后仍需核对产物，再恢复任务。':details.message||'';
$('takeover').textContent=control==='pausing'?'重新确认暂停':'暂停并接管';$('takeover').disabled=mesh.getRole()==='viewer'||control==='human';$('release').disabled=mesh.getRole()==='viewer'||control!=='human';$('send').disabled=mesh.getRole()==='viewer'||control!=='automatic';
$('conversation').href='/?conv='+encodeURIComponent(d.conversation_id)+'&pm='+encodeURIComponent(d.pm_thread_id);$('graph').href='/spa/#/dag/'+encodeURIComponent(taskId);
const members=d.teams.flatMap(team=>team.members||[]);rows('participants',[d.pm?`${d.pm.agent_name||d.pm.pm_id} · ${d.pm.hostname||'主机未知'} · ${labels[d.pm.status]||d.pm.status}`:'PM 尚未分配',...members.map(m=>`${m.agent_name||m.member_id||m.agent_id} · ${m.current_task||m.role||''} · ${labels[m.status]||m.status||'未知'}`)]);
const nodes=d.graph.nodes||[], names=new Map(nodes.map(n=>[n.id,n.name||n.id]));rows('dependencies',(d.graph.edges||[]).map(e=>`${names.get(e.target)||e.target} 等待 ${names.get(e.source)||e.source}`).concat(nodes.filter(n=>n.status==='awaiting_input').map(n=>`${n.name} 等待你的回复`)));if(!$('dependencies').children.length)rows('dependencies',['当前没有等待关系']);$('question').textContent=d.clarification||'当前没有待回复问题';
const review=t.output_data?._delivery?.acceptance_review;renderEvidence(review);rows('notices',d.notices.map(n=>n.message));$('status').textContent='已更新';}
$('tasks').onchange=()=>action(async()=>{taskId=$('tasks').value;browserId='';$('browser-controls').hidden=true;await load();});$('refresh').onclick=()=>action(load);$('takeover').onclick=()=>action(async()=>{await request('/api/collaboration/'+encodeURIComponent(taskId)+'/takeover',{});await load();});$('release').onclick=()=>action(async()=>{await request('/api/collaboration/'+encodeURIComponent(taskId)+'/release',{});await load();});$('send').onclick=()=>action(async()=>{const d=await request('/api/collaboration/'+encodeURIComponent(taskId)+'/reply',{message:$('reply').value});if(!d.ok)throw Error(d.message||'回复未送达');$('reply').value='';$('reply-status').textContent='回复已送达';});
async function browserAction(op,params={}){const d=await request('/api/browser/sessions/'+encodeURIComponent(browserId)+'/'+op,{...params,task_id:taskId});$('browser-status').textContent='会话 '+browserId+' · '+(labels[d.mode]||d.mode||'');if(d.image)$('browser-image').src='data:image/png;base64,'+d.image;if(d.text!==undefined)$('browser-text').textContent=d.text;return d;}
$('browser-create').onclick=()=>action(async()=>{const d=await request('/api/browser/sessions',{task_id:taskId});browserId=d.session_id;$('browser-controls').hidden=false;$('browser-status').textContent='浏览器已创建 · 自动控制';});$('browser-takeover').onclick=()=>action(()=>browserAction('takeover'));$('browser-release').onclick=()=>action(()=>browserAction('release'));$('browser-close').onclick=()=>action(async()=>{await browserAction('close');browserId='';$('browser-controls').hidden=true;});$('browser-go').onclick=()=>action(()=>browserAction('actions',{operation:'navigate',url:$('browser-url').value}));$('browser-snapshot').onclick=()=>action(()=>browserAction('actions',{operation:'snapshot'}));
action(async()=>{await mesh.ensure();const data=await request('/api/tasks?limit=200');for(const t of data.tasks){const o=document.createElement('option');o.value=t.task_id;o.textContent=t.name||t.task_id;$('tasks').append(o);}const initial=new URLSearchParams(location.search).get('task_id');if(initial&&data.tasks.some(t=>t.task_id===initial)){$('tasks').value=taskId=initial;await load();}else $('status').textContent='请选择任务';});

$('browser-image').onclick=e=>action(()=>{const img=$('browser-image'), rect=img.getBoundingClientRect();return browserAction('actions',{operation:'click_point',x:(e.clientX-rect.left)*img.naturalWidth/rect.width,y:(e.clientY-rect.top)*img.naturalHeight/rect.height});});
$('browser-type').onclick=()=>action(()=>browserAction('actions',{operation:'type_text',text:$('browser-input').value}));
$('browser-enter').onclick=()=>action(()=>browserAction('actions',{operation:'press_enter'}));

function renderEvidence(review){
  $('evidence').replaceChildren();
  for(const check of review?.checks||[]){
    const row=document.createElement('div');row.className='row';
    const title=document.createElement('p');title.textContent=`${check.met?'自评满足':'待验证'} · ${check.criterion||''} · ${check.evidence||''}`;row.append(title);
    (check.source_refs||[]).forEach((ref,index)=>{
      const button=document.createElement('button'), preview=document.createElement('pre');let offset=0;
      button.textContent='查看来源 '+(index+1);
      button.onclick=()=>action(async()=>{
        if(ref.content!==undefined){preview.textContent=String(ref.content);button.disabled=true;return;}
        const query=new URLSearchParams({...ref.scope,offset:String(offset),limit:'4000'});
        const data=await request('/api/runtime/artifacts/'+encodeURIComponent(ref.execution_id)+'/'+encodeURIComponent(ref.artifact_id||ref.id)+'?'+query);
        preview.textContent+=data.content;offset=data.next_offset;button.disabled=!data.truncated;button.textContent=data.truncated?'继续读取来源':'来源已完整读取';
      });row.append(button,preview);
    });$('evidence').append(row);
  }
  if(!$('evidence').children.length)rows('evidence',['尚无验收记录']);
}
