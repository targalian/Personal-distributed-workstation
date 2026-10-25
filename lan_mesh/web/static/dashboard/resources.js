// ── 模型资源面板 ──
async function refreshResources(){
  loadCloudSync(); // iter-101 UI-069: 云存储同步状态卡
  try{
    const r=await apiFetch('/api/resources');const d=await r.json();
    renderResAlerts(d.alerts||[]);
    document.getElementById('res-total').textContent=(d.resources||[]).length;
    const stateEl=document.getElementById('res-state');
    stateEl.textContent=d.enabled?'已启用':'未启用';
    stateEl.style.color=d.enabled?'var(--green)':'var(--dim)';
    const body=document.getElementById('res-body');
    if(!(d.resources||[]).length){
      body.innerHTML='<tr><td colspan="10" style="text-align:center;color:var(--dim);padding:20px">未配置资源池 — 点击上方「⚙️ 配置资源」打开配置向导</td></tr>';
      return;
    }
    body.innerHTML=(d.resources||[]).map(res=>{
      const pct=Math.min(100,Math.round((res.rate||0)*100));
      const color=pct>=90?'var(--red)':pct>=70?'#e6a23c':'var(--green)';
      const bal=res.balance||{};
      let balTxt='<span style="color:var(--dim)">—</span>';
      if(bal.supported)balTxt=`${fmtMoney(bal.balance)}<span style="color:var(--dim)"> ${esc(bal.currency||'')}</span>`;
      else if(bal.error)balTxt=`<span style="color:#e6a23c" title="${esc(bal.hint||bal.error)}">未获取</span>`;
      return `<tr>
        <td><b>${esc(res.resource_id)}</b></td>
        <td>${esc(res.provider||'')}</td>
        <td>${esc(res.plan_type||'')}</td>
        <td>${fmtQuota(res.quota,res.unit)}</td>
        <td>${fmtQuota(res.used,res.unit)}</td>
        <td>${fmtQuota(res.remaining,res.unit)}</td>
        <td style="color:${color}">${pct}%</td>
        <td>${balTxt}</td>
        <td>${esc(res.status||'')}</td>
        <td style="color:var(--dim)">${esc(res.note||'')}</td>
      </tr>`;
    }).join('');
  }catch(e){console.error(e);}
  finally{refreshRotationPlan();refreshCostByTask();}  // 早退分支也必须刷新轮换表与成本表
}
// iter-101 (UI-069): 云存储同步状态卡 — GET /api/cloud-sync/status
let _csDegraded=false; // 503/未启动降级态: 禁用两个操作按钮
async function loadCloudSync(){
  const box=document.getElementById('cloud-sync-card');
  if(!box)return;
  try{
    const r=await apiFetch('/api/cloud-sync/status');
    if(!r.ok){box.innerHTML='<div class="empty"><p>状态加载失败: '+r.status+'</p></div>';return;}
    renderCloudSync(await r.json());
  }catch(e){box.innerHTML='<div class="empty"><p>状态加载失败: '+esc(e.message)+'</p></div>';}
}
function renderCloudSync(s){
  const box=document.getElementById('cloud-sync-card');
  if(!box)return;
  // 降级判据: 完整状态含 endpoint 字段; 未启动时后端只回 4 字段+message
  _csDegraded=!(s.enabled&&s.endpoint);
  const badge=(on,tOn,tOff)=>`<span class="badge ${on?'idle':'archived'}" style="margin-right:6px">${on?tOn:tOff}</span>`;
  const last=s.last_sync||null;
  const errs=(last&&last.errors||[]);
  const lastTxt=last?`↑${Number(last.uploaded)||0} ↓${Number(last.downloaded)||0} · ${Number(last.duration||0).toFixed(1)}s${errs.length?` · <span style="color:#ff6b6b">${errs.length} 错误</span>`:''}`:'暂无同步记录';
  const rows=[
    ['状态',badge(!!s.enabled,'已启用','未启用')+badge(s.configured!==false&&!!s.endpoint,'已配置','未配置')+badge(!!s.running,'守护运行中','守护未运行')],
    ['目标',s.endpoint?`<span style="font-family:monospace;font-size:12px">${esc(s.endpoint)}</span> · bucket <b>${esc(s.bucket||'—')}</b> · 前缀 ${esc(s.prefix||'—')}<br><span style="color:var(--dim);font-size:12px">本地: ${esc(s.local_path||'—')}</span>`:`<span style="color:var(--dim)">${esc(s.message||'未配置 endpoint')}</span>`],
    ['自动同步',s.auto_sync?`每 ${Number(s.sync_interval)||0}s`:'关闭'],
    ['最近同步',lastTxt]];
  box.innerHTML=`<table class="fleet-table"><tbody>`+rows.map(x=>`<tr><td style="color:var(--dim);width:90px;vertical-align:top">${x[0]}</td><td>${x[1]}</td></tr>`).join('')+`</tbody></table>`
    +`<div style="margin-top:10px;display:flex;gap:10px">`
    +`<button class="btn sm primary" id="cs-sync-btn" onclick="cloudSyncNow(this)" ${_csDegraded?'disabled title="云存储同步未启动/未配置, 按钮不可用"':''}>🔄 立即同步</button>`
    +`<button class="btn sm" id="cs-test-btn" onclick="cloudSyncTest(this)" ${_csDegraded?'disabled title="云存储同步未启动/未配置, 按钮不可用"':''}>🔗 测试连接</button>`
    +(_csDegraded?`<span style="font-size:12px;color:var(--dim);align-self:center">${esc(s.message||'云存储同步未启动')} — 在 config.yaml cloud_storage 段启用后重启生效</span>`:'')
    +`</div>`;
}
async function cloudSyncNow(btn){
  if(btn){btn.disabled=true;btn.textContent='⏳ 同步中...';}
  try{
    const r=await apiFetch('/api/cloud-sync/sync',{method:'POST'});
    const d=await r.json().catch(()=>({}));
    if(!r.ok){showToast('同步失败: '+(d.detail||(r.status===503?'云存储同步未启动':r.status)),'err');return;}
    const errs=d.errors||[];
    showToast(errs.length?`⚠️ 同步完成但有 ${errs.length} 个错误: ↑${d.uploaded||0} ↓${d.downloaded||0}`:`☁️ 同步完成: ↑${d.uploaded||0} ↓${d.downloaded||0} (${Number(d.duration||0).toFixed(1)}s)`,errs.length?'err':'ok');
  }catch(e){showToast('同步请求异常: '+e.message,'err');}
  finally{loadCloudSync();}
}
async function cloudSyncTest(btn){
  if(btn){btn.disabled=true;btn.textContent='⏳ 测试中...';}
  try{
    const r=await apiFetch('/api/cloud-sync/test',{method:'POST'});
    const d=await r.json().catch(()=>({}));
    if(!r.ok){showToast('测试失败: '+(d.detail||(r.status===503?'云存储同步未启动':r.status)),'err');return;}
    if(d.ok)showToast(`☁️ 连接正常: ${d.bucket||''} @ ${d.endpoint||''}`,'ok');
    else showToast('连接失败: '+(d.error||'未知错误'),'err');
  }catch(e){showToast('测试请求异常: '+e.message,'err');}
  finally{loadCloudSync();}
}
async function refreshRotationPlan(){
  // R5: 多池轮换调度方案 (逐模型候选池优先级)
  const body=document.getElementById('rotation-body');
  if(!body)return;
  try{
    const r=await apiFetch('/api/resources/rotation');
    const d=await r.json();
    renderRotationPlan(body,d.plan||[]);
  }catch(e){renderRotationPlan(body,[]);}
}
function renderRotationPlan(body,plan){
  if(!(plan||[]).length){
    body.innerHTML='<tr><td colspan="3" style="text-align:center;color:var(--dim);padding:14px">暂无关联模型的资源池 — 配置向导中为资源池关联模型后生效</td></tr>';
    return;
  }
  body.innerHTML=plan.map(r=>{
    const pools=(r.pools||[]).map(p=>p.id===r.chosen
      ?`<b style="color:#4ade80">${esc(p.id)}⭐ ${p.priority}</b>`
      :`<span style="color:var(--dim)">${esc(p.id)} ${p.priority}</span>`).join(' · ');
    return `<tr>
      <td><b>${esc(r.model)}</b></td>
      <td style="color:var(--accent)">${esc(r.chosen||'-')}</td>
      <td>${pools}</td>
    </tr>`;
  }).join('');
}
function renderResAlerts(alerts){
  // R7: 到期/额度预警横幅 (level 3 红 / 2 橙 / 1 黄)
  const box=document.getElementById('res-alerts');
  if(!box)return;
  if(!(alerts||[]).length){box.innerHTML='';return;}
  const style={3:{bg:'rgba(239,68,68,.12)',bd:'var(--red)',icon:'🚨'},
               2:{bg:'rgba(230,162,60,.12)',bd:'#e6a23c',icon:'⚠️'},
               1:{bg:'rgba(234,179,8,.10)',bd:'#d4a017',icon:'🔔'}};
  const sorted=[...alerts].sort((a,b)=>(b.level||0)-(a.level||0));
  box.innerHTML=sorted.map(a=>{
    const s=style[a.level]||style[1];
    return `<div style="background:${s.bg};border:1px solid ${s.bd};border-radius:10px;padding:8px 12px;margin-bottom:8px;font-size:13px">${s.icon} <b>${esc(a.resource_id)}</b> · ${esc(a.message)}</div>`;
  }).join('');
}
async function checkResourceAlerts(){
  // R7: 手动触发一轮预警检查并刷新面板
  try{
    await apiFetch('/api/resources/alerts/check',{method:'POST'});
  }catch(e){console.error(e);}
  await refreshResources();
}
async function syncSecretsAll(){
  // S1: 手动将本机资源密钥 (加密) 推送到全部在线节点
  const r=await apiFetch('/api/secrets/sync-all',{method:'POST'});
  const d=await r.json();
  if(!r.ok){showToast('同步失败: '+(d.detail||d.message||''),'err');return;}
  const lines=(d.results||[]).map(x=>`${x.device_name||x.device_id||'?'}: ${x.ok?'✅':'❌'} ${x.detail||''}`);
  showToast(`🔑 密钥同步 ${d.pushed||0}/${d.total||0} 成功${lines.length?' — '+lines.join('；'):''}`,(d.pushed||0)?'ok':'err');
}
async function refreshCostByTask(){
  // R6: 成本分摊聚合 (按 task_id 分组)
  const body=document.getElementById('cost-body');
  if(!body)return;
  try{
    const r=await apiFetch('/api/resources/cost');const d=await r.json();
    const items=d.items||[];
    if(!items.length){
      body.innerHTML='<tr><td colspan="6" style="text-align:center;color:var(--dim);padding:16px">暂无记账记录</td></tr>';
      return;
    }
    body.innerHTML=items.map(it=>{
      const tid=it.task_id?esc(it.task_id):'<span style="color:var(--dim)">未归因 (聊天/系统调用)</span>';
      const cost=parseFloat(it.cost||0);
      const costTxt=cost>0?fmtMoney(cost):'<span style="color:var(--dim)">'+Number(it.tokens||0).toLocaleString()+' tok</span>';
      return `<tr>
        <td><b>${tid}</b></td>
        <td style="color:var(--dim)">${esc(it.project_id||'-')}</td>
        <td>${it.calls||0}</td>
        <td>${Number(it.tokens||0).toLocaleString()}</td>
        <td>${costTxt}</td>
        <td style="color:var(--dim)">${fmtTime(it.last_at)}</td>
      </tr>`;
    }).join('');
  }catch(e){console.error(e);}
}
async function probeBalances(){
  try{
    const r=await apiFetch('/api/resources/probe',{method:'POST'});const d=await r.json();
    const ok=(d.supported||0), total=(d.probed||0);
    alert(`余额探测完成: ${total} 个池, ${ok} 个成功`);
    await refreshResources();
  }catch(e){alert('余额探测失败: '+e.message||e);}
}
// ── 资源配置向导 (R4) ──
let resCatalog=[];let resProbeSupported=[];let resProbeHints={};let resPools=[];
// 全球模型精选 Top50 (参考榜单, 点击可加入关联模型)
const RES_TOP_MODELS=['gpt-5.2','gpt-5.2-pro','gpt-5.1','gpt-5.1-codex','o4-pro','claude-opus-4.6','claude-sonnet-4.6','claude-haiku-4.5','claude-opus-4.5','claude-sonnet-4.5','gemini-3-pro','gemini-3-flash','gemini-2.5-pro','gemini-2.5-flash','deepseek-chat','deepseek-reasoner','deepseek-coder','qwen3.8-max','qwen3.5-plus','qwen3-coder','GLM5.2','GLM-5','glm-4.7','kimi2.7','kimi-k2.5','kimi-k2','grok-4.1','grok-4','grok-code-fast','mistral-large-3','mixtral-next','llama-4-maverick','llama-4-scout','command-r-plus','jamba-2-pro','yi-lightning','step-3','minimax-m2','abab-7','hunyuan-turbo','ernie-5.0','spark-5','baichuan-4','doubao-2.0','seed-2.0-pro','internlm-3.5','phi-5','olmo-3','nova-pro','reka-core'];
function isTokenPlan(t){return t==='token_plan'||t==='coding_plan';}
// 模型选择弹窗徽章样式 (选中=绿色实框, 未选=淡色虚框)
const RES_PICK_ON='color:#4ade80;border:1px solid #4ade80;opacity:1';
const RES_PICK_OFF='opacity:.65;border:1px dashed var(--border)';
let resWizardHtml='';let resWizardGlobals=null;
async function showResourceConfig(){
  try{
    const r=await apiFetch('/api/resources/config');const d=await r.json();
    resCatalog=d.catalog||[];resProbeSupported=d.probe_supported||[];resProbeHints=d.probe_hints||{};
    const data=d.data||{};
    resPools=JSON.parse(JSON.stringify(data.resources||[]));
    const providers=[...new Set([...resCatalog.map(c=>c.provider),...resProbeSupported])];
    document.getElementById('modal').innerHTML=`<span class="modal-close" onclick="closeOverlayForce()">&times;</span>
    <h2>⚙️ 资源配置向导</h2>
    <p style="color:var(--dim);font-size:13px">${d.exists?'编辑现有配置':'首次配置 — 添加至少一个资源池即可启用预算跟踪'}${d.parse_error?' · <span style="color:var(--red)">现有文件解析失败: '+esc(d.parse_error)+'</span>':''}</p>
    <datalist id="res-providers">${providers.map(p=>`<option value="${esc(p)}">`).join('')}</datalist>
    <div class="modal-sec"><h3>全局选项</h3>
      <div class="form-row"><label>严格模式</label><input type="checkbox" id="rc-strict" ${data.strict?'checked':''}> <span style="color:var(--dim);font-size:12px">开启后未关联资源池的模型将被禁用</span></div>
      <div class="form-row"><label>Secretary 地址</label><input id="rc-secretary" value="${esc(data.secretary_url||'')}" placeholder="留空: 自动发现注入 (多数情况无需填写)">
        <div style="color:var(--dim);font-size:12px;line-height:1.6;margin-top:4px">仅多主机场景下、自动发现不可用时才需填写 (形如 http://192.168.x.x:45470): 本机用量将周期性上报到该 Secretary 汇总。<br>单机/Secretary 本机运行 → 留空即可, 记账已直接发生在本机。</div>
      </div>
    </div>
    <div class="modal-sec"><h3>资源池 (<span id="rc-count">0</span>)</h3>
      <div id="rc-pools"></div>
      <button class="btn" onclick="addResPool()">➕ 添加资源池</button>
    </div>
    <div class="modal-sec" style="display:flex;gap:8px">
      <button class="btn primary" onclick="saveResourceConfig()">💾 保存并生效</button>
      <button class="btn" onclick="closeOverlayForce()">取消</button>
      <span id="rc-msg" style="align-self:center;font-size:13px"></span>
    </div>`;
    renderResPools();
    document.getElementById('overlay').classList.add('show');
  }catch(e){alert('加载配置失败: '+(e.message||e));}
}
function resPoolRow(p,i){
  const cat=resCatalog.find(c=>c.provider===p.provider);
  const modelBadges=cat?(cat.models||[]).map(m=>`<span class="badge worker" style="cursor:pointer" onclick="appendResModel(${i},'${esc(m)}')">${esc(m)}</span>`).join(' '):'';
  const supported=resProbeSupported.includes((p.provider||'').toLowerCase());
  return `<div style="border:1px solid var(--border);border-radius:12px;padding:12px;margin-bottom:12px" id="rc-pool-${i}">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
      <b style="font-size:13px">资源池 #${i+1}</b>
      <span class="badge archived" style="cursor:pointer" onclick="removeResPool(${i})">🗑️ 删除</span>
    </div>
    <div class="form-row"><label>ID</label><input id="rp-id-${i}" value="${esc(p.id||'')}" placeholder="如 deepseek-token-pack"></div>
    <div class="form-row"><label>服务商</label><input id="rp-provider-${i}" list="res-providers" value="${esc(p.provider||'')}" placeholder="deepseek / siliconflow ..." onchange="collectAndRender()"></div>
    <div class="form-row"><label>计划类型</label><select id="rp-plan-${i}" onchange="collectAndRender()">
      <option value="payg" ${p.plan_type==='payg'?'selected':''}>按量付费 (预算金额)</option>
      <option value="token_plan" ${p.plan_type==='token_plan'?'selected':''}>token 包 (一次性额度)</option>
      <option value="coding_plan" ${p.plan_type==='coding_plan'?'selected':''}>编程订阅 (周期重置)</option></select></div>
    ${isTokenPlan(p.plan_type)?`
    <div class="form-row"><label>额度</label><input id="rp-quota-${i}" type="number" step="any" value="${p.quota?(Number(p.quota)/1e7):''}" placeholder="单位: 千万 tokens (如 50 = 5亿)">
      <span style="color:var(--dim);font-size:12px;white-space:nowrap">千万 tokens</span></div>`:`
    <div class="form-row"><label>额度</label><input id="rp-quota-${i}" type="number" step="any" value="${p.quota??''}" placeholder="预算金额">
      <select id="rp-unit-${i}" style="width:90px"><option value="usd" ${p.unit==='usd'?'selected':''}>USD</option><option value="cny" ${p.unit==='cny'?'selected':''}>CNY</option></select></div>`}
    <div class="form-row"><label>关联模型</label><input id="rp-models-${i}" value="${esc((Array.isArray(p.models)?p.models:String(p.models||'').split(',').map(s=>s.trim()).filter(Boolean)).join(', '))}" placeholder="点击 📋 选择按钮挑选模型">
      <button class="btn" onclick="openModelPicker(${i})">📋 选择</button></div>
    ${modelBadges?`<div style="display:flex;flex-wrap:wrap;gap:4px;margin:-4px 0 6px 0">${modelBadges}</div>`:''}
    <div class="form-row"><label>API Key</label><input id="rp-key-${i}" type="password" value="${esc(p.api_key||'')}" placeholder="填入可一键测试余额 (仅存本机)">
      <button class="btn" onclick="testResKey(${i})">🔍 测试</button></div>
    <div id="rp-keymsg-${i}" style="font-size:12px;margin:-4px 0 6px 0;min-height:16px"></div>
    <div class="form-row"><label>或环境变量名</label><input id="rp-keyenv-${i}" value="${esc(p.api_key_env||'')}" placeholder="DEEPSEEK_API_KEY (与直填二选一)"></div>
    <div class="form-row"><label>备注</label><input id="rp-note-${i}" value="${esc(p.note||'')}" placeholder="套餐名称/购买日期"></div>
    ${supported?'':`<div style="font-size:12px;color:#e6a23c">ℹ️ ${esc(resProbeHints[(p.provider||'').toLowerCase()]||'该服务商无自动余额接口, 控制台查看')}</div>`}
  </div>`;
}
function renderResPools(){
  // 纯渲染 (不重新收集表单, 避免新加空行被过滤)
  document.getElementById('rc-pools').innerHTML=resPools.length?resPools.map((p,i)=>resPoolRow(p,i)).join(''):'<p style="color:var(--dim);font-size:13px">尚未添加资源池</p>';
  document.getElementById('rc-count').textContent=resPools.length;
}
function collectResPools(keepEmpty){
  // keepEmpty=true 时保留空行 (编辑中); 保存时才过滤全空行
  const rows=resPools.map((p,i)=>{
    const g=id=>{const el=document.getElementById(id);return el?el.value:(p[ id.replace(/^rp-(\w+)-\d+$/,'$1') ]??'');};
    const el=id=>document.getElementById(id);
    if(!el('rp-id-'+i))return p;  // 行未渲染 (首次)
    const plan=el(`rp-plan-${i}`).value;
    const tok=isTokenPlan(plan);  // token 类计划: UI 按千万输入, 存储为原始 token 数
    const qRaw=g(`rp-quota-${i}`);
    const unitEl=el(`rp-unit-${i}`);
    return {id:g(`rp-id-${i}`),provider:g(`rp-provider-${i}`),plan_type:plan,
      quota:tok&&qRaw!==''?Number(qRaw)*1e7:qRaw,unit:tok?'token':(unitEl?unitEl.value:'cny'),
      models:g(`rp-models-${i}`),api_key:g(`rp-key-${i}`),api_key_env:g(`rp-keyenv-${i}`),
      note:g(`rp-note-${i}`),status:p.status||'active',alert_threshold:p.alert_threshold??0.8,
      billing_period:p.billing_period,expire_at:p.expire_at,renew_at:p.renew_at,period_days:p.period_days};
  });
  return keepEmpty?rows:rows.filter(p=>p.id||p.provider||p.quota);
}
function collectAndRender(){resPools=collectResPools(true);renderResPools();}
function addResPool(){resPools=collectResPools(true);resPools.push({plan_type:'payg',unit:'cny',status:'active',alert_threshold:0.8});renderResPools();}
function removeResPool(i){resPools=collectResPools(true);resPools.splice(i,1);renderResPools();}
function appendResModel(i,m){const el=document.getElementById('rp-models-'+i);if(!el)return;const cur=el.value.trim();el.value=cur?(cur.replace(/,?\s*$/,', ')+m):m;}
function resPoolSelected(i){
  const el=document.getElementById('rp-models-'+i);
  return el?el.value.split(',').map(s=>s.trim()).filter(Boolean):[];
}
function openModelPicker(i){
  resPools=collectResPools(true);
  const p=resPools[i]||{};
  const cat=resCatalog.find(c=>c.provider===p.provider);
  const catModels=cat?(cat.models||[]):[];
  const selected=resPoolSelected(i);
  // 保存向导 DOM 与全局选项 (picker 会替换 #modal 内容, 关闭时恢复)
  resWizardHtml=document.getElementById('modal').innerHTML;
  resWizardGlobals={strict:document.getElementById('rc-strict')?.checked??false,
    secretary_url:document.getElementById('rc-secretary')?.value??''};
  const topModels=RES_TOP_MODELS.filter(m=>!catModels.includes(m));
  const selSet=new Set(selected);
  const badge=(m)=>`<span class="badge${selSet.has(m)?' mp-picked':''}" style="cursor:pointer;margin:3px;${selSet.has(m)?RES_PICK_ON:RES_PICK_OFF}" data-model="${esc(m)}" onclick="togglePickModel(this)">${esc(m)}</span>`;
  document.getElementById('modal').innerHTML=`<span class="modal-close" onclick="closeModelPicker(${i})">&times;</span>
  <h2>📋 选择关联模型 — 资源池 #${i+1}</h2>
  <p style="color:var(--dim);font-size:12px;margin-bottom:8px">点击徽章选中/取消 (已选高亮为绿色), 确认后回填到关联模型输入框</p>
  <div class="form-row"><input id="mp-filter" placeholder="🔍 过滤模型名..." oninput="filterPickBadges()"></div>
  <div class="modal-sec"><h3>该服务商目录 (${catModels.length}${cat?'':' — 未识别服务商'})</h3>
    <div style="display:flex;flex-wrap:wrap">${catModels.map(badge).join('')||'<span style="color:var(--dim);font-size:12px">无目录模型</span>'}</div></div>
  <div class="modal-sec"><h3>🌐 全球精选 Top${topModels.length} (参考榜单)</h3>
    <div style="display:flex;flex-wrap:wrap">${topModels.map(badge).join('')}</div></div>
  <div class="modal-sec" style="display:flex;gap:8px">
    <button class="btn primary" onclick="confirmPickModels(${i})">✅ 确认选择</button>
    <button class="btn" onclick="closeModelPicker(${i})">取消</button>
    <span style="align-self:center;font-size:12px;color:var(--dim)">已选 <b id="mp-count">${selected.length}</b> 个</span>
  </div>`;
  document.getElementById('overlay').classList.add('show');
}
function togglePickModel(el){
  const on=el.classList.toggle('mp-picked');
  el.style.cssText='cursor:pointer;margin:3px;'+(on?RES_PICK_ON:RES_PICK_OFF);
  const c=document.getElementById('mp-count');if(c)c.textContent=document.querySelectorAll('#modal .badge.mp-picked').length;
}
function filterPickBadges(){
  const q=(document.getElementById('mp-filter').value||'').toLowerCase();
  document.querySelectorAll('#modal .badge').forEach(b=>{b.style.display=(b.textContent||'').toLowerCase().includes(q)?'':'none';});
}
function confirmPickModels(i){
  const picked=[...document.querySelectorAll('#modal .badge.mp-picked')].map(b=>b.dataset.model);
  if(resPools[i])resPools[i].models=picked;  // 直接更新内存态, 重渲染后回填输入框
  closeModelPicker(i);
}
function closeModelPicker(i){
  // 恢复向导 DOM (picker 已替换 #modal) + 全局选项 + 重渲染资源池卡片
  if(resWizardHtml){document.getElementById('modal').innerHTML=resWizardHtml;}
  if(resWizardGlobals){
    const s=document.getElementById('rc-strict');if(s)s.checked=resWizardGlobals.strict;
    const u=document.getElementById('rc-secretary');if(u)u.value=resWizardGlobals.secretary_url;
  }
  renderResPools();
  document.getElementById('overlay').classList.add('show');
}
async function testResKey(i){
  const provider=(document.getElementById('rp-provider-'+i).value||'').trim();
  const key=(document.getElementById('rp-key-'+i).value||'').trim();
  const msg=document.getElementById('rp-keymsg-'+i);
  if(!provider||!key){msg.innerHTML='<span style="color:var(--red)">请先填写服务商与 API Key</span>';return;}
  msg.innerHTML='<span style="color:var(--dim)">测试中...</span>';
  try{
    const r=await apiFetch('/api/resources/test-key',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({provider,api_key:key})});
    const d=await r.json();
    if(d.supported)msg.innerHTML=`<span style="color:var(--green)">✅ 余额: ${d.balance} ${esc(d.currency||'')}</span>`;
    else msg.innerHTML=`<span style="color:#e6a23c">⚠️ ${esc(d.error||'无法探测')}${d.hint?' — '+esc(d.hint):''}</span>`;
  }catch(e){msg.innerHTML='<span style="color:var(--red)">请求失败: '+esc(e.message||e)+'</span>';}
}
async function saveResourceConfig(){
  resPools=collectResPools();
  const config={strict:document.getElementById('rc-strict').checked,
    secretary_url:document.getElementById('rc-secretary').value.trim(),
    resources:resPools};
  const msg=document.getElementById('rc-msg');
  try{
    const r=await apiFetch('/api/resources/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({config})});
    const d=await r.json();
    if(!r.ok){msg.innerHTML='<span style="color:var(--red)">'+esc(d.detail||'保存失败')+'</span>';return;}
    msg.innerHTML='<span style="color:var(--green)">✅ 已保存, '+d.pools+' 个资源池'+(d.enabled?'已生效':'未生效')+'</span>';
    setTimeout(async()=>{closeOverlayForce();await refreshResources();},800);
  }catch(e){msg.innerHTML='<span style="color:var(--red)">'+esc(e.message||e)+'</span>';}
}
function fmtMoney(v){
  if(v===null||v===undefined)return '';
  const n=Number(v);
  return n>=10000?(n/10000).toFixed(2)+'万':n.toFixed(2);
}
function fmtQuota(v,unit){
  if(unit==='token'){if(v>=1e8)return (v/1e8).toFixed(1)+'亿';if(v>=1e4)return (v/1e4).toFixed(1)+'万';return Number(v).toLocaleString();}
  if(unit==='usd')return '$'+Number(v).toFixed(2);
  if(unit==='cny')return '¥'+Number(v).toFixed(2);
  return v;
}
function renderServers(servers){
  const grid=document.getElementById('server-grid');
  document.getElementById('srv-total').textContent=servers.length;
  if(!servers.length){grid.innerHTML='<div class="empty"><div class="icon">🔌</div><p>未注册MCP Server</p></div>';return;}
  grid.innerHTML=servers.map(s=>`<div class="card ${s.connected?'':'offline'}" onclick="showServerDetail('${esc(s.name)}')">
    <div class="card-head"><div class="card-title">🔌 ${esc(s.name)}<span class="badge ${s.connected?'idle':'archived'}">${s.connected?'已连接':'断开'}</span></div></div>
    <div class="card-meta">传输: <span>${esc(s.transport)}</span> | 工具数: <span>${s.tool_count||0}</span><br>连接时间: <span>${fmtTime(s.connected_at)}</span></div>
  </div>`).join('');
}
function renderTools(data){
  const grid=document.getElementById('tool-grid');
  const tools=data.tools||[];
  document.getElementById('tool-total').textContent=tools.length;
  if(!tools.length){grid.innerHTML='';return;}
  grid.innerHTML=tools.map(t=>`<div class="card" onclick='callTool("${esc(t.name)}")'>
    <div class="card-head"><div class="card-title">🔧 ${esc(t.name)}</div><span class="badge idle">${esc(t.source_server||'')}</span></div>
    <div class="card-meta">${esc((t.description||'').slice(0,100))}</div>
  </div>`).join('');
}
function showServerDetail(name){
  document.getElementById('modal').innerHTML=`<span class="modal-close" onclick="closeOverlayForce()">&times;</span><h2>🔌 ${esc(name)}</h2>
  <div class="modal-sec"><h3>操作</h3><button class="btn" onclick="apiFetch('/tools/servers/${encodeURIComponent(name)}',{method:'DELETE'}).then(()=>{refreshTools();closeOverlayForce();})">🗑️ 注销Server</button></div>`;
  document.getElementById('overlay').classList.add('show');
}
function showRegisterServer(){
  document.getElementById('modal').innerHTML=`<span class="modal-close" onclick="closeOverlayForce()">&times;</span><h2>注册 MCP Server</h2>
  <div class="modal-sec">
    <div class="form-row"><label>Server名称</label><input id="srv-name" placeholder="filesystem"></div>
    <div class="form-row"><label>传输方式</label><select id="srv-transport"><option value="stdio">stdio</option><option value="http">http</option></select></div>
    <div class="form-row"><label>命令 (stdio)</label><input id="srv-command" placeholder="npx"></div>
    <div class="form-row"><label>参数 (逗号分隔)</label><input id="srv-args" placeholder="@modelcontextprotocol/server-filesystem,/tmp"></div>
    <div class="form-row"><label>URL (http)</label><input id="srv-url" placeholder="http://192.168.1.100:3000/mcp"></div>
    <button class="btn primary" onclick="registerServer()">注册</button>
  </div>`;
  document.getElementById('overlay').classList.add('show');
}
async function registerServer(){
  const name=document.getElementById('srv-name').value.trim();if(!name){alert('请输入名称');return;}
  const transport=document.getElementById('srv-transport').value;
  const config={transport};
  if(transport==='stdio'){config.command=document.getElementById('srv-command').value.trim();const args=document.getElementById('srv-args').value.trim();config.args=args?args.split(',').map(s=>s.trim()):[];}
  else{config.url=document.getElementById('srv-url').value.trim();}
  try{const r=await apiFetch('/tools/servers',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name,config})});const d=await r.json();if(d.ok){refreshTools();closeOverlayForce();}else{alert('注册失败');}}catch(e){alert('失败:'+e.message);}
}
function callTool(name){
  document.getElementById('modal').innerHTML=`<span class="modal-close" onclick="closeOverlayForce()">&times;</span><h2>🔧 调用 ${esc(name)}</h2>
  <div class="modal-sec"><h3>参数 (JSON)</h3><div class="form-row"><textarea id="tool-args" style="width:100%;min-height:120px" placeholder='{"path": "/tmp/test.txt"}'></textarea></div>
  <button class="btn primary" onclick='execTool("${esc(name)}")'>执行</button></div>
  <div class="modal-sec" id="tool-result" style="display:none"><h3>结果</h3><pre id="tool-output"></pre></div>`;
  document.getElementById('overlay').classList.add('show');
}
async function execTool(name){
  const argsText=document.getElementById('tool-args').value.trim();
  let args={};try{args=argsText?JSON.parse(argsText):{};}catch(e){alert('JSON格式错误');return;}
  try{const r=await apiFetch('/tools/call',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({tool_name:name,arguments:args})});const d=await r.json();
    document.getElementById('tool-result').style.display='block';
    document.getElementById('tool-output').textContent=JSON.stringify(d,null,2);
  }catch(e){alert('调用失败:'+e.message);}
}

