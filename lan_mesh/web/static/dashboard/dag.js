// ── DAG 图编辑器 ──────────────────────────────────────────────
let dagNodes=[];let dagEdges=[];let dagTaskId='';let dagSelected=null;
let dagConnectMode=false;let dagConnectSource=null;let dagDragging=null;let dagDragOffset={x:0,y:0};
const dagSvg=()=>document.getElementById('dag-svg');
const dagNodesG=()=>document.getElementById('dag-nodes');
const dagEdgesG=()=>document.getElementById('dag-edges');
const NODE_W=160,NODE_H=50;

function dagStatusColor(s){
  if(s==='completed')return'var(--green)';
  if(s==='running'||s==='assigned')return'var(--accent)';
  if(s==='failed')return'var(--red)';
  return'var(--yellow)';
}

async function loadDAGFromSelect(){
  const sel=document.getElementById('dag-task-select');
  const tid=sel.value;
  if(!tid){showToast('请先选择任务','err');return;}
  dagTaskId=tid;
  try{
    const r=await apiFetch(`/api/tasks/${tid}/graph`);
    if(!r.ok){const e=await r.json();showToast(e.detail||'加载失败','err');return;}
    const data=await r.json();
    dagNodes=data.nodes||[];dagEdges=data.edges||[];
    renderDAG();
    document.getElementById('dag-status').textContent=`已加载: ${dagNodes.length} 节点 / ${dagEdges.length} 边`;
    showToast('DAG 图已加载','ok');
  }catch(e){showToast('加载失败: '+e.message,'err');}
}

function renderDAG(){
  const ng=dagNodesG();const eg=dagEdgesG();
  ng.innerHTML='';eg.innerHTML='';
  // 渲染边
  dagEdges.forEach((e,i)=>{
    const src=dagNodes.find(n=>n.id===e.source);
    const tgt=dagNodes.find(n=>n.id===e.target);
    if(!src||!tgt)return;
    const x1=(src.x||0)+NODE_W/2,y1=(src.y||0)+NODE_H;
    const x2=(tgt.x||0)+NODE_W/2,y2=(tgt.y||0);
    const line=document.createElementNS('http://www.w3.org/2000/svg','line');
    line.setAttribute('x1',x1);line.setAttribute('y1',y1);
    line.setAttribute('x2',x2);line.setAttribute('y2',y2);
    line.setAttribute('stroke',e.condition?'var(--yellow)':'var(--dim)');
    line.setAttribute('stroke-width','2');
    if(e.condition)line.setAttribute('stroke-dasharray','6,4');
    line.setAttribute('marker-end',e.condition?'url(#arrowhead-cond)':'url(#arrowhead)');
    eg.appendChild(line);
    if(e.condition){
      const txt=document.createElementNS('http://www.w3.org/2000/svg','text');
      txt.setAttribute('x',(x1+x2)/2);txt.setAttribute('y',(y1+y2)/2-6);
      txt.setAttribute('fill','var(--yellow)');txt.setAttribute('font-size','10');
      txt.setAttribute('text-anchor','middle');
      txt.textContent=e.condition.length>20?e.condition.slice(0,20)+'...':e.condition;
      eg.appendChild(txt);
    }
  });
  // 渲染节点
  dagNodes.forEach((n,i)=>{
    const g=document.createElementNS('http://www.w3.org/2000/svg','g');
    g.setAttribute('transform',`translate(${n.x||0},${n.y||0})`);
    g.style.cursor='grab';
    g.dataset.idx=i;
    const rect=document.createElementNS('http://www.w3.org/2000/svg','rect');
    rect.setAttribute('width',NODE_W);rect.setAttribute('height',NODE_H);
    rect.setAttribute('rx','10');rect.setAttribute('ry','10');
    rect.setAttribute('fill','var(--surface2)');
    rect.setAttribute('stroke',dagSelected===n.id?'#fff':dagStatusColor(n.status));
    rect.setAttribute('stroke-width',dagSelected===n.id?'3':'2');
    g.appendChild(rect);
    const txt=document.createElementNS('http://www.w3.org/2000/svg','text');
    txt.setAttribute('x',NODE_W/2);txt.setAttribute('y',20);
    txt.setAttribute('text-anchor','middle');txt.setAttribute('fill','var(--text)');
    txt.setAttribute('font-size','12');txt.setAttribute('font-weight','600');
    txt.textContent=(n.name||'unnamed').length>14?(n.name||'').slice(0,14)+'...':(n.name||'');
    g.appendChild(txt);
    const sub=document.createElementNS('http://www.w3.org/2000/svg','text');
    sub.setAttribute('x',NODE_W/2);sub.setAttribute('y',38);
    sub.setAttribute('text-anchor','middle');sub.setAttribute('fill','var(--dim)');
    sub.setAttribute('font-size','10');
    sub.textContent=`${n.skill||''} [${n.status||'pending'}]`;
    g.appendChild(sub);
    // 事件
    g.addEventListener('mousedown',(ev)=>dagNodeMouseDown(ev,i));
    g.addEventListener('click',(ev)=>dagNodeClick(ev,i));
    ng.appendChild(g);
  });
}

function dagNodeMouseDown(ev,idx){
  if(dagConnectMode)return;
  ev.preventDefault();
  dagDragging=idx;
  const svg=dagSvg();const rect=svg.getBoundingClientRect();
  dagDragOffset={x:ev.clientX-rect.left-(dagNodes[idx].x||0),y:ev.clientY-rect.top-(dagNodes[idx].y||0)};
}

function dagNodeClick(ev,idx){
  ev.stopPropagation();
  const n=dagNodes[idx];
  if(dagConnectMode){
    if(!dagConnectSource){dagConnectSource=n.id;showToast(`连线: 源=${n.name}`,'info');}
    else{
      if(dagConnectSource!==n.id){
        dagEdges.push({source:dagConnectSource,target:n.id,condition:'',description:''});
        renderDAG();showToast('连线已添加','ok');
      }
      dagConnectSource=null;
    }
    return;
  }
  dagSelected=n.id;
  renderDAG();
  // 显示详情
  const det=document.getElementById('dag-node-detail');
  det.style.display='block';
  document.getElementById('dag-detail-content').innerHTML=`
    <div style="margin-bottom:6px"><b>名称:</b> ${n.name||'-'}</div>
    <div style="margin-bottom:6px"><b>技能:</b> ${n.skill||'-'}</div>
    <div style="margin-bottom:6px"><b>状态:</b> <span style="color:${dagStatusColor(n.status)}">${n.status||'pending'}</span></div>
    <div style="margin-bottom:6px"><b>描述:</b> ${n.description||'-'}</div>
    <div style="margin-bottom:6px"><b>Agent:</b> ${n.assigned_agent_id||'未分配'}</div>
    <div><b>条件:</b> ${n.condition_expr||'无'}</div>`;
}

document.addEventListener('mousemove',(ev)=>{
  if(dagDragging===null)return;
  const svg=dagSvg();const rect=svg.getBoundingClientRect();
  dagNodes[dagDragging].x=Math.max(0,ev.clientX-rect.left-dagDragOffset.x);
  dagNodes[dagDragging].y=Math.max(0,ev.clientY-rect.top-dagDragOffset.y);
  renderDAG();
});
document.addEventListener('mouseup',()=>{dagDragging=null;});

function dagToggleConnect(){
  dagConnectMode=!dagConnectMode;dagConnectSource=null;
  const btn=document.getElementById('dag-connect-btn');
  btn.style.background=dagConnectMode?'var(--accent)':'';
  btn.style.color=dagConnectMode?'#fff':'';
  showToast(dagConnectMode?'连线模式: 依次点击源节点和目标节点':'连线模式已关闭','info');
}

async function dagAddNode(){
  const name=await showInputDialog('节点名称:','新子任务');
  if(!name)return;
  const skill=await showInputDialog('技能 (code_generation/code_review/shell_exec/...):','code_generation')||'code_generation';
  const id='st-'+Date.now().toString(36)+'-'+Math.random().toString(36).slice(2,6);
  dagNodes.push({id,name,status:'pending',skill,description:'',input_data:{},output_data:{},assigned_agent_id:'',condition_expr:'',x:100+Math.random()*200,y:100+Math.random()*200});
  renderDAG();
  document.getElementById('dag-status').textContent=`${dagNodes.length} 节点 / ${dagEdges.length} 边`;
}

function dagDeleteSelected(){
  if(!dagSelected){showToast('请先点击选中一个节点','err');return;}
  dagNodes=dagNodes.filter(n=>n.id!==dagSelected);
  dagEdges=dagEdges.filter(e=>e.source!==dagSelected&&e.target!==dagSelected);
  dagSelected=null;
  document.getElementById('dag-node-detail').style.display='none';
  renderDAG();
  document.getElementById('dag-status').textContent=`${dagNodes.length} 节点 / ${dagEdges.length} 边`;
  showToast('节点已删除','ok');
}

async function saveDAG(){
  // iter-51 (F4.3): 手工/自然语言编辑图保存端点已恢复,
  // 仅 pending 任务可保存 (后端校验环/状态)
  if(!dagTaskId){showToast('请先选择任务','err');return;}
  const nodes=dagNodes.map(n=>({
    id:n.id,name:n.name||'未命名',status:n.status||'pending',
    skill:n.skill||'',description:n.description||'',x:n.x||0,y:n.y||0,
  }));
  const edges=dagEdges.map(e=>({source:e.source,target:e.target,condition:e.condition||'',description:e.description||''}));
  try{
    const r=await apiFetch(`/api/tasks/${dagTaskId}/graph`,{
      method:'PUT',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({nodes,edges}),
    });
    const d=await r.json();
    if(r.ok&&d.ok){showToast('✅ '+(d.message||'已保存'));}
    else{showToast('保存失败: '+(d.detail||d.message||'未知错误'),'err');}
  }catch(e){showToast('保存失败: '+e.message,'err');}
}

async function refreshDagTaskSelect(){
  try{
    const r=await apiFetch('/api/tasks?limit=50');
    const d=await r.json();
    const sel=document.getElementById('dag-task-select');
    sel.innerHTML='<option value="">选择任务...</option>';
    (d.tasks||[]).forEach(t=>{
      const opt=document.createElement('option');
      opt.value=t.task_id;opt.textContent=`${t.name||t.task_id.slice(0,8)} [${t.status}]`;
      sel.appendChild(opt);
    });
  }catch(e){}
}

