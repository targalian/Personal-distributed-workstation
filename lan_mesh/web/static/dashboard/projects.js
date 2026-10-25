// ── 项目面板 ──
async function refreshProjects(){
  try{const r=await apiFetch('/api/projects');const d=await r.json();allProjects=d.projects||[];renderProjects();updateProjectSelect();}catch(e){console.error(e);}
}
function renderProjects(){
  const grid=document.getElementById('project-grid');
  document.getElementById('proj-total').textContent=allProjects.length;
  document.getElementById('proj-active').textContent=allProjects.filter(p=>p.status==='active').length;
  if(!allProjects.length){grid.innerHTML='<div class="empty"><div class="icon">📁</div><p>暂无项目</p></div>';return;}
  grid.innerHTML=allProjects.map(p=>{
    const ratio=p.budget_limit_usd>0?Math.min(1,p.budget_used_usd/p.budget_limit_usd):0;
    const pct=Math.round(ratio*100);const c=ratio>=1?'high':ratio>=0.8?'mid':'low';
    const hasBlueprint=!!((p.charter&&Object.keys(p.charter).length)||(p.roadmap||[]).length||(p.decisions||[]).length);
    return `<div class="card ${p.status!=='active'?'offline':''}" onclick='showProjectDetail("${p.project_id}")'>
      <div class="card-head"><div class="card-title">📁 ${esc(p.name)}<span class="badge ${p.status}">${p.status}</span></div></div>
      <div class="card-meta">💰 $${(p.budget_used_usd||0).toFixed(4)} / $${(p.budget_limit_usd||0).toFixed(2)} (${pct}%)<br>🎯 ${esc(p.routing_strategy)} | 🤖 ${(p.allowed_models||[]).length}模型${hasBlueprint?' | 📘 蓝图':''}</div>
      ${p.budget_limit_usd>0?`<div class="pbar"><div class="pfill ${c}" style="width:${pct}%"></div></div>`:''}
    </div>`;
  }).join('');
}
function updateProjectSelect(){
  const sel=document.getElementById('task-project');
  sel.innerHTML='<option value="">不关联项目</option>';
  allProjects.filter(p=>p.status==='active').forEach(p=>{sel.innerHTML+=`<option value="${p.project_id}">${esc(p.name)}</option>`;});
  // 同步工作台项目选择器
  const wsSel=document.getElementById('ws-project-select');
  if(wsSel){
    wsSel.innerHTML='<option value="">🌐 全部任务</option>';
    allProjects.forEach(p=>{wsSel.innerHTML+=`<option value="${p.project_id}">${esc(p.status==='active'?'📁':'📦')} ${esc(p.name)}</option>`;});
    wsSel.value=wsSelectedProject;
  }
}
async function showProjectDetail(id){
  try{const r=await apiFetch(`/api/projects/${id}`);const p=await r.json();
    const ratio=p.budget_ratio||0;const pct=Math.round(ratio*100);
    let models=(p.allowed_models||[]).map(m=>`<code>${esc(m)}</code>`).join(' ')||'<span style="color:var(--dim)">全部允许</span>';
    document.getElementById('modal').innerHTML=`<span class="modal-close" onclick="closeOverlayForce()">&times;</span><h2>📁 ${esc(p.name)}</h2><p style="color:var(--dim);font-size:13px">${p.status} · ${esc(p.routing_strategy)} · ${fmtTime(p.created_at)}</p>
    <div class="modal-sec"><h3>项目蓝图</h3>${renderProjectBlueprint(p)}<button class="btn sm primary" style="margin-top:10px" onclick="showBlueprintWorkbench('${p.project_id}')">编辑蓝图</button></div>
    <div class="modal-sec"><h3>预算</h3><div class="kv-grid"><span class="k">预算上限</span><span class="v">$${(p.budget_limit_usd||0).toFixed(2)}</span><span class="k">已消费</span><span class="v">$${(p.budget_used_usd||0).toFixed(4)}</span><span class="k">使用率</span><span class="v">${pct}%</span><span class="k">剩余</span><span class="v">$${(p.budget_remaining||0).toFixed(4)}</span><span class="k">超支</span><span class="v">${p.is_over_budget?'是':'否'}</span><span class="k">经济模式</span><span class="v">${p.should_use_economy?'是':'否'}</span></div></div>
    <div class="modal-sec"><h3>配置</h3><div class="kv-grid"><span class="k">工作空间</span><span class="v">${esc(p.workspace_path||'-')}</span><span class="k">路由策略</span><span class="v">${esc(p.routing_strategy)}</span></div></div>
    <div class="modal-sec"><h3>允许模型</h3><div>${models}</div></div>
    <div class="modal-sec"><h3>最近调用 (${p.recent_calls||0})</h3><div class="kv-grid"><span class="k">输入tokens</span><span class="v">${p.recent_input_tokens||0}</span><span class="k">输出tokens</span><span class="v">${p.recent_output_tokens||0}</span></div>
    <button class="btn sm" style="margin-top:8px" onclick="showUsageLog('${p.project_id}')">查看消费记录</button></div>`;
    document.getElementById('overlay').classList.add('show');
  }catch(e){alert('失败:'+e.message);}
}
function _blueprintLines(value){
  return (Array.isArray(value)?value:[]).map(v=>`<div class="task-step"><span class="step-dot completed"></span><span>${esc(typeof v==='string'?v:JSON.stringify(v))}</span></div>`).join('');
}
function _blueprintRows(items,fieldNames){
  return (Array.isArray(items)?items:[]).map(item=>{
    if(typeof item==='string') return `<div class="task-step"><span class="step-dot completed"></span><span>${esc(item)}</span></div>`;
    const obj=item||{};
    const text=fieldNames.map(f=>obj[f]||'').filter(Boolean).join(' · ');
    return `<div class="task-step"><span class="step-dot completed"></span><span>${esc(text||JSON.stringify(obj))}</span></div>`;
  }).join('');
}
function renderProjectBlueprint(p){
  const charter=p.charter||{};
  const mission=charter.mission||'';
  const empty='<p style="color:var(--dim);font-size:13px">尚未定义蓝图</p>';
  const roadmap=_blueprintRows(p.roadmap,['phase','goal','status']);
  const decisions=_blueprintRows(p.decisions,['title','decision','rationale']);
  return `
    <div class="kv-grid"><span class="k">使命</span><span class="v">${mission?esc(mission):'-'}</span></div>
    <div style="margin-top:8px"><b style="font-size:12px;color:var(--dim)">目标</b>${_blueprintLines(charter.goals)||empty}</div>
    <div style="margin-top:8px"><b style="font-size:12px;color:var(--dim)">非目标</b>${_blueprintLines(charter.non_goals)||'<p style="color:var(--dim);font-size:13px">无</p>'}</div>
    <div style="margin-top:8px"><b style="font-size:12px;color:var(--dim)">验收标准</b>${_blueprintLines(charter.acceptance_criteria)||empty}</div>
    <div style="margin-top:8px"><b style="font-size:12px;color:var(--dim)">路线图</b>${roadmap||empty}</div>
    <div style="margin-top:8px"><b style="font-size:12px;color:var(--dim)">决策记录</b>${decisions||'<p style="color:var(--dim);font-size:13px">暂无</p>'}</div>`;
}
let _blueprintEditing={};
function _linesToText(value){return (Array.isArray(value)?value:[]).join('\n');}
function _rowsToText(items,fields){
  return (Array.isArray(items)?items:[]).map(item=>{
    if(typeof item==='string') return item;
    return fields.map(f=>(item||{})[f]||'').join(' | ');
  }).join('\n');
}
function _parseLines(text){return text.split(/\r?\n/).map(v=>v.trim()).filter(Boolean);}
function _parseRows(text,fields){
  return _parseLines(text).map(line=>{
    const parts=line.split('|').map(v=>v.trim());
    return fields.reduce((obj,f,i)=>(obj[f]=parts[i]||'',obj),{});
  });
}
async function showBlueprintWorkbench(pid){
  try{
    const r=await apiFetch(`/api/projects/${pid}/blueprint`);const p=await r.json();_blueprintEditing=p;
    const charter=p.charter||{};
    document.getElementById('modal').innerHTML=`<span class="modal-close" onclick="showProjectDetail('${pid}')">&times;</span><h2>📘 蓝图工作台</h2><p style="color:var(--dim);font-size:13px">${esc(p.name||'')}</p>
    <div class="modal-sec">
      <div class="form-row"><label>使命 / 一句话目标</label><textarea id="bp-mission">${esc(charter.mission||'')}</textarea></div>
      <div class="form-row"><label>目标 (每行一条)</label><textarea id="bp-goals">${esc(_linesToText(charter.goals))}</textarea></div>
      <div class="form-row"><label>非目标 (每行一条)</label><textarea id="bp-non-goals">${esc(_linesToText(charter.non_goals))}</textarea></div>
      <div class="form-row"><label>验收标准 (每行一条)</label><textarea id="bp-acceptance">${esc(_linesToText(charter.acceptance_criteria))}</textarea></div>
      <div class="form-row"><label>约束 (每行一条)</label><textarea id="bp-constraints">${esc(_linesToText(charter.constraints))}</textarea></div>
      <div class="form-row"><label>路线图 (阶段 | 目标 | 状态)</label><textarea id="bp-roadmap" placeholder="MVP | 完成登录闭环 | planned">${esc(_rowsToText(p.roadmap,['phase','goal','status']))}</textarea></div>
      <div class="form-row"><label>决策记录 (标题 | 结论 | 理由)</label><textarea id="bp-decisions" placeholder="技术栈 | FastAPI | 团队已有经验">${esc(_rowsToText(p.decisions,['title','decision','rationale']))}</textarea></div>
      <button class="btn primary" onclick="saveProjectBlueprint('${pid}')">保存蓝图</button>
    </div>`;
    document.getElementById('overlay').classList.add('show');
  }catch(e){alert('失败:'+e.message);}
}
async function saveProjectBlueprint(pid){
  const base=(_blueprintEditing.charter||{});
  const charter={...base,
    mission:document.getElementById('bp-mission').value.trim(),
    goals:_parseLines(document.getElementById('bp-goals').value),
    non_goals:_parseLines(document.getElementById('bp-non-goals').value),
    acceptance_criteria:_parseLines(document.getElementById('bp-acceptance').value),
    constraints:_parseLines(document.getElementById('bp-constraints').value)};
  const roadmap=_parseRows(document.getElementById('bp-roadmap').value,['phase','goal','status']);
  const decisions=_parseRows(document.getElementById('bp-decisions').value,['title','decision','rationale']);
  try{
    await apiFetch(`/api/projects/${pid}/blueprint`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({charter,roadmap,decisions})});
    await refreshProjects();await showProjectDetail(pid);
  }catch(e){alert('保存失败:'+e.message);}
}
async function showUsageLog(pid){
  try{const r=await apiFetch(`/api/projects/${pid}/usage`);const d=await r.json();
    let rows=(d.records||[]).map(r=>`<div class="task-step"><span class="step-dot completed"></span><span>${esc(r.model)} · ${r.input_tokens}+${r.output_tokens}tokens · $${(r.cost_usd||0).toFixed(6)} · ${fmtTime(r.timestamp)}</span></div>`).join('');
    document.getElementById('modal').insertAdjacentHTML('beforeend',`<div class="modal-sec"><h3>消费记录 (${d.total})</h3>${rows||'<p style="color:var(--dim)">无记录</p>'}</div>`);
  }catch(e){alert('失败:'+e.message);}
}
function showCreateProject(){
  document.getElementById('modal').innerHTML=`<span class="modal-close" onclick="closeOverlayForce()">&times;</span><h2>创建项目</h2>
  <div class="modal-sec">
    <div class="form-row"><label>项目名称</label><input id="proj-name" placeholder="my-blog-dev"></div>
    <div class="form-row"><label>描述</label><textarea id="proj-desc" placeholder="项目描述"></textarea></div>
    <div class="form-row"><label>月度预算 (USD)</label><input id="proj-budget" type="number" step="0.5" value="10" min="0"></div>
    <div class="form-row"><label>路由策略</label><select id="proj-strategy"><option value="balanced">balanced (均衡)</option><option value="cost_first">cost_first (优先省钱)</option><option value="quality_first">quality_first (优先质量)</option></select></div>
    <div class="form-row"><label>允许模型 (逗号分隔,空=全部)</label><input id="proj-models" placeholder="deepseek-chat,gpt-4o-mini"></div>
    <button class="btn primary" onclick="createProject()">创建</button>
  </div>`;
  document.getElementById('overlay').classList.add('show');
}
async function createProject(){
  const name=document.getElementById('proj-name').value.trim();if(!name){alert('请输入名称');return;}
  const desc=document.getElementById('proj-desc').value.trim();
  const budget=parseFloat(document.getElementById('proj-budget').value)||0;
  const strategy=document.getElementById('proj-strategy').value;
  const modelsText=document.getElementById('proj-models').value.trim();
  const allowed_models=modelsText?modelsText.split(',').map(s=>s.trim()):[];
  try{const r=await apiFetch('/api/projects',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name,description:desc,budget_limit_usd:budget,routing_strategy:strategy,allowed_models})});
    const d=await r.json();refreshProjects();closeOverlayForce();alert('项目已创建: '+d.name);}
  catch(e){alert('失败:'+e.message);}
}

