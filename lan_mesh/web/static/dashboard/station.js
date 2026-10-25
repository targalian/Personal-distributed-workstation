// ── 通用工具 ──
function closeOverlay(e){if(e.target===e.currentTarget)document.getElementById('overlay').classList.remove('show');}
function closeOverlayForce(){document.getElementById('overlay').classList.remove('show');}
function esc(s){if(!s)return'';return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');}
// JS 字符串上下文转义 (onclick 属性内): HTML 属性先解码实体再执行 JS,
// 故必须先转义 & 与 " 保住属性边界, 再用 \ 转义 ' 与 \ 防止 JS 字符串提前终止
function escJs(s){if(!s)return'';return String(s).replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/\\/g,'\\\\').replace(/'/g,"\\'").replace(/\r?\n/g,'\\n');}
function fmtMem(mb){if(!mb)return'-';return mb>=1024?(mb/1024).toFixed(1)+' GB':mb+' MB';}
function fmtSize(b){if(!b)return'0 B';const u=['B','KB','MB','GB','TB'];const i=Math.floor(Math.log(b)/Math.log(1024));return(b/Math.pow(1024,i)).toFixed(1)+' '+u[i];}
function fmtTime(ts){if(!ts)return'-';return new Date(ts*1000).toLocaleString('zh-CN');}

// ── Station面板 ──
let stationHosts=[],stationEvents=[];
let secretaryHostId=null,secretaryHostPort=null,secretaryIsLocal=false;

async function refreshStation(){
  try{
    const [fleetR,eventsR,secR]=await Promise.all([
      apiFetch('/api/station/fleet'),
      apiFetch('/api/station/events?limit=30'),
      apiFetch('/api/station/secretary-status')
    ]);
    const fleet=await fleetR.json();const evts=await eventsR.json();const sec=await secR.json();
    stationHosts=fleet.hosts||[];stationEvents=evts.events||[];
    secretaryHostId=sec.secretary_host_id||null;
    secretaryHostPort=sec.secretary_port||null;
    secretaryIsLocal=sec.is_local||false;
    // 同步 secretaryActive 状态
    secretaryActive=sec.active||false;
    updateSecretaryUI();
    renderStation();
  }catch(e){console.error('刷新Station失败:',e);}
}
function renderStation(){
  const online=stationHosts.filter(h=>h.online).length;
  const offline=stationHosts.length-online;
  document.getElementById('st-online').textContent=online;
  document.getElementById('st-offline').textContent=offline;
  const tiers={S:0,A:0,B:0,C:0,D:0};stationHosts.forEach(h=>{if(tiers[h.rating_tier]!==undefined)tiers[h.rating_tier]++;});
  document.getElementById('stat-cards').innerHTML=`
    <div class="stat-card online"><div class="num">${online}</div><div class="lbl">在线</div></div>
    <div class="stat-card offline"><div class="num">${offline}</div><div class="lbl">离线</div></div>
    <div class="stat-card tier-s"><div class="num">${tiers.S}</div><div class="lbl">S级</div></div>
    <div class="stat-card tier-a"><div class="num">${tiers.A}</div><div class="lbl">A级</div></div>
    <div class="stat-card tier-b"><div class="num">${tiers.B}</div><div class="lbl">B级</div></div>
    <div class="stat-card tier-c"><div class="num">${tiers.C+tiers.D}</div><div class="lbl">C/D级</div></div>`;
  const tbody=document.getElementById('fleet-body');
  if(!stationHosts.length){tbody.innerHTML='<tr><td colspan="8" style="text-align:center;color:var(--dim);padding:40px">暂无 Work Station</td></tr>';}
  else{tbody.innerHTML=stationHosts.map(h=>{
    const isStation=h.role==='station'||h.role==='secretary';
    const hasSecretary=secretaryHostId===h.device_id;
    let roleBadge;
    if(hasSecretary){roleBadge='<span style="color:var(--green);font-weight:600">主节点</span>';}
    else if(isStation){roleBadge='<span style="color:var(--accent)">Station</span>';}
    else{roleBadge='<span style="color:var(--dim)">Worker</span>';}
    let actionBtn='';
    if(hasSecretary){
      actionBtn=`<button class="btn sm danger" onclick="event.stopPropagation();revokeSecretary('${h.device_id}')">取消主节点</button>`;
    }else if(h.online){
      const label=isStation?'设为主节点':'设为主节点';
      actionBtn=`<button class="btn sm" onclick="event.stopPropagation();assignSecretary('${h.device_id}')">${label}</button>`;
    }
    const srcBadge=h.source==='fed'?`<span style="background:rgba(74,222,128,.12);color:#4ade80;border-radius:8px;padding:1px 6px;font-size:10px;margin-right:4px" title="跨网段联邦: ${esc(h.federation||'')}">🌐 联邦</span>`:'';
    return `<tr class="${h.online?'':'offline-row'}" onclick="showHostStationDetail('${h.device_id}')" style="cursor:pointer">
    <td><span class="status-dot ${h.online?'on':'off'}"></span>${h.online?'在线':'离线'}</td>
    <td>${srcBadge}${esc(h.device_name||h.hostname||'未知')}</td>
    <td>${esc(h.ip||'-')}</td>
    <td>${esc(h.platform||'-')}</td>
    <td><span class="badge-tier ${h.rating_tier||'D'}">${h.rating_tier||'-'}</span> ${h.rating_score||0}分</td>
    <td>${roleBadge}</td>
    <td>${actionBtn}</td>
    <td>${fmtTime(h.last_seen)}</td>
  </tr>`;
  }).join('');}
  const el=document.getElementById('event-list');
  if(!stationEvents.length){el.innerHTML='<p style="color:var(--dim);text-align:center;padding:20px">暂无事件</p>';}
  else{el.innerHTML=stationEvents.map(e=>`<div class="event-item">
    <span class="event-type ${e.event_type}">${e.event_type}</span>
    <span>${esc(e.device_id||'').slice(0,8)} ${e.detail?'· '+esc(e.detail):''}</span>
    <span class="event-time">${fmtTime(e.timestamp)}</span>
  </div>`).join('');}
}

// ── Secretary 远程分配 ──
async function assignSecretary(deviceId){
  if(secretaryHostId){alert('已有 Work Station 在运行 Secretary,请先取消');return;}
  if(!confirm('确定要在此 Work Station 上启动 Secretary 吗？'))return;
  try{
    const r=await apiFetch(`/api/station/hosts/${deviceId}/assign-secretary`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({})});
    const d=await r.json();
    if(d.ok){
      alert('Secretary 已分配!\n'+(d.message||'')+(d.port?'\n端口: '+d.port:''));
      refreshStation();
    }else{
      alert('分配失败: '+(d.detail||d.message||'未知错误'));
    }
  }catch(e){alert('失败:'+e.message);}
}
async function revokeSecretary(deviceId){
  if(!confirm('确定要取消此 Work Station 的 Secretary 角色吗？'))return;
  try{
    const r=await apiFetch(`/api/station/hosts/${deviceId}/revoke-secretary`,{method:'POST'});
    const d=await r.json();
    alert(d.message||'已取消');
    refreshStation();
  }catch(e){alert('失败:'+e.message);}
}
async function showHostStationDetail(id){
  try{
    const r=await apiFetch(`/api/station/hosts/${id}/events`);const d=await r.json();
    const h=stationHosts.find(x=>x.device_id===id);
    let eventsHtml=(d.events||[]).map(e=>`<div class="event-item"><span class="event-type ${e.event_type}">${e.event_type}</span><span>${esc(e.detail||'')}</span><span class="event-time">${fmtTime(e.timestamp)}</span></div>`).join('');
    document.getElementById('modal').innerHTML=`<span class="modal-close" onclick="closeOverlayForce()">&times;</span><h2>${esc(h.device_name||h.hostname||'Work Station')}</h2><p style="color:var(--dim);font-size:13px">${h.online?'在线':'离线'} · <span class="badge-tier ${h.rating_tier||'D'}">${h.rating_tier||'-'}</span> ${h.rating_score||0}分</p>
    <div class="modal-sec"><h3>评级信息</h3><div class="kv-grid"><span class="k">评级</span><span class="v">${h.rating_tier||'-'}级</span><span class="k">得分</span><span class="v">${h.rating_score||0}/100</span><span class="k">摘要</span><span class="v">${esc(h.rating_summary||'-')}</span></div></div>
    <div class="modal-sec"><h3>硬件配置</h3><div class="kv-grid"><span class="k">CPU</span><span class="v">${h.cpu_count||0}核</span><span class="k">内存</span><span class="v">${fmtMem(h.memory_total_mb)}</span><span class="k">磁盘</span><span class="v">${h.disk_total_gb||0}GB</span></div></div>
    <div class="modal-sec"><h3>事件历史 (${(d.events||[]).length})</h3><div class="event-list">${eventsHtml||'<p style="color:var(--dim)">无事件</p>'}</div></div>`;
    document.getElementById('overlay').classList.add('show');
  }catch(e){alert('失败:'+e.message);}
}
async function recomputeRatings(){
  if(!confirm('确定要重新评级所有主机吗？'))return;
  try{const r=await apiFetch('/api/station/rate',{method:'POST'});const d=await r.json();refreshStation();alert(`已重算 ${d.updated||0} 台主机评级`);}catch(e){alert('失败:'+e.message);}
}

