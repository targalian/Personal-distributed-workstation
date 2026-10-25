// ── 运行时性能 Tab (P0/P1 追踪数据) ──
function _rtFmtMs(ms){
  if(ms==null||ms<=0)return '—';
  return ms>=1000?(ms/1000).toFixed(2)+'s':Math.round(ms)+'ms';
}
function _rtFmtTs(ts){
  if(!ts)return '—';
  const d=new Date(ts*1000);
  const p=n=>String(n).padStart(2,'0');
  return `${p(d.getMonth()+1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}
function _rtStatusBadge(st){
  const map={ok:['rgba(74,222,128,.15)','#4ade80'],timeout:['rgba(255,169,77,.15)','#ffa94d'],error:['rgba(255,107,107,.15)','#ff6b6b'],failed:['rgba(255,107,107,.15)','#ff6b6b'],completed:['rgba(74,222,128,.15)','#4ade80']};
  const c=map[st]||['rgba(91,140,255,.15)','var(--accent)'];
  return `<span style="background:${c[0]};color:${c[1]};padding:2px 8px;border-radius:4px;font-size:11px;font-weight:700">${esc(st||'unknown')}</span>`;
}
async function refreshRuntime(){
  const hours=document.getElementById('runtime-hours').value||'1';
  try{
    const [m,c,t]=await Promise.all([
      apiFetch(`/api/runtime/metrics?hours=${hours}`).then(r=>r.json()),
      apiFetch('/api/runtime/calls?limit=50').then(r=>r.json()),
      apiFetch('/api/runtime/trace?limit=30').then(r=>r.json()),
    ]);
    renderRuntimeMetrics(m);
    renderRuntimeCalls((c.calls)||[]);
    renderRuntimeTrace((t.records)||[]);
  }catch(e){
    console.error('运行时性能加载失败:',e);
    document.getElementById('runtime-stat-cards').innerHTML='<div class="empty" style="grid-column:1/-1"><p>加载失败: '+esc(e.message)+'</p></div>';
  }
  loadTaskFlowList();
  loadStallAlerts();
  loadTaskMemory();
  loadErrors();
  loadRuntimeStats(); // iter-101 UI-068: JSONL 执行统计 (独立时间窗)
}
// iter-101 (UI-068): 执行统计 — GET /api/runtime/stats (JSONL 追踪源, 含子任务层)
async function loadRuntimeStats(){
  const box=document.getElementById('rstats-panel');
  if(!box)return;
  const hours=document.getElementById('rstats-hours').value||'1';
  try{
    const r=await apiFetch(`/api/runtime/stats?hours=${hours}`);
    if(!r.ok){box.innerHTML='<div class="empty"><p>加载失败: '+r.status+(r.status===503?' (秘书未激活)':'')+'</p></div>';return;}
    renderRuntimeStats(await r.json());
  }catch(e){box.innerHTML='<div class="empty"><p>加载失败: '+esc(e.message)+'</p></div>';}
}
function renderRuntimeStats(s){
  const box=document.getElementById('rstats-panel');
  if(!box)return;
  const info=document.getElementById('rstats-window-info');
  if(info)info.textContent=`窗口 ${s.window_hours||'?'}h · LLM 调用 ${Number(s.llm_calls)||0} 次`;
  const st=s.subtasks||{};
  const stTotal=Number(st.total)||0;
  const stRate=stTotal?((Number(st.completed)||0)/stTotal*100).toFixed(0)+'%':'—';
  const cards=`<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-bottom:12px">`
    +`<div class="stat-card"><div style="font-size:12px;color:var(--dim)">子任务成功率</div><div style="font-size:22px;font-weight:700;margin-top:4px;color:${stTotal&&stRate!=='—'&&Number(st.completed)/stTotal>=0.7?'#4ade80':'#ffa94d'}">${stRate}</div><div style="font-size:11px;color:var(--dim);margin-top:2px">${Number(st.completed)||0}✓ / ${Number(st.failed)||0}✗ · 共 ${stTotal}</div></div>`
    +`<div class="stat-card"><div style="font-size:12px;color:var(--dim)">LLM 调用</div><div style="font-size:22px;font-weight:700;margin-top:4px">${Number(s.llm_calls)||0}</div><div style="font-size:11px;color:var(--dim);margin-top:2px">均延 ${_rtFmtMs(s.avg_latency_ms)} · P99 ${_rtFmtMs(s.p99_latency_ms)}</div></div>`
    +`<div class="stat-card"><div style="font-size:12px;color:var(--dim)">Token 消耗</div><div style="font-size:22px;font-weight:700;margin-top:4px">${fmtTokens((Number(s.total_input_tokens)||0)+(Number(s.total_output_tokens)||0))}</div><div style="font-size:11px;color:var(--dim);margin-top:2px">入 ${fmtTokens(s.total_input_tokens)} · 出 ${fmtTokens(s.total_output_tokens)}</div></div>`
    +`<div class="stat-card"><div style="font-size:12px;color:var(--dim)">调用状态</div><div style="font-size:13px;margin-top:6px;line-height:1.6">${Object.entries(s.by_status||{}).map(([k,v])=>`${_rtStatusBadge(k)} ×${Number(v)||0}`).join(' ')||'—'}</div></div></div>`;
  const distTbl=(label,obj)=>{
    const es=Object.entries(obj||{}).sort((a,b)=>b[1]-a[1]);
    if(!es.length)return '';
    const max=Number(es[0][1])||1;
    return `<div class="stitle" style="font-size:12px;margin:10px 0 6px">${label}</div><table class="fleet-table"><tbody>`
      +es.map(([k,v])=>`<tr><td style="width:220px">${esc(k)}</td><td style="width:60px;text-align:right"><b>${Number(v)||0}</b></td><td><div style="background:var(--surface2);border-radius:3px;height:8px"><div style="width:${Math.max(2,Math.round((Number(v)||0)/max*100))}%;background:var(--accent);border-radius:3px;height:8px"></div></div></td></tr>`).join('')+`</tbody></table>`;
  };
  const errTbl=(s.errors||[]).length?`<div class="stitle" style="font-size:12px;margin:10px 0 6px">错误 Top5</div><table class="fleet-table"><thead><tr><th>模型: 错误</th><th>次数</th></tr></thead><tbody>`
    +s.errors.map(e=>`<tr><td style="color:#ff6b6b">${esc(e.model||'')} : ${esc(e.error||'')}</td><td><b>${Number(e.count)||0}</b></td></tr>`).join('')+`</tbody></table>`:'';
  const empty=(!(Number(s.llm_calls)||0)&&!stTotal)?'<div class="empty"><p>窗口内无 JSONL 追踪记录</p><p class="hint">子任务执行与 LLM 调用会自动写入追踪日志</p></div>':'';
  box.innerHTML=cards+empty+distTbl('模型分布 (JSONL)',s.by_model)+distTbl('技能分布 (子任务)',s.by_skill)+errTbl;
}
async function loadTaskFlowList(){
  const box=document.getElementById('taskflow-list');
  try{
    const r=await apiFetch('/api/runtime/task-flow-list?limit=20');
    if(!r.ok){box.innerHTML='<div class="empty"><p>加载失败: '+r.status+'</p></div>';return;}
    const d=await r.json();
    renderTaskFlowList((d.tasks)||[], d.stall_minutes||30);
  }catch(e){
    console.error('任务流总览加载失败:',e);
    box.innerHTML='<div class="empty"><p>加载失败: '+esc(e.message)+'</p></div>';
  }
}
function renderTaskFlowList(tasks, stallMinutes){
  const box=document.getElementById('taskflow-list');
  if(!tasks.length){box.innerHTML='<div class="empty"><p>暂无任务流记录</p><p class="hint">任务提交/执行/交付的阶段事件会自动记录于此</p></div>';return;}
  const stalled=tasks.filter(t=>t.stalled);
  const banner=stalled.length?
    `<div style="background:rgba(255,107,107,.08);border:1px solid rgba(255,107,107,.3);border-radius:8px;padding:8px 14px;margin-bottom:10px;font-size:12px">⚠️ <b>${stalled.length}</b> 个任务可能停滞 (未到终态且超过 ${stallMinutes} 分钟无阶段事件): ${stalled.slice(0,3).map(t=>esc(t.task_id)).join(', ')}${stalled.length>3?' …':''}</div>`:'';
  const statusCell=t=>t.done?'<span style="color:#4ade80;font-weight:700">✅ 已收尾</span>':
    (t.stalled?`<span style="color:#ff6b6b;font-weight:700" title="已空闲 ${_rtFmtMs(t.idle_ms)}">⚠️ 可能停滞</span>`:
    '<span style="color:#ffa94d;font-weight:700">🔄 进行中</span>');
  box.innerHTML=banner+`<table class="fleet-table"><thead><tr><th>任务</th><th>最新阶段</th><th>状态</th><th>阶段数</th><th>总耗时</th><th>末活动</th><th>操作</th></tr></thead><tbody>`+
    tasks.map(t=>`<tr>
      <td><span style="cursor:pointer;color:var(--accent)" title="点击复制: ${esc(t.task_id)}" onclick="copyTaskId('${escJs(t.task_id)}')">${esc(t.task_id)}</span></td>
      <td><span style="background:rgba(91,140,255,.12);color:var(--accent);padding:2px 10px;border-radius:12px;font-size:12px;font-weight:700">${esc(t.last_label||t.last_stage)}</span></td>
      <td>${statusCell(t)}</td>
      <td>${t.stage_count}</td>
      <td>${_rtFmtMs(t.total_ms)}</td>
      <td style="white-space:nowrap">${_rtFmtTs(t.last_ts)}</td>
      <td><button class="btn" style="padding:2px 10px;font-size:12px" onclick="jumpTaskFlow('${esc(t.task_id)}')">瀑布</button></td>
    </tr>`).join('')+`</tbody></table>`;
}
function jumpTaskFlow(tid){
  document.getElementById('taskflow-id').value=tid;
  queryTaskFlow();
}
// UI-065: 任务停滞告警面板 (iter-41 主动守护, 后端已就绪)
async function loadStallAlerts(){
  const box=document.getElementById('stall-alert-list');
  if(!box)return;
  const info=document.getElementById('stall-watch-info');
  try{
    const r=await apiFetch('/api/runtime/task-stall-alerts');
    if(!r.ok){
      box.innerHTML='<div class="empty"><p>加载失败: '+r.status+'</p></div>';
      if(info)info.textContent='加载失败';
      return;
    }
    renderStallAlerts(await r.json());
  }catch(e){
    console.error('停滞告警加载失败:',e);
    box.innerHTML='<div class="empty"><p>加载失败: '+esc(e.message)+'</p></div>';
    if(info)info.textContent='加载失败';
  }
}
function renderStallAlerts(d){
  const box=document.getElementById('stall-alert-list');
  const info=document.getElementById('stall-watch-info');
  if(info)info.textContent=(d.watching?'🟢 守护运行中':'🔴 守护未运行')+' · 巡检间隔 '+(d.interval??'—')+'s · 停滞阈值 '+(d.stall_minutes??'—')+' 分钟';
  const alerts=d.alerts||[];
  if(!alerts.length){box.innerHTML='<div class="empty"><p>✅ 当前无停滞告警</p><p class="hint">任务超过阈值分钟无阶段事件且未到终态时在此列出, WS 实时推送 + 手动巡检双通道</p></div>';return;}
  const lvStyle=l=>l>=3?'color:#ff6b6b;font-weight:700':(l>=2?'color:#ffa94d;font-weight:700':'color:var(--dim)');
  box.innerHTML=`<table class="fleet-table"><thead><tr><th>任务</th><th>级别</th><th>最新阶段</th><th>空闲时长</th><th>说明</th></tr></thead><tbody>`+
    alerts.map(a=>`<tr>
      <td><span style="cursor:pointer;color:var(--accent)" title="点击复制: ${esc(a.task_id)}" onclick="copyTaskId('${escJs(a.task_id)}')">${esc(a.task_id)}</span></td>
      <td style="${lvStyle(a.level)}">L${Number(a.level)||0}</td>
      <td><span style="background:rgba(91,140,255,.12);color:var(--accent);padding:2px 10px;border-radius:12px;font-size:12px;font-weight:700">${esc(a.last_label||a.last_stage||'—')}</span></td>
      <td>${_rtFmtMs((a.idle_min||0)*60000)}</td>
      <td style="max-width:340px;font-size:12px">${esc(a.message||'')}</td>
    </tr>`).join('')+`</tbody></table>`;
}
async function checkStallNow(btn){
  const orig=btn?btn.textContent:'';
  if(btn){btn.disabled=true;btn.textContent='⏳ 巡检中...';}
  try{
    const r=await apiFetch('/api/runtime/task-stall-alerts/check',{method:'POST'});
    if(!r.ok){const d=await r.json().catch(()=>({}));showToast('巡检失败: '+(d.detail||r.status),'err');return;}
    const d=await r.json();
    window._stallManualTs=Date.now(); // iter-91 F4: 4s 内抑制 WS task_stall_alert 重复 toast
    const n=(d.pushed||[]).length;
    showToast(n?('🔔 巡检完成, 新推送 '+n+' 条停滞告警'):'✅ 巡检完成, 未发现新增停滞','ok');
    await loadStallAlerts();
  }catch(e){showToast('巡检失败: '+e.message,'err');}
  finally{if(btn){btn.disabled=false;btn.textContent=orig;}}
}
// UI-064: 影子开发面板 (iter-70/71 安全自举, 后端已就绪)
function _shadowStatusBadge(s){
  const m={queued:['⏳ 排队中','var(--dim)'],running:['⚙️ 执行中','var(--accent)'],READY_FOR_REVIEW:['✅ 待审','var(--green)'],GATES_FAILED:['⚠️ 门禁未过','#ffa94d'],AGENT_FAILED:['❌ Agent 失败','#ff6b6b'],ERROR:['💥 执行异常','#ff6b6b'],REPORT_ERROR:['❓ 报告异常','#ff6b6b']};
  const pair=m[s]||['❓ '+esc(s),'var(--dim)']; // iter-91 F2: 未知状态转义防 report.json 被篡改注入
  return `<span style="color:${pair[1]};font-weight:700;white-space:nowrap">${pair[0]}</span>`;
}
function _shadowIsoTs(v){
  if(!v)return '—';
  const d=new Date(v);
  return isNaN(d)?esc(v):d.toLocaleString();
}
function _shadowDur(r){
  if(!r.started_at)return '—';
  const a=new Date(r.started_at).getTime();
  if(isNaN(a))return '—';
  const b=r.finished_at?new Date(r.finished_at).getTime():Date.now();
  const ms=Math.max(0,(isNaN(b)?Date.now():b)-a);
  return ms>=60000?(ms/60000).toFixed(1)+' 分钟':Math.round(ms/1000)+' 秒';
}
async function refreshShadowDev(){
  const listBox=document.getElementById('shadow-run-list');
  const guardBox=document.getElementById('shadow-guardian');
  try{
    const r=await apiFetch('/api/shadow-dev/runs');
    if(!r.ok){
      if(r.status===503){renderShadowDegraded();return;}
      guardBox.innerHTML='<div class="empty"><p>加载失败: '+r.status+'</p></div>';
      listBox.innerHTML='<div class="empty"><p>加载失败: '+r.status+'</p></div>';
      return;
    }
    const d=await r.json();
    renderShadowGuardian(d.guardian||{});
    renderShadowRuns(d.runs||[]);
  }catch(e){
    console.error('影子开发加载失败:',e);
    guardBox.innerHTML='<div class="empty"><p>加载失败: '+esc(e.message)+'</p></div>';
    listBox.innerHTML='<div class="empty"><p>加载失败: '+esc(e.message)+'</p></div>';
  }
}
function renderShadowDegraded(){
  document.getElementById('shadow-guardian').innerHTML='<div class="empty"><div class="icon">🛡️</div><p>守护未启动</p><p class="hint">Station 未初始化影子开发守护 (503), 提交与队列不可用</p></div>';
  document.getElementById('shadow-run-list').innerHTML='<div class="empty"><p>守护未启动, 队列不可用</p></div>';
}
function renderShadowGuardian(g){
  const box=document.getElementById('shadow-guardian');
  const chip=(t,c)=>`<span style="background:rgba(91,140,255,.1);border:1px solid var(--border);border-radius:16px;padding:4px 12px;font-size:12px${c?';color:'+c:''}">${t}</span>`;
  const chips=[
    chip(g.running!==false?'🟢 守护线程运行中':'🔴 守护线程已停止'),
    chip(g.busy?'⚙️ 正在执行':'💤 空闲'),
    chip('队列 '+(g.queued||0))
  ];
  if(g.stopping)chips.push(chip('⏹️ 停止中','#ff6b6b'));
  box.innerHTML='<div style="display:flex;gap:8px;flex-wrap:wrap;padding:4px 0 10px">'+chips.join('')+'</div>';
}
function renderShadowRuns(runs){
  const box=document.getElementById('shadow-run-list');
  if(!runs.length){box.innerHTML='<div class="empty"><div class="icon">🕶️</div><p>暂无影子运行记录</p><p class="hint">提交开发任务后由常驻守护在仓库外副本串行执行, 产出 diff 与门禁报告, 主仓库不被改动</p></div>';return;}
  box.innerHTML=`<table class="fleet-table"><thead><tr><th>Run ID</th><th>任务</th><th>后端</th><th>状态</th><th>耗时</th><th>创建时间</th><th>操作</th></tr></thead><tbody>`+
    runs.map(x=>`<tr>
      <td><span style="cursor:pointer;color:var(--accent)" title="点击复制: ${esc(x.run_id)}" onclick="copyTaskId('${escJs(x.run_id)}')">${esc((x.run_id||'').slice(0,15))}</span></td>
      <td style="max-width:280px;font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(x.task||'')}">${esc((x.task||'').slice(0,60))}</td>
      <td><code style="font-size:12px">${esc(x.backend||'auto')}</code></td>
      <td>${_shadowStatusBadge(x.status)}</td>
      <td>${_shadowDur(x)}</td>
      <td style="white-space:nowrap;font-size:12px">${_shadowIsoTs(x.created_at||x.started_at)}</td>
      <td><button class="btn" style="padding:2px 10px;font-size:12px" onclick="openShadowDetail('${escJs(x.run_id)}')">详情</button></td>
    </tr>`).join('')+`</tbody></table>`;
}
async function openShadowDetail(runId){
  const md=document.getElementById('modal');
  md.innerHTML='<span class="modal-close" onclick="closeOverlayForce()">&times;</span><h2>🕶️ 影子运行报告</h2><p style="color:var(--dim);font-size:13px">加载中...</p>';
  document.getElementById('overlay').classList.add('show');
  try{
    const r=await apiFetch('/api/shadow-dev/runs/'+encodeURIComponent(runId));
    if(!r.ok){md.innerHTML='<span class="modal-close" onclick="closeOverlayForce()">&times;</span><h2>🕶️ 影子运行报告</h2><p style="color:var(--dim)">加载失败: '+(r.status===404?'运行不存在':r.status)+'</p>';return;}
    renderShadowDetail(await r.json());
  }catch(e){
    md.innerHTML='<span class="modal-close" onclick="closeOverlayForce()">&times;</span><h2>🕶️ 影子运行报告</h2><p style="color:var(--dim)">加载失败: '+esc(e.message)+'</p>';
  }
}
function renderShadowDetail(x){
  const md=document.getElementById('modal');
  const gates=x.gates||{};
  const gateRows=Object.keys(gates).length?
    Object.entries(gates).map(([k,g])=>`<div class="kv-grid" style="grid-template-columns:120px 70px 1fr"><span class="k">${esc(k)}</span><span class="v" style="color:${g.pass?'var(--green)':'#ff6b6b'}">${g.pass?'PASS':'FAIL'}</span><span class="v" style="font-size:12px">${esc(g.detail||'')}</span></div>`).join(''):'<p style="color:var(--dim)">无门禁数据</p>';
  const diff=x.diff||{};
  const findings=diff.secret_findings||[];
  const diffSec=diff.diff_file?`<div class="modal-sec"><h3>📦 Diff 产出</h3><div class="kv-grid"><span class="k">文件</span><span class="v">${esc(diff.diff_file)}</span><span class="k">大小</span><span class="v">${diff.bytes||0} 字节</span></div>${diff.stat?`<pre style="max-height:160px;overflow:auto;font-size:12px;margin-top:8px">${esc(diff.stat)}</pre>`:''}${findings.length?`<p style="color:#ff6b6b;font-size:12px;margin-top:8px">⚠️ 疑似明文密钥 ${findings.length} 处</p>`:''}</div>`:'';
  const agent=x.agent||{};
  const agentSec=Object.keys(agent).length?`<div class="modal-sec"><h3>🤖 Agent 执行</h3><div class="kv-grid"><span class="k">后端</span><span class="v">${esc(agent.backend||'—')}</span><span class="k">耗时</span><span class="v">${agent.elapsed_secs!=null?agent.elapsed_secs+'s':'—'}</span><span class="k">退出码</span><span class="v">${agent.returncode??'—'}</span>${agent.error?`<span class="k">错误</span><span class="v" style="color:#ff6b6b">${esc(agent.error)}</span>`:''}</div></div>`:'';
  md.innerHTML=`<span class="modal-close" onclick="closeOverlayForce()">&times;</span>
    <h2>🕶️ 影子运行 — ${esc((x.run_id||'').slice(0,15))}</h2>
    <p style="color:var(--dim);font-size:13px">${_shadowStatusBadge(x.status)} · 创建 ${_shadowIsoTs(x.created_at)} · 开始 ${_shadowIsoTs(x.started_at)} · 结束 ${_shadowIsoTs(x.finished_at)}</p>
    <div class="modal-sec"><h3>任务描述</h3><p style="font-size:14px;white-space:pre-wrap">${esc(x.task||'—')}</p></div>
    <div class="modal-sec"><h3>基础信息</h3><div class="kv-grid"><span class="k">后端</span><span class="v">${esc(x.backend||'auto')}</span><span class="k">超时</span><span class="v">${x.timeout??'—'}s</span><span class="k">副本</span><span class="v" style="font-size:12px">${esc(x.shadow_path||'—')}</span></div></div>
    <div class="modal-sec"><h3>🚧 门禁</h3>${gateRows}</div>
    ${agentSec}
    ${diffSec}
    ${x.error?`<div class="modal-sec"><h3>异常</h3><p style="color:#ff6b6b;font-size:13px;white-space:pre-wrap">${esc(x.error)}</p></div>`:''}
    <div style="margin-top:18px;display:flex;gap:10px;justify-content:flex-end"><button class="btn sm" onclick="closeOverlayForce()">关闭</button></div>`;
}
async function submitShadowRun(btn){
  const taskEl=document.getElementById('shadow-task');
  const task=(taskEl.value||'').trim();
  if(!task){showToast('任务描述不能为空','err');taskEl.focus();return;}
  if(task.length>4000){showToast('任务描述超过 4000 字符上限','err');return;}
  const backend=document.getElementById('shadow-backend').value||'';
  let timeout=parseInt(document.getElementById('shadow-timeout').value||'1800',10);
  if(isNaN(timeout))timeout=1800;
  timeout=Math.max(30,Math.min(timeout,1800));
  const orig=btn?btn.textContent:'';
  if(btn){btn.disabled=true;btn.textContent='⏳ 提交中...';}
  try{
    const r=await apiFetch('/api/shadow-dev/runs',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({task:task,backend:backend,timeout:timeout})});
    const d=await r.json().catch(()=>({}));
    if(!r.ok){
      if(r.status===503){renderShadowDegraded();showToast('提交失败: 守护未启动','err');return;}
      showToast('提交失败: '+(d.detail||(r.status===409?'队列冲突':r.status===422?'参数非法':r.status)),'err');
      return;
    }
    showToast('✅ 影子任务已提交: '+String(d.run_id||'').slice(0,15),'ok');
    taskEl.value='';
    await refreshShadowDev();
  }catch(e){showToast('提交失败: '+e.message,'err');}
  finally{if(btn){btn.disabled=false;btn.textContent=orig;}}
}
// iter-42: 任务记忆面板 (F4.1 历史经验可视化)
async function loadTaskMemory(){
  const box=document.getElementById('task-memory-panel');
  if(!box)return;
  try{
    const r=await apiFetch('/api/task-memory/overview?limit=10');
    if(!r.ok){box.innerHTML='<div class="empty"><p>加载失败: '+r.status+(r.status===503?' (秘书未激活)':'')+'</p></div>';return;}
    renderTaskMemory(await r.json());
  }catch(e){box.innerHTML='<div class="empty"><p>加载失败: '+esc(e.message)+'</p></div>';}
}
function renderTaskMemory(m){
  const box=document.getElementById('task-memory-panel');
  window._tmLastM=m; // iter-101 UI-066: 缓存供下钻重渲染
  if(window._tmSelType&&!(m.by_type||[]).some(g=>g.task_type===window._tmSelType))window._tmSelType='';
  if(!m.total){box.innerHTML='<div class="empty"><p>暂无任务记忆</p><p class="hint">任务完成后会自动沉淀经验于此, 供后续同类任务规划参考 (F4.1)</p></div>';return;}
  const pct=v=>(v*100).toFixed(0)+'%';
  const errs=(m.common_errors||[]).map(e=>`${esc(String(e[0]||''))} ×${e[1]}`).join(' · ')||'—';
  const cards=`<div class="stat-grid" style="display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:10px;margin-bottom:12px">`
    +`<div class="stat-card"><div style="font-size:12px;color:var(--dim)">记忆总数</div><div style="font-size:22px;font-weight:700;margin-top:4px">${m.total}</div></div>`
    +`<div class="stat-card"><div style="font-size:12px;color:var(--dim)">总体成功率</div><div style="font-size:22px;font-weight:700;margin-top:4px;color:${m.success_rate>=0.7?'#4ade80':'#ff6b6b'}">${pct(m.success_rate||0)}</div></div>`
    +`<div class="stat-card"><div style="font-size:12px;color:var(--dim)">推荐协作模式</div><div style="font-size:15px;font-weight:700;margin-top:8px">${esc(m.recommended_mode||'—')}</div></div>`
    +`<div class="stat-card"><div style="font-size:12px;color:var(--dim)">常见错误预警</div><div style="font-size:12px;margin-top:6px;line-height:1.5">${errs}</div></div></div>`;
  const typeTbl=(m.by_type||[]).length?`<table class="fleet-table"><thead><tr><th>任务类型</th><th>次数</th><th>成功率</th><th>平均耗时</th><th>推荐模式</th></tr></thead><tbody>`+
    m.by_type.map(g=>`<tr style="cursor:pointer${g.task_type===window._tmSelType?';background:var(--surface2)':''}" title="点击下钻该类型统计 (UI-066)" onclick="drillMemoryType('${escJs(g.task_type)}')"><td><b>${esc(g.task_type)}</b>${g.task_type===window._tmSelType?' <span style="color:var(--dim);font-size:11px">▼</span>':''}</td><td>${g.count}</td><td style="color:${g.success_rate>=0.7?'#4ade80':'#ffa94d'}">${pct(g.success_rate)}</td><td>${(g.avg_duration||0)>=60?((g.avg_duration/60).toFixed(1)+'m'):((g.avg_duration||0).toFixed(0)+'s')}</td><td>${esc(g.recommended_mode||'—')}</td></tr>`).join('')+`</tbody></table>`:'';
  const recentTbl=(m.recent||[]).length?`<div class="stitle" style="font-size:12px;margin:12px 0 8px">最近沉淀 (${m.recent.length} 条)</div>`+`<table class="fleet-table"><thead><tr><th>任务</th><th>类型</th><th>协作模式</th><th>团队</th><th>耗时</th><th>结果</th><th>关键词</th></tr></thead><tbody>`+
    m.recent.map(r=>`<tr><td><b>${esc(r.task_name||'—')}</b></td><td>${esc(r.task_type)}</td><td>${esc(r.collaboration_mode||'—')}</td><td>${r.team_size||'—'}</td><td>${(r.duration_secs||0)>=60?((r.duration_secs/60).toFixed(1)+'m'):((r.duration_secs||0).toFixed(0)+'s')}</td><td>${r.success?'<span style="color:#4ade80">✅</span>':('<span style="color:#ff6b6b" title="'+esc(r.error_pattern||'')+'">❌</span>')}</td><td style="max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${(r.keywords||[]).map(esc).join(', ')||'—'}</td></tr>`).join('')+`</tbody></table>`:'';
  box.innerHTML=cards+typeTbl+`<div id="tm-type-stats"></div>`+recentTbl;
  if(window._tmSelType)loadMemoryDrill(window._tmSelType); // 刷新后保持下钻态 (纯加载, 不切换)
}
// iter-101 (UI-066): 类型行点击 — 切换选中态后重渲染 (再次点击同类型 = 收起)
function drillMemoryType(tt){
  window._tmSelType=(window._tmSelType===tt)?'':tt;
  if(window._tmLastM)renderTaskMemory(window._tmLastM);
}
// iter-101 (UI-066): 拉取并渲染类型下钻统计卡 + 常见错误 Top3 (GET /api/task-memory/stats)
async function loadMemoryDrill(tt){
  const b2=document.getElementById('tm-type-stats');
  if(b2)b2.innerHTML='<div class="empty"><p>统计加载中...</p></div>';
  try{
    const r=await apiFetch('/api/task-memory/stats?task_type='+encodeURIComponent(tt));
    if(!r.ok||window._tmSelType!==tt)return; // 失败/已切换类型时静默
    const s=await r.json();
    const el=document.getElementById('tm-type-stats');
    if(!el)return;
    if(!s.total){el.innerHTML='';return;}
    const pct=v=>((v||0)*100).toFixed(0)+'%';
    const errs=(s.common_errors||[]).map(e=>`<span style="color:#ffa94d">${esc(String(e[0]||''))}</span> ×${Number(e[1])||0}`).join(' · ')||'—';
    el.innerHTML=`<div class="stitle" style="font-size:12px;margin:12px 0 8px">🔬 类型下钻: ${esc(tt)} <span style="color:var(--dim);font-weight:400">(点击类型行收起)</span></div>`
      +`<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px">`
      +`<div class="stat-card"><div style="font-size:12px;color:var(--dim)">同类任务</div><div style="font-size:20px;font-weight:700;margin-top:4px">${Number(s.total)||0}</div></div>`
      +`<div class="stat-card"><div style="font-size:12px;color:var(--dim)">成功率</div><div style="font-size:20px;font-weight:700;margin-top:4px;color:${(s.success_rate||0)>=0.7?'#4ade80':'#ff6b6b'}">${pct(s.success_rate)}</div></div>`
      +`<div class="stat-card"><div style="font-size:12px;color:var(--dim)">平均耗时</div><div style="font-size:20px;font-weight:700;margin-top:4px">${(s.avg_duration||0)>=60?((s.avg_duration/60).toFixed(1)+'m'):((s.avg_duration||0).toFixed(0)+'s')}</div></div>`
      +`<div class="stat-card"><div style="font-size:12px;color:var(--dim)">推荐模式</div><div style="font-size:14px;font-weight:700;margin-top:8px">${esc(s.recommended_mode||'—')}</div></div>`
      +`<div class="stat-card"><div style="font-size:12px;color:var(--dim)">常见错误 Top</div><div style="font-size:12px;margin-top:6px;line-height:1.5">${errs}</div></div></div>`;
  }catch(e){/* 下钻失败不打扰主面板 */}
}
// iter-44: 错误追踪面板 (F1.4 异常聚合可视化)
async function loadErrors(){
  const box=document.getElementById('error-panel');
  if(!box)return;
  try{
    const [rs,rr,rd,rh,rhd,rhl,rhs]=await Promise.all([apiFetch('/api/errors/stats'),apiFetch('/api/errors/recent?limit=15'),apiFetch('/api/errors/diagnosis?window=200'),apiFetch('/api/errors/history?limit=20'),apiFetch('/api/errors/diagnosis?source=history&window=200'),apiFetch('/api/errors/heal/history?limit=10'),apiFetch('/api/errors/heal/status')]);
    if(!rs.ok||!rr.ok){box.innerHTML='<div class="empty"><p>加载失败: '+(rs.status||rr.status)+'</p></div>';return;}
    // 诊断建议/持久化历史/自愈历史/守护状态失败不影响主面板 (降级为无该区块)
    const diag=rd.ok?await rd.json():null;
    const hist=rh.ok?((await rh.json()).errors||[]):null;
    const hdiag=rhd.ok?await rhd.json():null;
    const heals=rhl.ok?((await rhl.json()).heals||[]):null;
    const healStatus=rhs.ok?await rhs.json():null;
    renderErrors(await rs.json(),(await rr.json()).errors||[],diag,hist,hdiag,heals,healStatus);
  }catch(e){box.innerHTML='<div class="empty"><p>加载失败: '+esc(e.message)+'</p></div>';}
}
function renderErrors(stats,errors,diag,hist,hdiag,heals,healStatus){
  const box=document.getElementById('error-panel');
  const total=stats.total_errors||0;
  // 统计卡片 (总错误数/窗口计数与阈值对比, 超限红色) + 按模块分布行
  const win=stats.recent_window_count||0,th=stats.alert_threshold||0;
  const mods=Object.entries(stats.by_module||{}).map(([m,v])=>`<b>${esc(m)}</b>×${v.total}`).join(' · ')||'—';
  const cards=`<div class="stat-grid" style="display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:10px;margin-bottom:12px">`
    +`<div class="stat-card"><div style="font-size:12px;color:var(--dim)">错误总数 (进程内)</div><div style="font-size:22px;font-weight:700;margin-top:4px;color:${total?'#ffa94d':'#4ade80'}">${total}</div></div>`
    +`<div class="stat-card"><div style="font-size:12px;color:var(--dim)">告警窗口计数</div><div style="font-size:22px;font-weight:700;margin-top:4px;color:${th&&win>=th?'#ff6b6b':'var(--text)'}">${win} / ${th}</div><div style="font-size:11px;color:var(--dim);margin-top:2px">达阈触发突发告警</div></div>`
    +`<div class="stat-card"><div style="font-size:12px;color:var(--dim)">按模块分布</div><div style="font-size:12px;margin-top:6px;line-height:1.5">${mods}</div></div></div>`;
  // iter-46: 🔧 自愈诊断建议 (F4.2) — 模式分组 + 建议动作, 无诊断数据时隐藏;
  // iter-48: 同构渲染器复用于历史诊断 (重启后不断档)
  const CAT={timeout:'⏱ 超时',connection:'🔌 连接',auth:'🔑 认证',rate_limit:'🚦 限流',upstream_5xx:'🌐 上游5xx'};
  const mkDiag=(d,head,emptyHead)=>{
    if(!(d&&d.scanned))return '';
    if(d.findings&&d.findings.length){
      return `<div style="font-size:13px;font-weight:600;margin:10px 0 6px">${head} (扫描 ${d.scanned} 条${d.unmatched?', 未识别 '+d.unmatched:''})</div>`
        +d.findings.map(f=>`<div style="background:rgba(255,169,77,.06);border:1px solid rgba(255,169,77,.25);border-radius:8px;padding:8px 12px;margin:6px 0;font-size:12px" title="${esc(f.sample||'')}">`
        +`<button onclick="runHeal('${f.action}','${f.category}')" style="float:right;background:rgba(74,222,128,.12);border:1px solid rgba(74,222,128,.4);color:#4ade80;border-radius:6px;padding:2px 10px;font-size:11px;cursor:pointer">🩹 执行</button>`
        +`<b style="color:#ffa94d">${CAT[f.category]||f.category}</b> ×${f.count} <span style="color:var(--dim)">[${(f.modules||[]).join('/')}]</span> — ${esc(f.suggestion||'')}</div>`).join('');
    }
    return `<div style="font-size:12px;color:var(--dim);margin:8px 0">${emptyHead}: 扫描 ${d.scanned} 条, 未命中已知错误模式</div>`;
  };
  let diagBlock=mkDiag(diag,'🔧 自愈诊断建议','🔧 自愈诊断');
  let histDiagBlock=mkDiag(hdiag,'🗂 历史诊断 (持久化记录)','🗂 历史诊断');
  // iter-49: 🩹 自愈执行历史 (heal_log 表, 跨重启保留), 空/拉取失败时隐藏
  // iter-50: 顶部附 🛡 自动自愈守护状态条 (开关/周期/冷却/轮次 + 立即检查)
  const fmtFull=ts=>{try{return new Date(ts*1000).toLocaleString();}catch(e){return '';}};
  let healStatusBlock='';
  if(healStatus){
    healStatusBlock=`<div style="font-size:12px;color:var(--dim);margin:10px 0 4px;display:flex;align-items:center;gap:12px;flex-wrap:wrap">`
      +`<span>🛡 自动自愈: <b style="color:${healStatus.enabled?'#4ade80':'var(--dim)'}">${healStatus.enabled?'已启用':'已禁用'}</b> (周期 ${Math.round(healStatus.interval||0)}s · 冷却 ${Math.round(healStatus.cooldown||0)}s)</span>`
      +(healStatus.runs?`<span>已扫描 ${healStatus.runs} 轮</span>`:'')
      +`<button onclick="runAutoHealCheck()" style="background:rgba(74,222,128,.12);border:1px solid rgba(74,222,128,.4);color:#4ade80;border-radius:6px;padding:2px 10px;font-size:11px;cursor:pointer">🔍 立即检查</button></div>`;
  }
  let healBlock='';
  if(heals&&heals.length){
    const RES={ok:'<span style="color:#4ade80">✅ 已执行</span>',failed:'<span style="color:#ff6b6b">❌ 失败</span>',manual_required:'<span style="color:#ffa94d">🙋 需人工</span>'};
    healBlock=`<div style="font-size:13px;font-weight:600;margin:14px 0 6px">🩹 自愈执行历史 (跨重启保留, 最近 ${heals.length} 条)</div>`
      +`<table class="fleet-table"><thead><tr><th>时间</th><th>动作</th><th>类别</th><th>结果</th><th>详情</th></tr></thead><tbody>`
      +heals.map(h=>`<tr><td style="white-space:nowrap">${fmtFull(h.timestamp)}</td><td><b>${esc(h.action||'')}</b></td><td>${CAT[h.category]||esc(h.category||'')}</td><td>${RES[h.result]||esc(h.result||'')}</td><td style="max-width:380px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(h.detail||'')}">${esc(h.detail||'')}</td></tr>`).join('')+`</tbody></table>`;
  }
  // iter-47: 📜 持久化历史 (error_log 表, 跨重启保留), 空/拉取失败时隐藏
  let histBlock='';
  if(hist&&hist.length){
    histBlock=`<div style="font-size:13px;font-weight:600;margin:14px 0 6px">📜 持久化历史 (跨重启保留, 最近 ${hist.length} 条)</div>`
      +`<table class="fleet-table"><thead><tr><th>时间</th><th>模块</th><th>类型</th><th>消息</th></tr></thead><tbody>`
      +hist.slice().reverse().map(e=>`<tr><td style="white-space:nowrap">${fmtFull(e.timestamp)}</td><td><b>${esc(e.module||'')}</b></td><td style="color:#74c0fc">${esc(e.error_type||'')}</td><td style="max-width:420px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(e.message||'')}">${esc(e.message||'')}</td></tr>`).join('')+`</tbody></table>`;
  }
  if(!errors.length){box.innerHTML=cards+diagBlock+histDiagBlock+healStatusBlock+healBlock+histBlock+'<div class="empty"><p>暂无错误记录</p><p class="hint">进程内捕获的异常会实时汇聚于此 (环形保留最近 500 条)</p></div>';return;}
  const fmtTs=ts=>{try{return new Date(ts*1000).toLocaleTimeString();}catch(e){return '';}};
  const tbl=`<table class="fleet-table"><thead><tr><th>时间</th><th>模块</th><th>类型</th><th>消息</th></tr></thead><tbody>`+
    errors.slice().reverse().map(e=>`<tr><td style="white-space:nowrap">${fmtTs(e.timestamp)}</td><td><b>${esc(e.module||'')}</b></td><td style="color:#ffa94d">${esc(e.error_type||'')}</td><td style="max-width:420px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(e.message||'')}">${esc(e.message||'')}</td></tr>`).join('')+`</tbody></table>`;
  box.innerHTML=cards+diagBlock+histDiagBlock+healStatusBlock+healBlock+tbl+histBlock;
}
// iter-50: 🛡 手动触发一轮自动自愈扫描 (与守护线程同逻辑)
async function runAutoHealCheck(){
  try{
    const r=await apiFetch('/api/errors/heal/auto-check',{method:'POST'});
    const d=await r.json();
    const n=(d.actions_run||[]).length;
    showToast(n?('已自动执行 '+n+' 个自愈动作'):('扫描完成: '+(d.skipped_cooldown&&d.skipped_cooldown.length?'部分冷却中/':'无需执行或需人工')), n?'ok':'info');
    loadErrors();
  }catch(e){showToast('检查失败: '+esc(e.message),'err');}
}
// iter-49: 🩹 自愈动作执行 (F4.2 修复环节) — POST 后 Toast 结果并刷新错误面板
async function runHeal(action,category){
  try{
    const r=await apiFetch(`/api/errors/heal?action=${encodeURIComponent(action||'')}&category=${encodeURIComponent(category||'')}`,{method:'POST'});
    const d=await r.json();
    const RES={ok:'✅ 已执行',failed:'❌ 执行失败',manual_required:'🙋 需人工介入'};
    showToast(`${RES[d.result]||d.result}: ${d.detail||''}`,d.result==='ok'?'ok':'err');
    loadErrors();
  }catch(e){showToast('执行失败: '+esc(e.message),'err');}
}
function renderRuntimeMetrics(m){
  document.getElementById('runtime-window-info').textContent=`窗口: ${m.window_hours||0}h · ${new Date().toLocaleTimeString()}`;
  const errCount=(m.by_status||{}).error||0;
  const timeoutCount=(m.by_status||{}).timeout||0;
  const tokenTotal=(m.total_input_tokens||0)+(m.total_output_tokens||0);
  const card=(label,val,sub)=>`<div class="stat-card"><div style="font-size:12px;color:var(--dim)">${label}</div><div style="font-size:22px;font-weight:700;margin-top:4px">${val}</div>${sub?`<div style="font-size:11px;color:var(--dim);margin-top:2px">${sub}</div>`:''}</div>`;
  document.getElementById('runtime-stat-cards').innerHTML=
    card('调用总数',m.total_calls||0,`异常 ${errCount} · 超时 ${timeoutCount}`)+
    card('平均延迟',_rtFmtMs(m.avg_latency_ms),`P99: ${_rtFmtMs(m.p99_latency_ms)}`)+
    card('平均 TTFT',_rtFmtMs(m.avg_ttft_ms),'首 token 延迟')+
    card('Token 总量',tokenTotal.toLocaleString(),`入 ${(m.total_input_tokens||0).toLocaleString()} · 出 ${(m.total_output_tokens||0).toLocaleString()}`);
  // 近期错误汇总 (红/橙横幅)
  const errs=(m.recent_errors||[]);
  document.getElementById('runtime-errors').innerHTML=errs.length?
    `<div style="background:rgba(255,107,107,.08);border:1px solid rgba(255,107,107,.3);border-radius:8px;padding:10px 14px;margin:12px 0;font-size:12px">`+
    errs.map(e=>`<div style="margin:2px 0">❌ <b>${esc(e.model||'')}</b> × ${e.count}: ${esc((e.error||'').slice(0,120))}</div>`).join('')+`</div>`:'';
  // 按模型统计表
  const models=Object.entries(m.by_model||{}).sort((a,b)=>b[1].calls-a[1].calls);
  document.getElementById('runtime-model-table').innerHTML=models.length?
    `<table class="fleet-table"><thead><tr><th>模型</th><th>调用数</th><th>Token</th><th>平均延迟</th></tr></thead><tbody>`+
    models.map(([name,s])=>`<tr><td><b>${esc(name)}</b></td><td>${s.calls}</td><td>${(s.tokens||0).toLocaleString()}</td><td>${_rtFmtMs(s.avg_ms)}</td></tr>`).join('')+
    `</tbody></table>`:'<div class="empty"><p>窗口内无调用记录</p></div>';
}
function renderRuntimeCalls(calls){
  const box=document.getElementById('runtime-call-table');
  if(!calls.length){box.innerHTML='<div class="empty"><p>暂无调用记录</p></div>';return;}
  box.innerHTML=`<table class="fleet-table"><thead><tr><th>时间</th><th>类型</th><th>模型</th><th>Token 入/出</th><th>TTFT</th><th>总时长</th><th>状态</th><th>任务</th><th>错误</th></tr></thead><tbody>`+
    calls.map(c=>`<tr>
      <td style="white-space:nowrap">${_rtFmtTs(c.created_at)}</td>
      <td>${esc(c.call_type||'')}</td>
      <td>${esc(c.model||'')}</td>
      <td>${(c.input_tokens||0).toLocaleString()} / ${(c.output_tokens||0).toLocaleString()}</td>
      <td>${_rtFmtMs(c.ttft_ms)}</td>
      <td>${_rtFmtMs(c.total_ms)}</td>
      <td>${_rtStatusBadge(c.status)}</td>
      <td>${c.task_id?`<span style="cursor:pointer;color:var(--accent)" title="点击复制: ${esc(c.task_id)}" onclick="copyTaskId('${escJs(c.task_id)}')">${esc((c.task_id).slice(0,8))}</span>`:'—'}</td>
      <td style="max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(c.error||'')}">${esc(c.error||'')}</td>
    </tr>`).join('')+`</tbody></table>`;
}
function renderRuntimeTrace(records){
  const box=document.getElementById('runtime-trace-list');
  if(!records.length){box.innerHTML='<div class="empty"><p>暂无轨迹记录</p></div>';return;}
  box.innerHTML=`<table class="fleet-table"><thead><tr><th>时间</th><th>类型</th><th>技能/模型</th><th>状态</th><th>耗时</th><th>Token</th><th>备注</th></tr></thead><tbody>`+
    records.filter(r=>r.type!=='subtask_start').map(r=>{
      const isLLM=r.type==='llm_call';
      const name=isLLM?(r.model||''):(r.model?`${r.skill} (${r.model})`:r.skill||'');
      const note=isLLM?(r.error||''):(r.error||r.task_id||'');
      const toks=isLLM?`${(r.input_tokens||0).toLocaleString()}/${(r.output_tokens||0).toLocaleString()}`:
        ((r.input_tokens||r.output_tokens)?`${(r.input_tokens||0).toLocaleString()}/${(r.output_tokens||0).toLocaleString()}`:'—');
      return `<tr>
        <td style="white-space:nowrap">${_rtFmtTs(r.ts)}</td>
        <td>${isLLM?'LLM':'子任务'}</td>
        <td><b>${esc(name)}</b></td>
        <td>${_rtStatusBadge(r.status)}</td>
        <td>${_rtFmtMs(isLLM?r.total_ms:r.elapsed_ms)}</td>
        <td>${toks}</td>
        <td style="max-width:240px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(note)}">${esc(note)}</td>
      </tr>`;
    }).join('')+`</tbody></table>`;
}

function copyTaskId(id){
  navigator.clipboard.writeText(id).then(()=>showToast('已复制 '+id,'ok')).catch(()=>showToast('复制失败','err'));
}
async function queryTaskFlow(){
  const tid=document.getElementById('taskflow-id').value.trim();
  const box=document.getElementById('taskflow-box');
  if(!tid){box.innerHTML='<div class="empty"><p>请输入 task_id</p></div>';return;}
  try{
    const r=await apiFetch('/api/runtime/task-flow?task_id='+encodeURIComponent(tid));
    if(!r.ok){const d=await r.json().catch(()=>({}));box.innerHTML='<div class="empty"><p>查询失败: '+esc(d.detail||r.status)+'</p></div>';return;}
    const flow=await r.json();
    renderTaskFlow(flow);
  }catch(e){
    console.error('任务流查询失败:',e);
    box.innerHTML='<div class="empty"><p>查询失败: '+esc(e.message)+'</p></div>';
  }
}
function renderTaskFlow(flow){
  const box=document.getElementById('taskflow-box');
  if(!flow.events||!flow.events.length){
    box.innerHTML='<div class="empty"><p>该任务无追踪记录</p><p class="hint">仅记录启用追踪后发生的阶段事件 (追溯 5000 行)</p></div>';return;
  }
  const stageColor={submitted:'var(--accent)','pm:planning':'#ffa94d','pm:executing':'#4ade80','pm:monitoring':'#4ade80','pm:completed':'#4ade80','pm:failed':'#ff6b6b','pm:cancelled':'#ff6b6b','pm:paused':'#ffa94d','pm:awaiting_input':'#ffa94d',subtask_result:'#74c0fc',delivered:'#4ade80'};
  box.innerHTML=`<div style="font-size:12px;color:var(--dim);margin-bottom:10px">共 ${flow.stage_count} 个阶段 · 总耗时 <b style="color:var(--text)">${_rtFmtMs(flow.total_ms)}</b></div>`+
    flow.events.map(e=>`<div style="display:flex;gap:12px;padding:8px 0;border-bottom:1px solid var(--border);font-size:13px">
      <span style="color:var(--dim);white-space:nowrap;min-width:110px">${_rtFmtTs(e.ts)}</span>
      <span style="background:rgba(91,140,255,.12);color:${stageColor[e.stage]||'var(--accent)'};padding:2px 10px;border-radius:12px;font-size:12px;font-weight:700;white-space:nowrap;height:fit-content">${esc(e.label)}</span>
      <span style="color:var(--dim);flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(e.detail||'')}">${esc(e.detail||'')}</span>
      ${e.gap_ms?`<span style="color:var(--dim);white-space:nowrap;font-size:12px" title="距上一阶段">+${_rtFmtMs(e.gap_ms)}</span>`:''}
    </div>`).join('');
}

