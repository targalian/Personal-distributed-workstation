// ── Agent面板 ──
async function refreshAgents(){
  try{const r=await apiFetch('/api/agents');const d=await r.json();allAgents=d.agents||[];renderAgents();}catch(e){console.error(e);}
}
function renderAgents(){
  const grid=document.getElementById('agent-grid');
  document.getElementById('agent-total').textContent=allAgents.length;
  document.getElementById('agent-idle').textContent=allAgents.filter(a=>a.status==='idle').length;
  document.getElementById('agent-busy').textContent=allAgents.filter(a=>a.status==='busy').length;
  if(!allAgents.length){grid.innerHTML='<div class="empty"><div class="icon">🤖</div><p>暂无Agent</p></div>';return;}
  grid.innerHTML=allAgents.map(a=>{
    const sc=(a.skills||[]).length,tc=(a.tools||[]).length;
    return `<div class="card" onclick='showAgentDetail("${a.agent_id}")'>
      <div class="card-head"><div class="card-title">🤖 ${esc(a.agent_name||'未知')}<span class="badge ${a.status==='idle'?'idle':'busy'}">${a.status}</span></div></div>
      <div class="card-meta">📡 <span>${esc(a.ip||'-')}</span>:${a.api_port||'-'} | 🖥️ <span>${esc(a.hostname||'-')}</span><br>🎯 ${sc}技能 | 🔧 ${tc}工具 | ⚡ ${a.current_task_count||0}/${a.max_concurrent_tasks}并发</div>
    </div>`;
  }).join('');
}
function showAgentDetail(id){
  const a=allAgents.find(x=>x.agent_id===id);if(!a)return;
  const skills=(a.skills||[]).map(s=>`<span class="badge worker">${esc(s.name)}</span> `).join('')||'<span style="color:var(--dim)">无</span>';
  const tools=(a.tools||[]).map(t=>`<span class="badge idle">${esc(t.name)}</span> `).join('')||'<span style="color:var(--dim)">无</span>';
  const models=(a.model_preferences||[]).map(m=>`<code>${esc(m)}</code>`).join(' ')||'<span style="color:var(--dim)">无</span>';
  document.getElementById('modal').innerHTML=`<span class="modal-close" onclick="closeOverlayForce()">&times;</span><h2>🤖 ${esc(a.agent_name)}</h2><p style="color:var(--dim);font-size:13px">${a.status} · v${a.version}</p>
  <div class="modal-sec"><h3>宿主信息</h3><div class="kv-grid"><span class="k">AgentID</span><span class="v">${esc(a.agent_id)}</span><span class="k">设备ID</span><span class="v">${esc(a.device_id)}</span><span class="k">主机名</span><span class="v">${esc(a.hostname||'-')}</span><span class="k">IP:端口</span><span class="v">${esc(a.ip||'-')}:${a.api_port||'-'}</span><span class="k">并发</span><span class="v">${a.current_task_count||0}/${a.max_concurrent_tasks}</span><span class="k">注册时间</span><span class="v">${fmtTime(a.registered_at)}</span></div></div>
  <div class="modal-sec"><h3>技能 (${(a.skills||[]).length})</h3><div style="display:flex;flex-wrap:wrap;gap:4px">${skills}</div></div>
  <div class="modal-sec"><h3>工具 (${(a.tools||[]).length})</h3><div style="display:flex;flex-wrap:wrap;gap:4px">${tools}</div></div>
  <div class="modal-sec"><h3>模型偏好</h3><div>${models}</div></div>`;
  document.getElementById('overlay').classList.add('show');
}

// ── MCP工具面板 ──
async function refreshTools(){
  try{const r=await apiFetch('/tools/list');const d=await r.json();renderTools(d);renderServers(d.servers||[]);}catch(e){console.error(e);}
}

