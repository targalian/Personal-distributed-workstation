// ── 项目工作台 (多维合一) ──
function switchWsView(view){
  document.querySelectorAll('.ws-subtab').forEach(x=>x.classList.toggle('active',x.dataset.wsview===view));
  document.querySelectorAll('.ws-view').forEach(x=>x.classList.toggle('active',x.id==='ws-'+view));
  if(view==='dag')refreshDagTaskSelect();
  if(view==='team'){refreshPMAgents();refreshAgents();}
  if(view==='budget')refreshProjects();
  if(view==='overview')renderWsOverview();
}
function onWsProjectChange(){
  wsSelectedProject=document.getElementById('ws-project-select').value;
  renderTasks();
  renderWsOverview();
}
async function refreshWorkspace(){
  await Promise.all([refreshTasks(),refreshProjects(),refreshPMAgents(),refreshAgents()]);
  renderWsOverview();
}
function renderWsOverview(){
  const tasks=getFilteredTasks();
  const total=tasks.length;
  const completed=tasks.filter(t=>t.status==='completed').length;
  const running=tasks.filter(t=>['running','assigned'].includes(t.status)).length;
  const failed=tasks.filter(t=>t.status==='failed').length;
  const pending=tasks.filter(t=>t.status==='pending').length;
  // 空状态引导: 无任务且无项目时显示
  const onb=document.getElementById('ws-onboarding');
  if(onb)onb.style.display=(total===0&&allProjects.length===0)?'block':'none';
  // 统计卡片
  document.getElementById('ws-stat-cards').innerHTML=`
    <div class="stat-card"><div class="num">${total}</div><div class="lbl">总任务</div></div>
    <div class="stat-card online"><div class="num">${completed}</div><div class="lbl">已完成</div></div>
    <div class="stat-card" style=""><div class="num" style="color:var(--yellow)">${running}</div><div class="lbl">进行中</div></div>
    <div class="stat-card"><div class="num" style="color:var(--dim)">${pending}</div><div class="lbl">等待中</div></div>
    <div class="stat-card offline"><div class="num">${failed}</div><div class="lbl">失败</div></div>`;
  // 进度环 (基于子任务完成率)
  let allSubs=0,doneSubs=0;
  tasks.forEach(t=>{const s=t.subtasks||[];allSubs+=s.length;doneSubs+=s.filter(x=>x.status==='completed').length;});
  const pct=allSubs?Math.round(doneSubs/allSubs*100):(total?Math.round(completed/total*100):0);
  const circ=351.8;  // 2*PI*56
  document.getElementById('ws-ring-fill').style.strokeDashoffset=circ*(1-pct/100);
  document.getElementById('ws-ring-pct').textContent=pct+'%';
  // 项目标题与元信息
  const proj=wsSelectedProject?allProjects.find(p=>p.project_id===wsSelectedProject):null;
  document.getElementById('ws-proj-title').textContent=proj?('📁 '+proj.name):'🌐 全部任务';
  if(proj){
    const bPct=proj.budget_limit_usd>0?Math.round(proj.budget_used_usd/proj.budget_limit_usd*100):0;
    document.getElementById('ws-proj-meta').innerHTML=`💰 预算 $${(proj.budget_used_usd||0).toFixed(4)} / $${(proj.budget_limit_usd||0).toFixed(2)} (${bPct}%)<br>🎯 路由: ${esc(proj.routing_strategy)} · 状态: ${esc(proj.status)}<br>📋 任务 ${total} 个 · 完成 ${completed} 个`;
  }else{
    document.getElementById('ws-proj-meta').innerHTML=`📋 任务 ${total} 个 · 完成 ${completed} · 进行中 ${running}<br>📁 项目 ${allProjects.length} 个 · 🧑‍💼 PM Agent ${allPMAgents.length} 个`;
  }
  renderWsActivity();
}
function renderWsActivity(){
  const list=document.getElementById('ws-activity-list');
  // 从进度报告 + 任务状态构建动态流
  const items=[];
  (window._wsReports||[]).slice(0,8).forEach(r=>{
    const color=r.status==='completed'?'var(--green)':r.status==='failed'?'var(--red)':'var(--yellow)';
    items.push(`<div class="activity-item"><span class="activity-dot" style="background:${color}"></span><div style="flex:1"><b>${esc(r.task_name||'任务')}</b> · ${esc(r.message||'').slice(0,60)}<div style="color:var(--dim);font-size:12px">${esc(r.status)} ${Math.round((r.progress||0)*100)}% · ${fmtTime(r.timestamp)}</div></div></div>`);
  });
  getFilteredTasks().slice(0,5).forEach(t=>{
    const color=t.status==='completed'?'var(--green)':t.status==='failed'?'var(--red)':'var(--accent)';
    items.push(`<div class="activity-item"><span class="activity-dot" style="background:${color}"></span><div style="flex:1"><b>${esc(t.name||'未命名')}</b> · 状态 ${esc(t.status)}<div style="color:var(--dim);font-size:12px">创建于 ${fmtTime(t.created_at)}</div></div></div>`);
  });
  list.innerHTML=items.length?items.join(''):'<p style="text-align:center;color:var(--dim);padding:20px">暂无动态</p>';
}

