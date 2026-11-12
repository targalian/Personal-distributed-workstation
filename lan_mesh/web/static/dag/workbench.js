/* One task-flow editor shared by secretary chat, classic workspace and React. */
(function () {
  'use strict';
  const $=id=>document.getElementById(id), M=DagModel, mesh=MeshTransport.create();
  const params=new URLSearchParams(location.search);
  let graph=M.normalize(), base=M.normalize(), task=null, tasks=[], selected=null, editing=false, busy=false, epoch=0;
  let undo=[],redo=[],context={conv_id:params.get('conv_id')||'',project_id:params.get('project_id')||''}, threadIds=new Set();
  let scoped=false, ready=false, expanded=false;
  const intro=$('inspector').innerHTML;
  const canvas=new DagCanvas($('graph'),value=>select(value));
  if(window.parent!==window)document.body.classList.add('embedded');
  const dirty=()=>M.signature(graph)!==M.signature(base);
  const editable=()=>task?.status==='pending'&&['boss','operator'].includes(mesh.getRole());
  function report(text,type='notice'){ $(type).textContent=text;$(type).hidden=!text; }
  function notifyParent(type,extra={}){if(window.parent!==window)window.parent.postMessage({type,...extra},location.origin);}
  async function request(path,init={}) {
    const controller=new AbortController(), timer=setTimeout(()=>controller.abort(),10000);
    try {const response=await mesh.request(path,{...init,signal:controller.signal});let data;
      try{data=await response.json();}catch{throw Error('服务返回了无法读取的内容，请重试。');}
      if(!response.ok){const error=Error(data.detail||data.message||`请求失败（${response.status}）`);error.status=response.status;throw error;}return data;
    } catch(error){if(error.name==='AbortError')throw Error('连接超时，当前草稿仍保留，请稍后重试。');throw error;}finally{clearTimeout(timer);}
  }
  async function getGraph(id){try{return M.normalize(await request(`/api/tasks/${encodeURIComponent(id)}/graph`));}catch(e){if(e.status===404)return M.normalize();throw e;}}
  function allowed(t){return !scoped||(context.project_id?t.project_id===context.project_id:threadIds.has(t.task_id));}
  function canLeave(){if(busy){report('当前操作尚未结束，请稍候。');return false;}if(!dirty())return true;if(!confirm('流程还有未保存的修改。放弃这些修改并继续？'))return false;graph=M.copy(base);undo=[];redo=[];editing=false;render();return true;}
  function header(){
    const modified=dirty();$('save-state').textContent=busy?'正在处理…':modified?'● 有未保存修改':task?'已与任务同步':'';
    $('task-state').textContent=task?(M.labels[task.status]||task.status):'未选择';
    $('edit').textContent=editing?'退出编辑':'编辑流程';$('edit').disabled=busy||!editable();
    $('edit').title=!task?'先选择任务':!editable()?'只有待开始的任务可由有编辑权限的用户修改':'';
    $('save').disabled=busy||!editing||!modified||!editable();$('structure-actions').hidden=!editing||!editable();
    $('undo').disabled=busy||!undo.length;$('redo').disabled=busy||!redo.length;
    $('add').disabled=busy;$('connect').disabled=busy||graph.nodes.length<2;
    $('task-select').disabled=busy||!ready;$('refresh').disabled=busy;$('reload').disabled=busy;
    $('discuss').disabled=busy||!task;
    $('inspector').querySelectorAll('input,textarea,button[data-edit]').forEach(el=>el.disabled=busy||!editing||!editable());
    const done=graph.nodes.filter(n=>n.status==='completed').length;
    $('summary').textContent=task?`${graph.nodes.length} 个步骤 · ${graph.edges.length} 条依赖 · ${done} 已完成`:'尚未加载流程';
    $('mode-hint').textContent=!task?'选择任务后开始':editing?'编辑草稿 · 保存后生效':task.status==='pending'?'查看模式 · 拖动仅调整当前视图':'任务已开始或结束 · 仅查看';
    notifyParent('dag-state',{dirty:modified,task_id:task?.task_id||''});
  }
  function emptyState(){const empty=$('empty');empty.hidden=graph.nodes.length>0;empty.querySelector('h2').textContent=task?'还没有任务步骤':'让每一步都清楚';empty.querySelector('p').textContent=task?(editable()?'点击“编辑流程”，添加第一个步骤。也可以与秘书讨论任务拆分。':'任务尚未生成流程，稍后刷新查看。'):scoped?'当前对话还没有关联任务。可先与秘书确认需求并创建任务。':'选择一个任务，查看步骤、负责人和依赖关系。';}
  function render(){canvas.set(graph,selected,$('search').value);header();emptyState();inspector();searchCount();}
  function select(value){selected=value;render();}
  function mutate(next,selection=selected){if(busy||!editing||!editable())return;undo.push(M.copy(graph));if(undo.length>60)undo.shift();redo=[];graph=next;selected=selection;report('');report('','error');render();}
  function patch(kind,index,field,value){
    if(busy||!editing||!editable())return;
    undo.push(M.copy(graph));if(undo.length>60)undo.shift();redo=[];
    graph[kind][index][field]=value;canvas.set(graph,selected,$('search').value);header();report('');
  }
  function history(direction){if(!editing||busy)return;const from=direction==='undo'?undo:redo,to=direction==='undo'?redo:undo;if(!from.length)return;to.push(M.copy(graph));graph=from.pop();selected=null;render();}
  function element(tag,text,className){const el=document.createElement(tag);if(text!==undefined)el.textContent=text;if(className)el.className=className;return el;}
  function field(container,label,value,change,multiline=false){const wrap=element('label',label),input=element(multiline?'textarea':'input');input.value=value||'';input.disabled=busy;input.addEventListener('input',()=>change(input.value));wrap.append(input);container.append(wrap);}
  function detail(container,label,value){const dl=element('dl'),dt=element('dt',label),dd=element('dd',value||'未填写');dl.append(dt,dd);container.append(dl);}
  function button(container,label,action,classes='button',edit=false){const b=element('button',label,classes);b.type='button';b.onclick=action;if(edit)b.dataset.edit='true';container.append(b);return b;}
  function dependencies(container,title,edges,key){
    container.append(element('h3',title,'section-title'));
    if(!edges.length){container.append(element('p',key==='source'?'无前置依赖，可独立开始。':'暂无后续步骤。'));return;}
    for(const e of edges){const n=graph.nodes.find(x=>x.id===e[key]);if(n)button(container,n.name,()=>{select({type:'node',id:n.id});canvas.focus(n.id);},'dependency');}
  }
  function inspector(){
    const panel=$('inspector');panel.replaceChildren();
    if(!selected){panel.innerHTML=intro;return;}
    if(selected.type==='edge'){edgeInspector(panel);return;}
    const i=graph.nodes.findIndex(n=>n.id===selected.id),n=graph.nodes[i];if(!n){selected=null;panel.innerHTML=intro;return;}
    panel.append(element('span',M.labels[n.status]||n.status||'待开始','eyebrow'),element('h2',n.name||'新步骤'));
    if(editing&&editable()){
      field(panel,'步骤名称',n.name,v=>patch('nodes',i,'name',v));
      field(panel,'步骤说明',n.description,v=>patch('nodes',i,'description',v),true);
      field(panel,'所需技能',n.skill,v=>patch('nodes',i,'skill',v));
    } else {detail(panel,'步骤说明',n.description);detail(panel,'所需技能',n.skill);}
    detail(panel,'负责人',n.assigned_agent_id||'尚未分配');
    dependencies(panel,'需要先完成',graph.edges.filter(e=>e.target===n.id),'source');
    dependencies(panel,'完成后继续',graph.edges.filter(e=>e.source===n.id),'target');
    button(panel,'带着这一步与秘书讨论',()=>discuss(n));
    if(editing&&editable())button(panel,'删除这个步骤',()=>{const count=graph.edges.filter(e=>e.source===n.id||e.target===n.id).length;if(!confirm(`删除“${n.name}”及相关的 ${count} 条依赖？保存前可以撤销。`))return;const next=M.copy(graph);next.nodes=next.nodes.filter(x=>x.id!==n.id);next.edges=next.edges.filter(e=>e.source!==n.id&&e.target!==n.id);mutate(next,null);},'button danger',true);
  }
  function edgeInspector(panel){
    const i=selected.index,e=graph.edges[i];if(!e){selected=null;panel.innerHTML=intro;return;}
    panel.append(element('span','DEPENDENCY','eyebrow'),element('h2','步骤依赖'));
    detail(panel,'前置步骤',graph.nodes.find(n=>n.id===e.source)?.name);detail(panel,'后续步骤',graph.nodes.find(n=>n.id===e.target)?.name);
    detail(panel,'继续条件',e.condition||'前置步骤完成');detail(panel,'依赖说明',e.description);
    if(editing&&editable())button(panel,'移除这条依赖',()=>{const next=M.copy(graph);next.edges.splice(i,1);mutate(next,null);},'button danger',true);
  }
  async function loadTask(id,force=false){
    if(!force&&!canLeave()){$('task-select').value=task?.task_id||'';return;}
    if(!id){task=null;graph=M.normalize();base=M.copy(graph);selected=null;editing=false;render();return;}
    const generation=++epoch;busy=true;report('');report('','error');header();
    try{
      const [nextTask,nextGraph]=await Promise.all([request('/api/tasks/'+encodeURIComponent(id)),getGraph(id)]);
      if(generation!==epoch)return;if(!allowed(nextTask))throw Error('这个任务不属于当前对话，请在对应对话中打开。');
      task=nextTask;graph=nextGraph;base=M.copy(graph);selected=null;undo=[];redo=[];editing=false;$('remote-notice').hidden=true;
      if(!tasks.some(t=>t.task_id===id)){tasks.push(task);taskOptions();}$('task-select').value=id;
      canvas.reset(graph);render();report(M.validate(graph),'error');
    }catch(e){if(generation===epoch){report('加载失败：'+e.message,'error');$('task-select').value=task?.task_id||'';}}
    finally{if(generation===epoch){busy=false;header();}}
  }
  function taskOptions(){const select=$('task-select');select.replaceChildren(new Option(scoped?'选择当前对话的任务…':'选择任务…',''));for(const t of tasks.filter(allowed))select.add(new Option(`${t.name||t.task_id} · ${M.labels[t.status]||t.status}`,t.task_id));select.value=task?.task_id||'';}
  async function initialize(id=''){
    const generation=++epoch;ready=false;busy=true;header();
    try{
      await mesh.ensure();scoped=Boolean(context.conv_id||context.project_id);threadIds=new Set();
      if(context.conv_id){const [conv,threads]=await Promise.all([request('/api/conversations'),request('/api/conversations/'+encodeURIComponent(context.conv_id)+'/pm-threads')]);if(generation!==epoch)return;
        const meta=conv.conversations?.find(c=>c.id===context.conv_id);if(!meta)throw Error('当前对话不存在，请重新选择。');context.project_id=meta.project_id||'';threadIds=new Set((threads.threads||[]).map(t=>t.task_id));}
      const data=await request('/api/tasks?limit=200');if(generation!==epoch)return;
      tasks=data.tasks||[];taskOptions();ready=true;$('scope-hint').textContent=scoped?'当前对话关联的任务 · 选择步骤即可带入讨论':'看清任务的先后关系，再和秘书一起调整。';
      busy=false;header();if(id)await loadTask(id,true);else if(tasks.filter(allowed).length===1)await loadTask(tasks.filter(allowed)[0].task_id,true);else render();
    }catch(e){if(generation===epoch){report('无法加载任务：'+e.message,'error');busy=false;header();}}
  }
  async function setContext(value){if(!canLeave())return false;context={conv_id:value.conv_id||'',project_id:value.project_id||''};task=null;graph=M.normalize();base=M.copy(graph);editing=false;selected=null;undo=[];redo=[];report('','error');$('remote-notice').hidden=true;render();await initialize(value.task_id||'');return true;}
  async function save(){
    if(busy||!dirty()||!editable())return;
    const error=M.validate(graph);if(error){report(error,'error');return;}
    const id=task.task_id, draft=M.copy(graph);busy=true;header();report('','error');
    try{
      const [current,latest]=await Promise.all([request('/api/tasks/'+encodeURIComponent(id)),getGraph(id)]);
      task=current;
      if(task.status!=='pending')throw Error('任务已经开始或结束，不能再修改流程。草稿仍保留。');
      if(M.signature(latest)!==M.signature(base)){$('remote-notice').hidden=false;throw Error('任务流程已被更新，本次没有覆盖。请核对后重新加载。');}
      const result=await request(`/api/tasks/${encodeURIComponent(id)}/graph`,{method:'PUT',body:JSON.stringify(draft)});
      if(!result.ok)throw Error(result.message||'保存未确认，请刷新后核对。');
      base=M.copy(draft);undo=[];redo=[];$('remote-notice').hidden=true;
      try{graph=await getGraph(id);base=M.copy(graph);report('修改已保存。任务仍保持待开始状态。');}
      catch{report('修改已保存，但暂时无法读取最新流程；请刷新后核对。');}
      notifyParent('dag-saved',{task_id:id});
    }catch(e){report('保存失败：'+e.message,'error');}finally{busy=false;render();}
  }
  async function refreshRemote(id){
    if(busy)return;
    if(!task){await initialize();return;}
    if(id&&id!==task.task_id)return;
    const currentId=task.task_id,generation=epoch;
    try{
      const [updated,latest]=await Promise.all([request('/api/tasks/'+encodeURIComponent(currentId)),getGraph(currentId)]);
      if(generation!==epoch||task?.task_id!==currentId)return;
      if(M.signature(latest)===M.signature(base)&&updated.status===task.status)return;
      task=updated;
      if(dirty()||editing){$('remote-notice').hidden=false;}
      else{graph=latest;base=M.copy(graph);}
      render();
    }catch{report('暂时无法更新流程，当前内容已保留。可点击刷新重试。');}
  }
  function discuss(node){
    if(!task)return;
    const data={task_id:task.task_id,task_name:task.name,project_id:task.project_id||'',node_id:node?.id||'',node_name:node?.name||'',draft:dirty()};
    if(window.parent!==window){notifyParent('dag-discuss',data);report(dirty()?'已把步骤带入对话草稿。当前图有未保存修改，请先保存后再让秘书调整。':'已把任务上下文带入对话草稿，补充问题后发送。');}
    else if(canLeave())location.href='/?'+new URLSearchParams({dag_task:task.task_id,dag_node:node?.id||''});
  }
  function searchCount(){const q=$('search').value.trim().toLowerCase();const count=graph.nodes.filter(n=>`${n.name} ${n.skill} ${n.description}`.toLowerCase().includes(q)).length;$('search-count').textContent=q?`${count} 项`:'';}
  function connectDialog(){const source=$('edge-source'),target=$('edge-target');source.replaceChildren();target.replaceChildren();for(const n of graph.nodes){source.add(new Option(n.name,n.id));target.add(new Option(n.name,n.id));}if(selected?.type==='node')source.value=selected.id;target.value=graph.nodes.find(n=>n.id!==source.value)?.id||'';$('connection-error').textContent='';$('connection-dialog').showModal();}
  $('connection-form').onsubmit=e=>{e.preventDefault();try{const next=M.connect(graph,$('edge-source').value,$('edge-target').value);mutate(next,{type:'edge',index:next.edges.length-1});$('connection-dialog').close();}catch(error){$('connection-error').textContent=error.message;}};
  $('connection-cancel').onclick=()=>$('connection-dialog').close();$('connect').onclick=connectDialog;
  $('task-select').onchange=()=>loadTask($('task-select').value);$('refresh').onclick=()=>task?loadTask(task.task_id):initialize();$('reload').onclick=()=>task&&loadTask(task.task_id);
  $('edit').onclick=()=>{if(editing){if(dirty()&&!canLeave())return;editing=false;}else editing=true;render();};
  $('save').onclick=save;$('undo').onclick=()=>history('undo');$('redo').onclick=()=>history('redo');
  $('add').onclick=()=>{const n={id:'st-'+crypto.randomUUID(),name:'新步骤',description:'',skill:'code_generation',status:'pending',input_data:{},output_data:{},assigned_agent_id:'',condition_expr:''};const next=M.copy(graph);next.nodes.push(n);mutate(next,{type:'node',id:n.id});canvas.focus(n.id);$('inspector').querySelector('input')?.focus();};
  $('layout').onclick=()=>canvas.arrange();$('fit').onclick=()=>canvas.fit();$('zoom-in').onclick=()=>canvas.zoom(1.2);$('zoom-out').onclick=()=>canvas.zoom(1/1.2);
  $('search').oninput=()=>{canvas.set(graph,selected,$('search').value);searchCount();};
  $('search').onkeydown=e=>{if(e.key!=='Enter')return;const q=$('search').value.toLowerCase(),n=graph.nodes.find(n=>`${n.name} ${n.skill} ${n.description}`.toLowerCase().includes(q));if(n){select({type:'node',id:n.id});canvas.focus(n.id);}};
  $('expand').onclick=()=>{if(window.parent!==window)notifyParent('dag-expand');else{document.body.classList.toggle('fullscreen');setExpanded(document.body.classList.contains('fullscreen'));}};
  function setExpanded(value){expanded=value;$('expand').textContent=value?'↙':'↗';$('expand').setAttribute('aria-label',value?'还原画布':'展开画布');requestAnimationFrame(()=>canvas.fit());}
  $('discuss').onclick=()=>discuss(selected?.type==='node'?graph.nodes.find(n=>n.id===selected.id):null);
  window.addEventListener('beforeunload',e=>{if(dirty()){e.preventDefault();e.returnValue='';}});
  window.addEventListener('keydown',e=>{if(e.key==='Escape'&&expanded&&!$('connection-dialog').open)$('expand').click();if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='s'){e.preventDefault();save();}if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='z'&&!/INPUT|TEXTAREA/.test(e.target.tagName)){e.preventDefault();history(e.shiftKey?'redo':'undo');}});
  window.addEventListener('message',e=>{if(e.origin!==location.origin||e.source!==window.parent)return;if(e.data?.type==='dag-expanded')setExpanded(e.data.expanded);if(e.data?.type==='dag-context')setContext(e.data);if(e.data?.type==='dag-refresh')refreshRemote(e.data.task_id);});
  window.DagWorkbench={canLeave,setContext,refresh:()=>task&&loadTask(task.task_id),isDirty:dirty};
  render();initialize(params.get('task_id')||'');
})();
