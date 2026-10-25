// ── Toast 通知 + 按钮反馈 ──
function showToast(msg,type='info'){
  const c=document.getElementById('toast-container');
  const t=document.createElement('div');
  t.className='toast '+type;
  t.textContent=msg;
  c.appendChild(t);
  requestAnimationFrame(()=>t.classList.add('show'));
  setTimeout(()=>{t.classList.remove('show');setTimeout(()=>t.remove(),300);},2500);
}
async function withFeedback(btn,fn){
  const orig=btn?btn.textContent:'';
  if(btn){btn.disabled=true;btn.textContent='⏳ '+orig.replace(/^[^\s]+\s*/,'');}
  try{await fn();if(btn)showToast('刷新完成','ok');}
  catch(e){showToast('操作失败: '+e.message,'err');console.error(e);}
  finally{if(btn){btn.disabled=false;btn.textContent=orig;}}
}

// ── 自定义输入对话框 (替代原生 prompt) ──
function showInputDialog(title,defaultVal=''){
  return new Promise(resolve=>{
    const ov=document.getElementById('overlay');
    const md=document.getElementById('modal');
    md.innerHTML=`<span class="modal-close" id="input-dlg-close">&times;</span><h2>${title}</h2>
<div style="margin-top:16px"><input id="input-dlg-field" class="input" style="width:100%;padding:10px 12px;border-radius:8px;border:1px solid var(--border);background:var(--bg);color:var(--text);font-size:14px" value="${defaultVal.replace(/"/g,'&quot;')}" /></div>
<div style="margin-top:16px;display:flex;gap:10px;justify-content:flex-end"><button class="btn" id="input-dlg-cancel">取消</button><button class="btn primary" id="input-dlg-ok">确定</button></div>`;
    ov.classList.add('show');
    const field=document.getElementById('input-dlg-field');
    field.focus();field.select();
    const cleanup=()=>{ov.classList.remove('show');field.removeEventListener('keydown',onKey);};
    const onKey=e=>{if(e.key==='Enter'){cleanup();resolve(field.value);}if(e.key==='Escape'){cleanup();resolve(null);}};
    field.addEventListener('keydown',onKey);
    document.getElementById('input-dlg-ok').onclick=()=>{cleanup();resolve(field.value);};
    document.getElementById('input-dlg-cancel').onclick=()=>{cleanup();resolve(null);};
    document.getElementById('input-dlg-close').onclick=()=>{cleanup();resolve(null);};
  });
}

let allHosts=[],allTasks=[],allAgents=[],allProjects=[];
let ws=null;
let secretaryActive=false;

// ── Secretary 激活/停用 ──
async function checkSecretaryState(){
  try{
    const r=await apiFetch('/api/station/roles');const d=await r.json();
    secretaryActive=(d.secretary==='active');
    updateSecretaryUI();
  }catch(e){console.error('查询 Secretary 状态失败:',e);}
}
function updateSecretaryUI(){
  const btn=document.getElementById('secretary-btn');
  const tabs=document.querySelectorAll('.secretary-tab');
  const mobileTabs=document.querySelectorAll('.mobile-nav-item.secretary-tab');
  const badge=document.getElementById('role-badge');
  if(secretaryActive){
    btn.textContent='停止秘书';btn.classList.add('danger');
    tabs.forEach(t=>t.style.display='');
    mobileTabs.forEach(t=>t.style.display='');
    if(badge){badge.textContent='Station + Secretary';badge.style.color='var(--green)';badge.style.borderColor='var(--green)';}
  }else{
    btn.textContent='启动秘书';btn.classList.remove('danger');
    tabs.forEach(t=>t.style.display='none');
    mobileTabs.forEach(t=>t.style.display='none');
    if(badge){badge.textContent='Station';badge.style.color='';badge.style.borderColor='';}
    // 如果当前在 Secretary tab, 切回 Station
    const activeTab=document.querySelector('.tab.active');
    if(activeTab&&activeTab.classList.contains('secretary-tab')){
      document.querySelectorAll('.tab').forEach(x=>x.classList.remove('active'));
      document.querySelectorAll('.panel').forEach(x=>x.classList.remove('active'));
      document.querySelectorAll('.mobile-nav-item').forEach(x=>x.classList.remove('active'));
      document.querySelector('[data-tab="station"]').classList.add('active');
      document.getElementById('panel-station').classList.add('active');
      refreshStation();
    }
  }
}
async function toggleSecretary(){
  const btn=document.getElementById('secretary-btn');
  if(secretaryActive){
    if(!confirm('确定要停用 Secretary 模式吗？\n停用后将无法使用任务/项目/Agent 功能。'))return;
    btn.disabled=true;btn.textContent='停用中...';
    try{const r=await apiFetch('/api/station/deactivate-secretary',{method:'POST'});const d=await r.json();
      secretaryActive=false;updateSecretaryUI();
      alert(d.message||'Secretary 已停用');
    }catch(e){alert('失败:'+e.message);}finally{btn.disabled=false;}
  }else{
    btn.disabled=true;btn.textContent='启动中...';
    try{const r=await apiFetch('/api/station/activate-secretary',{method:'POST'});const d=await r.json();
      if(d.ok){secretaryActive=true;updateSecretaryUI();alert('Secretary 已激活!\n对话/任务/团队/Agent/MCP工具 已就绪 (PM Agent 架构)');}
      else{alert('激活失败: '+(d.detail||d.message||'未知错误'));}
    }catch(e){alert('失败:'+e.message);}finally{btn.disabled=false;}
  }
}

// ── Tab切换（桌面端 + 移动端） ──
function switchTab(tab){
  document.querySelectorAll('.tab').forEach(x=>x.classList.remove('active'));
  document.querySelectorAll('.panel').forEach(x=>x.classList.remove('active'));
  document.querySelectorAll('.mobile-nav-item').forEach(x=>x.classList.remove('active'));
  document.querySelectorAll('[data-tab="'+tab+'"]').forEach(x=>x.classList.add('active'));
  document.getElementById('panel-'+tab).classList.add('active');
  if(tab==='hosts')refreshHosts();
  if(tab==='workspace')refreshWorkspace();
  if(tab==='tools')refreshTools();
  if(tab==='resources')refreshResources();
  if(tab==='station')refreshStation();
  if(tab==='skills')refreshSkills();
  if(tab==='bot')refreshBotChannels();
  if(tab==='p2p')refreshP2PHosts();
  if(tab==='chat')loadConversations();
  if(tab==='runtime')refreshRuntime();
  if(tab==='shadowdev')refreshShadowDev();
}
document.querySelectorAll('.tab').forEach(t=>{
  t.addEventListener('click',()=>switchTab(t.dataset.tab));
});
document.querySelectorAll('.mobile-nav-item').forEach(t=>{
  t.addEventListener('click',()=>switchTab(t.dataset.tab));
});

// ── WebSocket ──
function connectWS(){
  const proto=location.protocol==='https:'?'wss':'ws';
  ws=new WebSocket(`${proto}://${location.host}/ws`);
  ws.onopen=()=>{document.getElementById('ws-dot').classList.remove('offline');document.getElementById('ws-label').textContent='已连接';refreshAllState();};
  ws.onmessage=(e)=>{
    const msg=JSON.parse(e.data);
    if(msg.type==='hosts'||msg.type==='heartbeat'||msg.type==='host_registered')refreshHosts();
    if(msg.type==='task_submitted'||msg.type==='task_updated'||msg.type==='task_deleted')refreshTasks();
    if(msg.type==='task_delivered'){const r=msg.data.acceptance_review||{};showToast(`📦 任务「${msg.data.task_name||''}」已交付，等待验收${r.verdict==='risk'?` · ⚠️ 验收自检 ${r.reviewed-(r.unmet||[]).length}/${r.reviewed}`:''}`,'ok');refreshTasks();}
    if(msg.type==='task_cancelled'){showToast(`🚫 任务已取消: ${msg.data.message||''}`,'info');refreshTasks();refreshPMAgents();}
    if(msg.type==='task_paused'){showToast(`⏸️ 任务已暂停: ${msg.data.message||''}`,'info');refreshTasks();}
    if(msg.type==='agent_registered')refreshAgents();
    if(msg.type==='project_created'||msg.type==='project_updated'||msg.type==='project_archived')refreshProjects();
    if(msg.type==='secretary_activated'){secretaryActive=true;updateSecretaryUI();document.getElementById('role-badge').textContent='Station + Secretary';document.getElementById('role-badge').style.color='var(--green)';document.getElementById('role-badge').style.borderColor='var(--green)';}
    if(msg.type==='secretary_deactivated'){secretaryActive=false;updateSecretaryUI();document.getElementById('role-badge').textContent='Station';document.getElementById('role-badge').style.color='';document.getElementById('role-badge').style.borderColor='';}
    if(msg.type==='secretary_yielded'){
      secretaryActive=false;updateSecretaryUI();
      const peer=msg.data&&msg.data.peer?` (已由 ${msg.data.peer} 接管)`:'';
      showToast('Secretary 冲突仲裁: 本站已让位'+peer,'err');
    }
    if(msg.type==='secretary_assigned'||msg.type==='secretary_revoked')refreshStation();
    if(msg.type==='skill_assigned'||msg.type==='skill_revoked'||msg.type==='skills_scanned')refreshSkills();
    // iter-61: 安装/卸载 Toast 由本地操作函数弹出, WS 事件仅静默刷新 (防双重 Toast)
    if(msg.type==='skill_installed'||msg.type==='skill_uninstalled')refreshSkills();
    // PM Agent 架构: 新消息类型
    // iter-74: 讨论回复 (action_taken=opt_discuss) 不进主聊天流 — 讨论区由 HTTP 响应
    // 渲染 (WS data 不含 topic 无法归属话题), 避免串台与「HTTP+WS」双渲染
    if(msg.type==='chat_reply'){
      if(!(msg.data&&msg.data.action_taken==='opt_discuss')){
        appendChatMessage('assistant',msg.data.reply,msg.data.timestamp);
      }
    }
    if(msg.type==='pm_registered'){refreshPMAgents();refreshTasks();loadPMThreads();}
    if(msg.type==='pm_status_change'){
      refreshPMAgents();onPMThreadStatusChange(msg.data);
      const st=msg.data.status||'';
      if(st==='failed'){showToast(`❌ PM Agent 执行失败: ${msg.data.message||msg.data.pm_id||''}`,'err');refreshTasks();}
      if(st==='cancelled'){showToast(`🚫 PM 任务已取消`,'info');refreshTasks();}
      if(st==='awaiting_input'){showToast(`❓ PM 请求决策: ${msg.data.clarification_question||'请查看 PM 线程'}`,'info');}
    }
    if(msg.type==='team_update'){refreshPMAgents();}
    // iter-101 (UI-069): 云同步结果广播 (手动/定时/他机触发) — 静默刷新状态卡, 不打扰 toast
    if(msg.type==='cloud_sync'){const cs=document.getElementById('cloud-sync-card');if(cs&&cs.isConnected)loadCloudSync();}
    if(msg.type==='pm_thread_attached'){onPMThreadAttached(msg.data);}
    if(msg.type==='pm_thread_message'){/* 线程消息已通过 API 响应处理 */}
    if(msg.type==='progress_report'){
      window._wsReports=window._wsReports||[];
      window._wsReports.unshift(msg.data);
      window._wsReports=window._wsReports.slice(0,20);
      refreshPMAgents();renderWsActivity();
    }
    if(msg.type==='task_updated'||msg.type==='task_deleted'){renderWsOverview();}
    if(msg.type==='p2p_chat'){onP2PChatMessage(msg.data);}
    if(msg.type==='event'){onStationEvent(msg.data||{});}
  };
  ws.onclose=()=>{document.getElementById('ws-dot').classList.add('offline');document.getElementById('ws-label').textContent='重连中...';setTimeout(connectWS,3000);};
  ws.onerror=()=>{};
}
function refreshAllState(){
  refreshHosts();refreshTasks();refreshAgents();refreshProjects();
  checkSecretaryState();refreshPMAgents();loadPMThreads();renderWsOverview();
  loadVersionBadge();
  loadOptData(); // 工作站优化队列 (后端未实现时自动回退本地 mock)
  // iter-91 F7: WS 重连后按当前停留 Tab 补刷懒加载面板 (runtime/shadowdev)
  const act=document.querySelector('.panel.active');
  if(act&&act.id==='panel-runtime')refreshRuntime();
  if(act&&act.id==='panel-shadowdev')refreshShadowDev();
}
function onStationEvent(evt){
  // M5: 事件总线分发 — 资源面板实时刷新 (替代轮询/手动刷新)
  const t=evt.type||'';
  if(t==='usage_reported'||t==='resource_config'||t==='resource_alert'){
    refreshResources();
    if(t==='resource_alert'){
      const lv=(evt.data&&evt.data.max_level)||0;
      showToast(`🔔 收到 ${evt.data.count||0} 条资源预警`,lv>=3?'err':'info');
    }
    if(t==='resource_config'){showToast('⚙️ 资源配置已热重载','ok');}
  }
  if(t==='version_upgrade_notice'){showUpgradeBanner(evt.data||{});}
  // 工作站优化实时事件 (后端 WS 推送或本地 mock 同构分发)
  if(t==='workstation_optimization_created'||t==='workstation_optimization_updated'||t==='workstation_optimization_waiting_boss'||t==='workstation_optimization_completed'){
    handleOptEvent({type:t,data:evt.data||{}});
  }
  if(t==='task_stall_alert'){
    // iter-41: 任务停滞主动告警 — toast + 总览表实时刷新 (无需手动切 Tab)
    const lv=(evt.data&&evt.data.max_level)||0;
    const ids=(evt.data&&evt.data.tasks)||[];
    // iter-91 F4: 手动巡检后 4s 窗口内抑制重复 toast (按钮已给反馈), 列表照常刷新
    if(!(window._stallManualTs&&Date.now()-window._stallManualTs<4000)){
      showToast(`⚠️ ${evt.data.count||0} 个任务可能停滞${ids.length?': '+ids.join(', '):''}`,lv>=3?'err':'info');
    }
    if(typeof loadTaskFlowList==='function'){loadTaskFlowList();}
    if(typeof loadStallAlerts==='function'){loadStallAlerts();}
  }
  if(t==='error_captured'){
    // iter-44: 新错误捕获 — 面板实时刷新 (仅当前在运行时 Tab 时避免无谓请求)
    // iter-91 F3: 守卫修正 — #error-panel 系静态 HTML 恒存在, 原判据永真失效;
    // 改为仅当前停留在运行时 Tab 时才刷新, 与注释意图一致
    if(typeof loadErrors==='function'){const pr=document.getElementById('panel-runtime');if(pr&&pr.classList.contains('active'))loadErrors();}
  }
  if(t==='error_burst'){
    // iter-44: 错误突发告警 — toast 提醒 + 面板刷新
    const d=evt.data||{};
    showToast(`❗ 错误突发 [${d.module||'?'}] ${d.count||0} 条/窗口, 请检查日志`,'err');
    if(typeof loadErrors==='function'){loadErrors();}
  }
  if(t==='shadow_run_update'){
    // iter-98 F6: 影子运行状态实时联动 — 仅停留在影子 Tab 时才拉列表 (避免无谓请求);
    // 终态额外 toast, 排队/执行中不打扰 (一次运行会连发 queued→running→终态)
    const d=evt.data||{};
    const st=d.status||'';
    const ps=document.getElementById('panel-shadowdev');
    if(ps&&ps.classList.contains('active')&&typeof refreshShadowDev==='function')refreshShadowDev();
    if(st&&st!=='queued'&&st!=='running'){
      const rid=(d.run_id||'').slice(0,15);
      const ok=st==='READY_FOR_REVIEW';
      showToast(`🕶️ 影子运行 ${rid} ${ok?'门禁全过, 待审':'结束: '+st}`,ok?'ok':'err');
    }
  }
  if(t==='cost_budget_warning'){
    // iter-52: 成本感知调度预算适配告警 — toast + 任务面板刷新
    const cd=evt.data||{};
    showToast(`💰 预算适配告警: 「${cd.name||'?'}」预估 ${fmtTokens(cd.estimated_tokens||0)} tokens (${cd.status||''})`,'info');
    if(typeof refreshTasks==='function'){refreshTasks();}
  }
}
// S2: 版本升级提醒横幅 + 顶栏版本徽章
async function loadVersionBadge(){
  try{
    const r=await apiFetch('/api/version');
    if(!r.ok)return;
    const v=await r.json();
    const el=document.getElementById('version-badge');
    if(!el)return;
    const label=v.version?`${v.version} · ${v.commit}`:`${v.commit||'未知版本'}`;
    el.textContent='🏷️ '+label;
    el.title=(v.note||'本机代码版本')+(v.released_at?` (${v.released_at})`:'');
  }catch(e){}
}
function showUpgradeBanner(d){
  const banner=document.getElementById('upgrade-banner');
  const txt=document.getElementById('upgrade-banner-text');
  if(!banner||!txt)return;
  const who=d.from_name||'其他主机';
  txt.innerHTML=`<b>${who}</b> 版本领先 <code style="font-size:12px">${d.commit||''}</code>${d.note?` — ${d.note}`:''}, 建议本机尽快升级`;
  banner.style.display='flex';
  showToast(`⬆️ 收到升级提醒: ${who} 版本领先`,'info');
}

