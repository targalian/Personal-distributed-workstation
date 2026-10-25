// ── PM Agent / 团队管理 ──
let allPMAgents=[];
async function refreshPMAgents(){
  try{
    const r=await apiFetch('/api/pm');const d=await r.json();
    allPMAgents=d.pm_agents||[];
    renderPMAgents();
  }catch(e){console.error('刷新 PM Agent 失败:',e);}
}
async function renderPMAgents(){
  const tree=document.getElementById('pm-tree');
  document.getElementById('pm-total').textContent=allPMAgents.length;
  if(!allPMAgents.length){tree.innerHTML='<div class="empty"><div class="icon">👥</div><p>暂无 PM Agent</p><p class="hint">提交任务后将自动分配 PM Agent</p></div>';document.getElementById('team-total').textContent='0';return;}
  let totalTeams=0;
  // 为每个 PM 获取团队和进度
  const pmDetails=await Promise.all(allPMAgents.map(async pm=>{
    try{
      const [teamsR,progressR]=await Promise.all([
        apiFetch(`/api/pm/${pm.pm_id}/teams`),
        apiFetch(`/api/pm/${pm.pm_id}/progress?limit=5`)
      ]);
      const teamsD=await teamsR.json();const progressD=await progressR.json();
      totalTeams+=(teamsD.teams||[]).length;
      return{pm,teams:teamsD.teams||[],progress:progressD.reports||[]};
    }catch(e){return{pm,teams:[],progress:[]};}
  }));
  document.getElementById('team-total').textContent=totalTeams;
  tree.innerHTML=pmDetails.map(({pm,teams,progress})=>{
    const statusClass=pm.status||'starting';
    const teamHtml=(teams||[]).map(t=>{
      const members=(t.members||[]).map(m=>{
        const st=m.status==='busy'?'busy':m.status==='idle'?'idle':'archived';
        return `<div class="member-row" title="点击查看 Agent 详情 (UI-067)" onclick="showAgentDetail('${escJs(m.agent_id||'')}','${escJs(m.agent_name||'')}')">👤 <span>${esc(m.agent_name||m.agent_id||'未知')}</span> [${esc(m.role||'worker')}] ${esc(m.status||'idle')}${m.current_task?' · 执行: '+esc(m.current_task):''}${m.progress?` (${Math.round(m.progress*100)}%)`:''}</div>`;
      }).join('');
      const collapsed=window._teamCollapsed&&window._teamCollapsed.has(t.team_id); // iter-101 UI-067: 折叠态跨刷新保持
      return `<div class="team-block${collapsed?' collapsed':''}"><div class="team-head" title="点击折叠/展开 (UI-067)" onclick="toggleTeamCollapse('${escJs(t.team_id)}')"><span style="font-size:11px;color:var(--dim)">${collapsed?'▶':'▼'}</span>👥 ${esc(t.team_name||t.team_id)} <span class="badge ${t.status==='active'?'idle':t.status==='pending'?'busy':'archived'}">${esc(t.status||'pending')}</span> <span style="font-size:12px;color:var(--dim)">类型: ${esc(t.team_type||'single')} · ${(t.members||[]).length} 成员</span></div><div class="team-members">${members||'<div class="member-row" style="cursor:default">无成员</div>'}</div></div>`;
    }).join('');
    const taskList=(pm.task_list||[]);
    const taskHtml=taskList.length?`<div style="font-size:12px;color:var(--dim);margin-top:6px">📋 任务列表: ${taskList.map(t=>esc(t.name||'')+`[${esc(t.status||'pending')}]`).join(', ')}</div>`:'';
    const progPct=progress.length?Math.round((progress[0].progress||0)*100):0;
    const progClass=progPct>=100?'':progPct>=50?'mid':'low';
    const progHtml=progress.length?`<div class="progress-bar"><div class="progress-fill ${progClass}" style="width:${progPct}%"></div></div><div style="font-size:12px;color:var(--dim);margin-top:2px">${esc(progress[0].message||'')} · ${progPct}%</div>`:'';
    return `<div class="pm-card ${statusClass}">
      <div class="pm-head"><div class="pm-name">🧑‍💼 ${esc(pm.agent_name||pm.pm_id)} <span class="badge ${statusClass==='executing'||statusClass==='monitoring'?'idle':statusClass==='planning'?'busy':'archived'}">${esc(statusClass)}</span></div></div>
      <div class="pm-meta">📍 Work Station: <span>${esc(pm.hostname||pm.device_id||'-')}</span> | 📡 <span>${esc(pm.ip||'-')}:${pm.api_port||'-'}</span><br>🤝 协作模式: <span>${esc(pm.collaboration_mode||'-')}</span> | 📋 任务: <span>${esc(pm.task_id||'-').slice(0,12)}</span></div>
      ${taskHtml}
      ${teamHtml||'<div style="font-size:12px;color:var(--dim);margin-top:6px">暂无团队 (PM 自行执行或正在规划中)</div>'}
      ${progHtml}
    </div>`;
  }).join('');
  // 渲染进度报告
  const progDiv=document.getElementById('progress-reports');
  const allReports=pmDetails.flatMap(d=>d.progress.map(r=>({...r,pm_name:d.pm.agent_name})));
  if(!allReports.length){progDiv.innerHTML='<p style="text-align:center;padding:20px">暂无进度报告</p>';return;}
  progDiv.innerHTML=allReports.slice(0,10).map(r=>{
    const pct=Math.round((r.progress||0)*100);
    const c=r.status==='completed'?'idle':r.status==='failed'?'archived':'busy';
    return `<div class="task-step"><span class="step-dot ${r.status==='completed'?'completed':r.status==='failed'?'failed':'assigned'}"></span><span>${esc(r.pm_name||r.pm_id)} → ${esc(r.reporter_id||'').slice(0,8)}: ${esc(r.task_name||'')} <span class="badge ${c}">${esc(r.status)} ${pct}%</span> ${esc(r.message||'').slice(0,80)}</span></div>`;
  }).join('');
}
// iter-101 (UI-067): 团队折叠 — 折叠态存 window._teamCollapsed, WS 刷新后保持
function toggleTeamCollapse(teamId){
  window._teamCollapsed=window._teamCollapsed||new Set();
  const s=window._teamCollapsed;
  if(s.has(teamId))s.delete(teamId);else s.add(teamId);
  const blk=document.querySelector(`#pm-tree .team-block .team-head[onclick*="${CSS.escape(teamId)}"]`);
  const block=blk?blk.closest('.team-block'):null;
  if(block){
    block.classList.toggle('collapsed',s.has(teamId));
    const ar=block.querySelector('.team-head span');
    if(ar)ar.textContent=s.has(teamId)?'▶':'▼';
  }
}
// iter-101 (UI-067): 成员点击 → Agent 详情弹窗 (GET /api/agents/{id}: 技能/状态/主机)
async function showAgentDetail(agentId,fallbackName){
  const ov=document.getElementById('overlay'),md=document.getElementById('modal');
  const tok=(window._agentModalSeq=(window._agentModalSeq||0)+1); // 异步回来后验证弹窗未被替换/关闭
  const live=()=>window._agentModalSeq===tok&&ov.classList.contains('show');
  md.innerHTML=`<span class="modal-close" onclick="closeOverlayForce()">&times;</span><h2>🤖 ${esc(fallbackName||agentId||'Agent')}</h2><div class="empty"><p>加载中...</p></div>`;
  ov.classList.add('show');
  if(!agentId){
    md.innerHTML=`<span class="modal-close" onclick="closeOverlayForce()">&times;</span><h2>🤖 ${esc(fallbackName||'Agent')}</h2><div class="empty"><p>该成员未关联已注册 Agent (无 agent_id)</p></div>`;
    return;
  }
  try{
    const r=await apiFetch(`/api/agents/${encodeURIComponent(agentId)}`);
    if(!live())return;
    if(!r.ok){
      md.innerHTML=`<span class="modal-close" onclick="closeOverlayForce()">&times;</span><h2>🤖 ${esc(fallbackName||agentId)}</h2><div class="empty"><p>${r.status===404?'Agent 未注册或已离线':'加载失败: '+r.status}</p><p class="hint" style="font-family:monospace">${esc(agentId)}</p></div>`;
      return;
    }
    const a=await r.json();
    if(!live())return;
    const stColor={online:'#4ade80',idle:'#4ade80',busy:'#ffa94d',offline:'#ff6b6b'}[a.status]||'var(--dim)';
    const rows=[
      ['Agent ID',`<span style="font-family:monospace;font-size:12px">${esc(a.agent_id||'')}</span>`],
      ['状态',`<span style="color:${stColor};font-weight:700">${esc(a.status||'—')}</span> · 并发 ${Number(a.current_task_count)||0}/${Number(a.max_concurrent_tasks)||0}`],
      ['所在主机',`${esc(a.hostname||'—')} <span style="color:var(--dim)">(${esc(a.ip||'-')}:${Number(a.api_port)||0} · ${esc(a.device_id||'-')})</span>`],
      ['技能',(a.skills||[]).length?(a.skills||[]).map(s=>`<span class="badge idle" style="margin:2px">${esc(typeof s==='string'?s:(s.name||JSON.stringify(s)))}</span>`).join(''):'<span style="color:var(--dim)">—</span>'],
      ['工具',(a.tools||[]).length?esc((a.tools||[]).map(s=>typeof s==='string'?s:(s.name||'')).filter(Boolean).join(', ')):'<span style="color:var(--dim)">—</span>'],
      ['模型偏好',esc(Object.keys(a.model_preferences||{}).length?JSON.stringify(a.model_preferences):'—')],
      ['版本 / 注册',`${esc(a.version||'—')} · ${fmtTime(a.registered_at)}`],
      ['最后心跳',fmtTime(a.last_seen)]];
    md.innerHTML=`<span class="modal-close" onclick="closeOverlayForce()">&times;</span><h2>🤖 ${esc(a.agent_name||a.agent_id)}</h2>`
      +`<table class="fleet-table"><tbody>`+rows.map(x=>`<tr><td style="color:var(--dim);width:110px;vertical-align:top">${x[0]}</td><td>${x[1]}</td></tr>`).join('')+`</tbody></table>`
      +`<div style="margin-top:18px;display:flex;justify-content:flex-end"><button class="btn sm" onclick="closeOverlayForce()">关闭</button></div>`;
  }catch(e){
    if(live())md.innerHTML=`<span class="modal-close" onclick="closeOverlayForce()">&times;</span><h2>🤖 Agent 详情</h2><div class="empty"><p>加载异常: ${esc(e.message)}</p></div>`;
  }
}

