// ── 主机面板 ──
async function refreshHosts(){
  try{
    const r=await apiFetch('/api/hosts');const d=await r.json();allHosts=d.hosts||[];renderHosts();
  }catch(e){console.error('刷新主机失败:',e);}
}
function renderHosts(){
  const grid=document.getElementById('host-grid');
  const s=document.getElementById('host-search').value.toLowerCase();
  const hosts=allHosts.filter(h=>!s||(h.device_name||'').toLowerCase().includes(s)||(h.ip||'').toLowerCase().includes(s)||(h.platform||'').toLowerCase().includes(s));
  document.getElementById('stat-total').textContent=allHosts.length;
  document.getElementById('stat-online').textContent=allHosts.filter(h=>h.online).length;
  document.getElementById('stat-offline').textContent=allHosts.filter(h=>!h.online).length;
  // S3: 跨主机版本统计 — 版本分布汇总 (一致绿色, 不一致告警色)
  const vMap={};allHosts.forEach(h=>{const v=h.code_version||'未知';vMap[v]=(vMap[v]||0)+1;});
  const vTxt=Object.entries(vMap).map(([v,c])=>`${v}×${c}`).join(' · ')||'-';
  const vEl=document.getElementById('stat-versions');
  vEl.textContent=vTxt;
  vEl.style.color=Object.keys(vMap).length>1?'var(--amber,#f0ad4e)':'var(--green)';
  const vMaxTs=Math.max(0,...allHosts.map(h=>h.version_ts||0));
  if(!hosts.length){grid.innerHTML='<div class="empty"><div class="icon">🖥️</div><p>暂无 Work Station</p><p class="hint">请在各主机上运行 start_workstation 或 python main.py worker</p></div>';return;}
  grid.innerHTML=hosts.map(h=>{
    const rc=h.role==='secretary'?'secretary':h.role==='station'?'station':'worker';
    const rl=h.role==='secretary'?'SECRETARY':h.role==='station'?'STATION':'WORKER';
    const vMark=h.code_version?((h.version_ts||0)>=vMaxTs&&vMaxTs?'✅最新':(vMaxTs?'⚠️落后':'')):'未知版本';
    return `<div class="card ${h.online?'':'offline'} ${rc}" onclick='showHostDetail("${h.device_id}")'>
      <div class="card-head"><div class="card-title">${esc(h.device_name||h.hostname||'未知')}<span class="badge ${rc}">${rl}</span></div><span class="badge ${h.online?'idle':'archived'}">${h.online?'在线':'离线'}</span></div>
      <div class="card-meta">🖥️ <span>${esc(h.hostname||'-')}</span> | 📡 <span>${esc(h.ip||'-')}</span> | 💻 <span>${esc(h.platform||'-')}</span><br>🏷️ <span>${esc(h.code_version||'-')}</span> <span style="color:var(--dim);font-size:11px">${vMark}</span><br>📁 <span>${esc(h.shared_folder||'-')}</span> (${h.shared_file_count||0}文件)</div>
      ${resBar('CPU',h.cpu_count+'核',h.cpu_percent)}${resBar('内存',fmtMem(h.memory_total_mb),h.memory_percent)}${resBar('磁盘',h.disk_total_gb+'GB',h.disk_percent)}
    </div>`;
  }).join('');
}
document.getElementById('host-search').addEventListener('input',renderHosts);
function resBar(l,t,p){p=Math.round(p||0);const c=p<60?'low':p<85?'mid':'high';return `<div class="res-row"><span class="res-label">${l}</span><div class="pbar"><div class="pfill ${c}" style="width:${p}%"></div></div><span class="res-val">${p}% / ${t}</span></div>`;}
async function showHostDetail(id){
  const h=allHosts.find(x=>x.device_id===id);if(!h)return;
  document.getElementById('modal').innerHTML=`<span class="modal-close" onclick="closeOverlayForce()">&times;</span><h2>${esc(h.device_name||h.hostname)}</h2><p style="color:var(--dim);font-size:13px">${h.online?'在线':'离线'} · ${h.role==='secretary'?'Secretary':h.role==='station'?'Station':'Worker'}</p>
  <div class="modal-sec"><h3>基本信息</h3><div class="kv-grid"><span class="k">设备ID</span><span class="v">${esc(h.device_id)}</span><span class="k">主机名</span><span class="v">${esc(h.hostname||'-')}</span><span class="k">IP</span><span class="v">${esc(h.ip||'-')}</span><span class="k">平台</span><span class="v">${esc(h.platform||'-')}</span><span class="k">API端口</span><span class="v">${h.api_port||'-'}</span><span class="k">代码版本</span><span class="v">${esc(h.code_version||'-')}${h.version_ts?' ('+new Date(h.version_ts*1000).toLocaleString()+')':''}</span><span class="k">注册时间</span><span class="v">${fmtTime(h.registered_at)}</span><span class="k">最后心跳</span><span class="v">${fmtTime(h.last_seen)}</span></div></div>
  <div class="modal-sec"><h3>硬件资源</h3><div class="kv-grid"><span class="k">CPU</span><span class="v">${h.cpu_count||0}核 / ${h.cpu_percent||0}%</span><span class="k">内存</span><span class="v">${fmtMem(h.memory_total_mb)} / ${h.memory_percent||0}%</span><span class="k">磁盘</span><span class="v">${h.disk_total_gb||0}GB / ${h.disk_percent||0}%</span></div></div>
  <div class="modal-sec"><h3>共享文件夹</h3><div class="kv-grid"><span class="k">路径</span><span class="v">${esc(h.shared_folder||'-')}</span><span class="k">文件数</span><span class="v">${h.shared_file_count||0}</span></div>${h.ip&&h.api_port?`<div style="margin-top:10px"><button class="btn primary sm" onclick="browseFiles('${esc(h.ip)}',${h.api_port})">📂 浏览共享文件</button></div>`:''}</div>`;
  document.getElementById('overlay').classList.add('show');
}
async function browseFiles(ip,port){
  try{const r=await fetch(`http://${ip}:${port}/shared`);const d=await r.json();let h='<div class="modal-sec"><h3>远程文件</h3>';
    if(d.files&&d.files.length){h+='<div class="kv-grid">';for(const f of d.files){const dl=`http://${ip}:${port}/shared/${encodeURIComponent(f.path)}`;h+=`<span class="k">${f.is_dir?'📁':'📄'} ${esc(f.name)}</span><span class="v"><a href="${dl}" target="_blank" style="color:var(--accent)">下载</a> (${fmtSize(f.size)})</span>`;}h+='</div>';}else{h+='<p style="color:var(--dim)">共享文件夹为空</p>';}h+='</div>';
    document.getElementById('modal').insertAdjacentHTML('beforeend',h);
  }catch(e){alert('无法访问:'+e.message);}
}
async function probeIP(){const ip=await showInputDialog('输入要探测的 Work Station IP:');if(!ip)return;apiFetch(`/api/probe/${ip}`,{method:'POST'}).then(r=>r.json()).then(d=>{showToast(d.message||'已发送','ok');setTimeout(refreshHosts,2000);}).catch(e=>showToast('失败:'+e.message,'err'));}
async function showNetworkInfo(){try{const r=await apiFetch('/api/network');const d=await r.json();alert(`UDP端口: ${d.udp_port}\nAPI端口: ${d.api_port}\n\n本地IP:\n${d.local_ips.join('\n')}\n\n广播目标:\n${d.broadcast_targets.join('\n')}`);}catch(e){alert('失败:'+e.message);}}

// ── 任务面板 ──
async function refreshTasks(){
  try{const r=await apiFetch('/api/tasks');const d=await r.json();allTasks=d.tasks||[];renderTasks();}catch(e){console.error(e);}
}
function fmtTokens(n){
  // iter-52: token 数缩写 (K/M)
  if(!n||n<1000)return String(n||0);
  if(n<1e6)return (n/1e3).toFixed(1)+'K';
  return (n/1e6).toFixed(1)+'M';
}
let wsSelectedProject='';  // 工作台当前选中的项目 ('' = 全部)
function toggleTaskExpand(el){el.classList.toggle('open');}
function getFilteredTasks(){
  if(!wsSelectedProject)return allTasks;
  return allTasks.filter(t=>t.project_id===wsSelectedProject);
}
function renderTasks(){
  const grid=document.getElementById('task-grid');
  const tasks=getFilteredTasks();
  if(!tasks.length){grid.innerHTML='<div class="empty"><div class="icon">📋</div><p>暂无任务</p><p class="hint">提交一个任务来开始编排</p></div>';return;}
  grid.innerHTML=tasks.map(t=>{
    const subs=t.subtasks||[];const sc=subs.length;const done=subs.filter(s=>s.status==='completed').length;
    const pct=sc?Math.round(done/sc*100):(t.status==='completed'?100:0);
    const c=t.status==='completed'?'idle':t.status==='failed'?'archived':t.status==='interrupted'?'archived':t.status==='forwarded'?'suspended':'busy';
    // iter-65: 联邦委托徽标 — 任务已转发对端 Secretary 执行
    const fwdBadge=t.status==='forwarded'?`<span class="badge suspended" style="margin-left:4px" title="已委托联邦对端执行">↗ 联邦转发</span>`:'';
    const pmInfo=t.pm_agent_id?` | 🧑‍💼 PM: ${esc(t.pm_agent_id.slice(0,12))}`:'';
    // iter-52: 成本感知调度 — 预估 token + 预算适配徽章
    const ce=(t.input_data&&t.input_data._cost_estimate)||null;
    const fit=ce&&ce.budget_fit;
    const fitColor=fit?(fit.status==='ok'?'var(--green)':fit.status==='tight'?'var(--yellow)':'#e5533c'):'';
    const costBadge=ce?` | 💰 ~${fmtTokens(ce.estimated_tokens)} tokens${fit&&fit.status!=='unknown'?` <span class="badge" style="background:${fitColor};color:#fff;font-size:10px;padding:1px 5px;border-radius:3px" title="${esc(fit.advice||fit.label||'')}">${fit.status==='ok'?'✅':fit.status==='tight'?'⚠️':'❌'} ${esc(fit.label||fit.status||'')}</span>`:''}`:'';
    const projName=t.project_id?projNameById(t.project_id):'';
    const subRows=subs.map(s=>`<div class="subtask-row"><span class="step-dot ${s.status}"></span><span style="flex:1">${esc(s.name||s.description||'子任务')}</span><span style="color:var(--dim)">[${s.status}]</span>${s.assigned_agent_id?'<span style="color:var(--dim)"> → '+esc(s.assigned_agent_id.slice(0,8))+'</span>':''}</div>`).join('');
    const canCancel=['running','monitoring','planning','executing'].includes(t.status);
    const canResubmit=t.status==='interrupted'||t.status==='failed'||t.status==='cancelled';
    const actionBtns=(canCancel?`<button class="btn sm" style="background:#e5a33c;color:#fff;border:none" onclick="event.stopPropagation();pauseTask('${t.task_id}','${escJs(t.name||'')}')">&#10074;&#10074; 暂停</button><button class="btn sm" style="background:#e5533c;color:#fff;border:none" onclick="event.stopPropagation();cancelTask('${t.task_id}','${escJs(t.name||'')}')">&#9724; 取消</button>`:'')+(canResubmit?`<button class="btn sm primary" onclick="event.stopPropagation();resubmitTask('${t.task_id}')">🔄 重新提交</button>`:'');
    const hasDelivery=t.output_data&&t.output_data._delivery;
    const review=hasDelivery?t.output_data._delivery.acceptance_review||{}:{};
    const reviewBadge=review.verdict==='pass'?`<span class="badge" style="background:var(--green);color:#fff;font-size:11px;padding:2px 6px;border-radius:4px;margin-left:6px">验收自检通过</span>`:review.verdict==='risk'?`<span class="badge" style="background:#e5533c;color:#fff;font-size:11px;padding:2px 6px;border-radius:4px;margin-left:6px">验收自检未全过</span>`:'';
    const deliveryBadge=hasDelivery?`<span class="badge" style="background:${t.output_data._delivery.accepted===true?'var(--green)':t.output_data._delivery.accepted===false?'#e5533c':'var(--yellow)'};color:#fff;font-size:11px;padding:2px 6px;border-radius:4px;margin-left:6px">${t.output_data._delivery.accepted===true?'✅ 已验收':t.output_data._delivery.accepted===false?'❌ 已退回':'📦 待验收'}</span>`:'';
    return `<div class="card task-card" onclick="toggleTaskExpand(this)">
      <div class="card-head"><div class="card-title"><span class="task-toggle">▶</span>${esc(t.name||'未命名')}<span class="badge ${c}">${t.status}</span>${fwdBadge}${deliveryBadge}${reviewBadge}</div></div>
      <div class="card-meta">${esc(t.description||'-')}<br>📊 ${done}/${sc} 子任务 (${pct}%) ${projName?'| 📁 '+esc(projName):''}${pmInfo}${costBadge}</div>
      <div class="pbar"><div class="pfill ${pct>=100?'low':'mid'}" style="width:${pct}%"></div></div>
      <div class="task-expand">${subRows||'<div class="subtask-row" style="color:var(--dim)">无子任务</div>'}
        <div class="subtask-row" style="display:flex;gap:6px;flex-wrap:wrap"><button class="btn sm" onclick="event.stopPropagation();showTaskDetail('${t.task_id}')">🔍 查看详情</button>${hasDelivery?`<button class="btn sm primary" onclick="event.stopPropagation();showDelivery('${t.task_id}')">📦 查看交付物</button>`:''}${t.task_id?`<button class="btn sm" onclick="event.stopPropagation();openDagForTask('${t.task_id}')">📈 DAG</button>`:''}${actionBtns}</div>
      </div>
    </div>`;
  }).join('');
}
function projNameById(pid){const p=allProjects.find(x=>x.project_id===pid);return p?p.name:pid.slice(0,8);}
function openDagForTask(taskId){
  switchWsView('dag');
  const sel=document.getElementById('dag-task-select');
  sel.value=taskId;
  loadDAGFromSelect();
}
async function showTaskDetail(id){
  const t=allTasks.find(x=>x.task_id===id);if(!t)return;
  let steps=(t.subtasks||[]).map(s=>`<div class="task-step"><span class="step-dot ${s.status}"></span><span>${esc(s.name||s.description||'子任务')} <span style="color:var(--dim)">[${s.status}]</span>${s.assigned_agent_id?' → '+esc(s.assigned_agent_id.slice(0,8)):''}</span></div>`).join('');
  let out=t.output_data&&Object.keys(t.output_data).length?`<pre style="max-height:200px;overflow:auto">${esc(JSON.stringify(t.output_data,null,2))}</pre>`:'<p style="color:var(--dim)">暂无输出</p>';
  const d=t.output_data&&t.output_data._delivery;
  const deliverySec=d?`<div class="modal-sec" style="border:1px solid var(--yellow);border-radius:8px;padding:12px"><h3>📦 交付物 ${d.accepted===true?'<span style="color:var(--green)">(已验收)</span>':d.accepted===false?'<span style="color:#e5533c">(已退回)</span>':'<span style="color:var(--yellow)">(待验收)</span>'}</h3><p style="font-size:13px;white-space:pre-wrap;max-height:150px;overflow:auto">${esc(d.summary||d.deliverable||'').slice(0,500)}</p><button class="btn sm primary" onclick="closeOverlayForce();showDelivery('${t.task_id}')">查看完整交付物</button></div>`:'';
  const canAct=['running','monitoring','planning','executing'].includes(t.status);
  document.getElementById('modal').innerHTML=`<span class="modal-close" onclick="closeOverlayForce()">&times;</span><h2>${esc(t.name)}</h2><p style="color:var(--dim);font-size:13px">${t.status} · ${fmtTime(t.created_at)}${t.completed_at?' → '+fmtTime(t.completed_at):''}</p>
  <div class="modal-sec"><h3>描述</h3><p style="font-size:14px">${esc(t.description||'-')}</p></div>
  ${deliverySec}
  <div class="modal-sec"><h3>子任务 (${(t.subtasks||[]).length})</h3>${steps||'<p style="color:var(--dim)">无子任务</p>'}</div>
  <div class="modal-sec"><h3>输出结果</h3>${out}</div>
  <div class="modal-sec" id="task-cost-sec" style="display:none"><h3>💰 成本预估</h3><div id="task-cost-box"></div></div>
  <div class="modal-sec" id="task-ckpt-sec" style="display:none"><h3>📌 断点 (Checkpoints)</h3><div id="task-ckpt-box"></div></div>
  <div style="margin-top:18px;display:flex;gap:10px;justify-content:flex-end">
    ${canAct?`<button class="btn sm" style="background:#e5a33c;color:#fff;border:none" onclick="closeOverlayForce();pauseTask('${t.task_id}','${escJs(t.name||'')}')">&#10074;&#10074; 暂停</button><button class="btn sm" style="background:#e5533c;color:#fff;border:none" onclick="closeOverlayForce();cancelTask('${t.task_id}','${escJs(t.name||'')}')">&#9724; 取消任务</button>`:''}
    <button class="btn sm" onclick="closeOverlayForce()">关闭</button>
    <button class="btn sm" style="background:#e5533c;color:#fff;border:none" onclick="deleteTask('${t.task_id}','${escJs(t.name||'')}')">🗑️ 删除任务</button>
  </div>`;
  document.getElementById('overlay').classList.add('show');
  const mtok=(window._taskModalSeq=(window._taskModalSeq||0)+1); // iter-101: 异步段回来时验证弹窗未被替换
  loadTaskCostSec(t.task_id,mtok);loadTaskCkptSec(t.task_id,mtok); // iter-101 UI-066: 异步补成本/断点段, 无数据静默隐藏
}
// iter-101 (UI-066): 任务详情弹窗 — 成本预估段 (GET /api/tasks/{id}/cost-estimate)
async function loadTaskCostSec(taskId,mtok){
  try{
    const r=await apiFetch(`/api/tasks/${encodeURIComponent(taskId)}/cost-estimate`);
    if(!r.ok)return;
    const e=await r.json();
    const sec=document.getElementById('task-cost-sec');
    if(!sec||!sec.isConnected||(mtok&&window._taskModalSeq!==mtok))return; // 弹窗已关闭/被替换
    const fit=e.budget_fit||{};
    const fitColor={ok:'#4ade80',tight:'#ffa94d',insufficient:'#ff6b6b'}[fit.status]||'var(--dim)';
    const rows=[['预估 Token',`<b>${Number(e.estimated_tokens||0).toLocaleString()}</b>`],
      ['预估依据',{baseline:'文本基线',history:'历史均值',mixed:'基线+历史混合',failed:'预估失败'}[e.basis]||esc(e.basis||'—')],
      ['置信度',{high:'高',medium:'中',low:'低',none:'无'}[e.confidence]||esc(e.confidence||'—')],
      ['历史样本',`${Number(e.history_samples||0)} 个 (均值 ${Number(e.history_avg_tokens||0).toLocaleString()})`],
      ['预算适配',`<span style="color:${fitColor};font-weight:700">${esc(fit.label||'—')}</span>${fit.advice?` <span style="color:var(--dim);font-size:12px">${esc(fit.advice)}</span>`:''}`]];
    if(e.saved_estimate)rows.push(['提交时预估',`<span style="color:var(--dim)">${Number(e.saved_estimate).toLocaleString()} token (落盘)</span>`]);
    document.getElementById('task-cost-box').innerHTML=`<table class="fleet-table"><tbody>`+rows.map(x=>`<tr><td style="color:var(--dim);width:110px">${x[0]}</td><td>${x[1]}</td></tr>`).join('')+`</tbody></table>`;
    sec.style.display='';
  }catch(err){/* 静默隐藏 */}
}
// iter-101 (UI-066): 任务详情弹窗 — 断点列表段 (GET /api/tasks/{id}/checkpoints)
async function loadTaskCkptSec(taskId,mtok){
  try{
    const r=await apiFetch(`/api/tasks/${encodeURIComponent(taskId)}/checkpoints`);
    if(!r.ok)return;
    const d=await r.json();
    const sec=document.getElementById('task-ckpt-sec');
    if(!sec||!sec.isConnected||(mtok&&window._taskModalSeq!==mtok))return;
    if(!d.total){sec.remove();return;} // 无断点静默隐藏
    document.getElementById('task-ckpt-box').innerHTML=`<table class="fleet-table"><thead><tr><th>Checkpoint</th><th>阶段</th><th>创建时间</th></tr></thead><tbody>`+
      (d.checkpoints||[]).map(c=>`<tr><td style="font-family:monospace;font-size:12px">${esc(String(c.checkpoint_id||'').slice(0,12))}</td><td>${esc(c.phase||'—')}</td><td>${fmtTime(c.created_at)}</td></tr>`).join('')+`</tbody></table>`
      +`<div style="margin-top:10px;text-align:right"><button class="btn sm primary" onclick="resumeTask('${escJs(taskId)}')">▶️ 断点恢复</button></div>`;
    sec.style.display='';
  }catch(e){/* 静默隐藏 */}
}
// iter-101 (UI-066): 断点恢复 (复用 iter-53 POST /api/tasks/{id}/resume)
async function resumeTask(taskId){
  if(!confirm('从最新执行态快照恢复该任务？PM 将续跑未完成部分。'))return;
  try{
    const r=await apiFetch(`/api/tasks/${encodeURIComponent(taskId)}/resume`,{method:'POST'});
    if(r.ok){
      showToast('▶️ 任务已从断点恢复','ok');closeOverlayForce();refreshTasks();refreshPMAgents();
    }else{
      const d=await r.json().catch(()=>({}));
      showToast('恢复失败: '+(d.detail||(r.status===404?'无执行态快照':r.status)),'err');
    }
  }catch(e){showToast('恢复请求异常: '+e.message,'err');}
}
async function deleteTask(taskId, name){
  if(!confirm(`确定删除任务 "${name}" 及其 PM 记录？此操作不可恢复。`))return;
  try{
    const resp=await apiFetch(`/api/tasks/${taskId}`,{method:'DELETE'});
    if(resp.ok){
      allTasks=allTasks.filter(t=>t.task_id!==taskId);
      renderTasks();
      renderWsOverview();
      refreshPMAgents();
      loadPMThreads();
      closeOverlayForce();
      showToast(`任务「${name}」已删除`,'info');
    }else{
      const err=await resp.json();
      alert('删除失败: '+(err.detail||'未知错误'));
    }
  }catch(e){alert('删除请求异常: '+e.message);}
}
async function submitTask(){
  const name=document.getElementById('task-name').value.trim();
  const desc=document.getElementById('task-desc').value.trim();
  const proj=document.getElementById('task-project').value;
  if(!name){showToast('请输入任务名称','err');return;}
  const btn=event&&event.target;try{
    if(btn){btn.disabled=true;btn.textContent='⏳ 提交中...';}
    const r=await apiFetch('/api/tasks',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name,description:desc,project_id:proj})});
    if(r.status===402){const d=await r.json();showToast('预算超支: '+d.detail,'err');return;}
    const t=await r.json();document.getElementById('task-name').value='';document.getElementById('task-desc').value='';refreshTasks();
    if(t.pm_agent_id){showToast(`✅ 任务已提交，PM: ${t.pm_agent_id.slice(0,12)}`,'ok');}
    else if(t.status==='failed'){showToast(`❌ 任务提交失败: ${(t.output_data||{}).error||'未知错误'}`,'err');}
    else{showToast('✅ 任务已提交: '+t.task_id.slice(0,8),'ok');}
  }catch(e){showToast('提交失败: '+e.message,'err');}
  finally{if(btn){btn.disabled=false;btn.textContent='📤 提交任务';}}
}
async function cancelTask(taskId,name){
  if(!confirm(`确定取消任务「${name}」？PM Agent 将被停止。`))return;
  try{const r=await apiFetch(`/api/tasks/${taskId}/cancel`,{method:'POST'});
    if(r.ok){showToast(`任务「${name}」已取消`,'info');refreshTasks();refreshPMAgents();}
    else{const d=await r.json();showToast('取消失败: '+(d.detail||'未知错误'),'err');}
  }catch(e){showToast('取消请求异常: '+e.message,'err');}
}
async function pauseTask(taskId,name){
  try{const r=await apiFetch(`/api/tasks/${taskId}/pause`,{method:'POST'});
    if(r.ok){showToast(`任务「${name}」已暂停`,'info');refreshTasks();}
    else{const d=await r.json();showToast('暂停失败: '+(d.detail||'未知错误'),'err');}
  }catch(e){showToast('暂停请求异常: '+e.message,'err');}
}
function showDelivery(taskId){
  const t=allTasks.find(x=>x.task_id===taskId);if(!t||!t.output_data||!t.output_data._delivery)return;
  const d=t.output_data._delivery;
  const stats=d.subtask_stats||{};
  const rev=d.acceptance_review||{};
  const revHtml=rev&&rev.checks?`<div class="modal-sec"><h3>验收自检</h3><p>结论: ${rev.verdict==='pass'?'<span style="color:var(--green)">✅ 全部达成</span>':rev.verdict==='risk'?'<span style="color:#e5533c">⚠️ 有未达成项</span>':'<span style="color:var(--dim)">未知</span>'} · ${rev.reviewed||0} 条${rev.summary?' · '+esc(rev.summary):''}</p><div style="max-height:180px;overflow:auto;font-size:13px">${(rev.checks||[]).map(c=>`<p style="margin:4px 0">- ${c.met?'✅':'❌'} ${esc(c.criterion||'')}${c.evidence?' — '+esc(c.evidence):''}</p>`).join('')}</div></div>`:'';
  const statusHtml=d.accepted===true?'<span style="color:var(--green)">✅ 已验收</span>':d.accepted===false?'<span style="color:#e5533c">❌ 已退回'+(d.reject_reason?' — '+esc(d.reject_reason):'')+'</span>':'<span style="color:var(--yellow)">📦 待验收</span>';
  document.getElementById('modal').innerHTML=`<span class="modal-close" onclick="closeOverlayForce()">&times;</span><h2>📦 交付物 — ${esc(t.name||'')}</h2>
  <p style="color:var(--dim);font-size:13px">状态: ${statusHtml} · 交付时间: ${d.delivered_at?fmtTime(d.delivered_at):'-'}</p>
  <div class="modal-sec"><h3>摘要</h3><p style="font-size:14px;white-space:pre-wrap">${esc(d.summary||'无摘要')}</p></div>
  ${revHtml}
  <div class="modal-sec"><h3>子任务统计</h3><p>总计: ${stats.total||0} · 完成: <span style="color:var(--green)">${stats.completed||0}</span> · 失败: <span style="color:#e5533c">${stats.failed||0}</span></p></div>
  <div class="modal-sec"><h3>完整交付内容</h3><pre style="max-height:400px;overflow:auto;white-space:pre-wrap;font-size:13px;background:var(--bg);padding:12px;border-radius:8px;border:1px solid var(--border)">${esc(d.deliverable||'无内容')}</pre></div>
  <div style="margin-top:18px;display:flex;gap:10px;justify-content:flex-end">
    <button class="btn sm" onclick="closeOverlayForce()">关闭</button>
    ${d.accepted===null?`<button class="btn sm" style="background:#e5533c;color:#fff;border:none" onclick="rejectDelivery('${taskId}')">❌ 退回</button><button class="btn sm" style="background:var(--green);color:#fff;border:none" onclick="acceptDelivery('${taskId}')">✅ 验收</button>`:''}
  </div>`;
  document.getElementById('overlay').classList.add('show');
}
async function acceptDelivery(taskId){
  try{const r=await apiFetch(`/api/tasks/${taskId}/accept`,{method:'POST'});
    if(r.ok){showToast('✅ 已验收','ok');closeOverlayForce();refreshTasks();}
    else{const d=await r.json();showToast('验收失败: '+(d.detail||''),'err');}
  }catch(e){showToast('请求异常: '+e.message,'err');}
}
async function rejectDelivery(taskId){
  const reason=await showInputDialog('退回原因 (可选):');
  if(reason===null)return;
  try{const r=await apiFetch(`/api/tasks/${taskId}/reject`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({reason})});
    if(r.ok){showToast('已退回交付物','info');closeOverlayForce();refreshTasks();}
    else{const d=await r.json();showToast('退回失败: '+(d.detail||''),'err');}
  }catch(e){showToast('请求异常: '+e.message,'err');}
}
async function resubmitTask(taskId){
  const t=allTasks.find(x=>x.task_id===taskId);if(!t)return;
  try{
    const r=await apiFetch('/api/tasks',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:t.name,description:t.description||'',project_id:t.project_id||''})});
    if(r.ok){const nt=await r.json();showToast(`✅ 任务已重新提交: ${nt.task_id.slice(0,8)}`,'ok');refreshTasks();}
    else{const d=await r.json();showToast('重新提交失败: '+(d.detail||''),'err');}
  }catch(e){showToast('请求异常: '+e.message,'err');}
}

