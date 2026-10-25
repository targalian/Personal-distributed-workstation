// ── 技能库管理 ──
let allSkills=[];
async function refreshSkills(){
  const filter=document.getElementById('skill-role-filter');
  const cat=filter?filter.value:'';
  try{
    const url=cat?`/api/station/skills?category=${cat}`:'/api/station/skills';
    const r=await apiFetch(url);const skills=await r.json();
    allSkills=skills;
    const body=document.getElementById('skill-body');
    document.getElementById('skill-total').textContent=skills.length;
    let totalAssign=0;
    if(!skills.length){body.innerHTML='<tr><td colspan="6" style="text-align:center;color:var(--dim);padding:30px">暂无技能，点击「扫描新技能」从 skills/ 目录加载</td></tr>';return;}
    body.innerHTML=skills.map(s=>{
      const assignCount=(s.assignments||0);
      totalAssign+=assignCount;
      const isMarket=(s.origin||'builtin')==='market';
      const originBadge=isMarket?'<span style="display:inline-block;background:rgba(255,165,0,.15);color:#ffa500;border:1px solid rgba(255,165,0,.4);border-radius:10px;padding:1px 8px;font-size:11px;margin-left:6px">第三方</span>':'<span style="display:inline-block;background:var(--surface2);color:var(--dim);border:1px solid var(--border);border-radius:10px;padding:1px 8px;font-size:11px;margin-left:6px">内置</span>';
      const tagsHtml=(s.tags||[]).map(t=>`<span style="display:inline-block;background:var(--surface2);border:1px solid var(--border);border-radius:10px;padding:1px 8px;font-size:11px;margin:1px">${esc(t)}</span>`).join('');
      const accessHtml=(s.default_access||[]).map(a=>`<span style="color:${a==='all'?'var(--green)':'var(--accent)'};font-size:12px">${esc(a)}</span>`).join(', ');
      const uninstallBtn=isMarket?`<button class="btn sm danger" onclick="event.stopPropagation();uninstallSkill('${s.skill_id}')">卸载</button>`:'';
      return `<tr style="cursor:pointer" onclick="viewSkill('${s.skill_id}')">
        <td><b>${esc(s.name||s.skill_id)}</b>${originBadge}<br><span style="font-size:11px;color:var(--dim)">${esc(s.description||'').substring(0,60)}${s.description&&s.description.length>60?'...':''}</span></td>
        <td><span style="color:var(--accent);font-size:12px">${esc(s.category||'general')}</span></td>
        <td>${tagsHtml||'<span style="color:var(--dim)">-</span>'}</td>
        <td>${accessHtml}</td>
        <td><span style="color:var(--dim);font-size:12px">v${esc(s.version||'1.0')}</span></td>
        <td><button class="btn sm" onclick="event.stopPropagation();assignSkill('${s.skill_id}')">分配</button> ${uninstallBtn}</td>
      </tr>`;
    }).join('');
    document.getElementById('skill-assign-count').textContent=totalAssign;
  }catch(e){console.error('技能列表加载失败:',e);}
}
async function viewSkill(skillId){
  try{
    const r=await apiFetch(`/api/station/skills/${skillId}`);const d=await r.json();
    const assignsHtml=(d.assignments||[]).map(a=>`<div style="display:flex;align-items:center;gap:8px;padding:6px 0;border-bottom:1px solid var(--border)"><span style="font-size:12px;color:var(--accent)">${esc(a.assignee_type)}</span><span>${esc(a.assignee_id)}</span><button class="btn sm danger" onclick="revokeSkill('${skillId}','${a.assignee_type}','${a.assignee_id}')">撤销</button></div>`).join('');
    document.getElementById('modal').innerHTML=`<span class="modal-close" onclick="closeOverlayForce()">&times;</span>
      <h2>${esc(d.name||skillId)}</h2>
      <p style="color:var(--dim);font-size:13px">${esc(d.description||'')}</p>
      <div class="kv-grid" style="margin:12px 0">
        <span class="k">分类</span><span class="v">${esc(d.category||'general')}</span>
        <span class="k">版本</span><span class="v">v${esc(d.version||'1.0')}</span>
        <span class="k">默认权限</span><span class="v">${(d.default_access||[]).join(', ')}</span>
      </div>
      <div class="modal-sec"><h3>技能内容</h3><pre style="background:var(--bg);border:1px solid var(--border);border-radius:8px;padding:12px;overflow:auto;max-height:300px;font-size:12px;line-height:1.5;white-space:pre-wrap">${esc(d.content||'(无内容)')}</pre></div>
      ${d.reference?`<div class="modal-sec"><h3>扩展参考</h3><pre style="background:var(--bg);border:1px solid var(--border);border-radius:8px;padding:12px;overflow:auto;max-height:200px;font-size:12px;line-height:1.5;white-space:pre-wrap">${esc(d.reference)}</pre></div>`:''}
      <div class="modal-sec"><h3>权限分配 (${(d.assignments||[]).length})</h3>${assignsHtml||'<p style="color:var(--dim)">无分配记录</p>'}
        <div style="margin-top:12px"><button class="btn sm primary" onclick="assignSkill('${skillId}')">+ 添加分配</button></div>
      </div>`;
    document.getElementById('overlay').classList.add('show');
  }catch(e){alert('失败:'+e.message);}
}
async function assignSkill(skillId){
  const type=await showInputDialog('分配类型 (role/agent/host):','role');if(!type)return;
  const id=await showInputDialog('分配目标 ID (如: worker, secretary, agent_id, device_id):');if(!id)return;
  try{
    const r=await apiFetch(`/api/station/skills/${skillId}/assign`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({assignee_type:type,assignee_id:id})});
    const d=await r.json();if(d.ok){refreshSkills();viewSkill(skillId);}else{alert('失败: '+(d.detail||'未知错误'));}
  }catch(e){alert('失败:'+e.message);}
}
async function revokeSkill(skillId,assigneeType,assigneeId){
  if(!confirm(`确定要撤销 ${assigneeType}:${assigneeId} 对技能 ${skillId} 的访问权限吗？`))return;
  try{
    const r=await apiFetch(`/api/station/skills/${skillId}/assign?assignee_type=${assigneeType}&assignee_id=${assigneeId}`,{method:'DELETE'});
    const d=await r.json();if(d.ok){refreshSkills();viewSkill(skillId);}else{alert('失败: '+(d.detail||'未知错误'));}
  }catch(e){alert('失败:'+e.message);}
}
async function scanSkills(){
  try{
    const r=await apiFetch('/api/station/skills/scan');const d=await r.json();
    refreshSkills();
    alert(`扫描完成: ${d.scanned||0} 个技能已注册/更新`);
  }catch(e){alert('失败:'+e.message);}
}
async function showSkillMarket(){
  try{
    const r=await apiFetch('/api/station/skills/market');const items=await r.json();
    const modal=document.getElementById('modal');
    if(!items.length){modal.innerHTML=`<span class="modal-close" onclick="closeOverlayForce()">&times;</span><h2>🛒 插件市场</h2><div class="empty"><div class="icon">🛒</div><p>市场暂无可用插件</p><p class="hint">将第三方 SKILL.md 插件包放入项目根 skills_market/ 目录即可在此浏览安装</p></div>`;}
    else{
      modal.innerHTML=`<span class="modal-close" onclick="closeOverlayForce()">&times;</span><h2>🛒 插件市场</h2><p style="color:var(--dim);font-size:13px">第三方插件安装后默认仅 station 可用, 需在技能库中显式分配角色才会分发到 Worker/Agent</p>${items.map(m=>{
        const inst=m.installed?(m.installed==='market'?'<span class="badge active">已安装</span>':'<span class="badge suspended">内置同名</span>'):'<span class="badge">未安装</span>';
        const invalid=m.valid?'':`<span style="color:var(--red);font-size:12px">⚠ ${esc(m.error||'')}</span>`;
        const btn=m.valid?`<button class="btn sm primary" onclick="installMarketSkill('${esc(m.skill_id)}')">${m.installed?'🔄 更新':'⬇ 安装'}</button>`:'';
        return `<div class="card" style="cursor:default"><div class="card-head"><div class="card-title">📦 ${esc(m.name||m.skill_id)} ${inst}</div></div><div class="card-meta">${esc(m.description||'')}<br>分类: <span style="color:var(--accent)">${esc(m.category||'general')}</span> · v${esc(m.version||'1.0')} · ${m.size_kb}KB ${invalid}</div><div style="display:flex;gap:8px;margin-top:8px">${btn}</div></div>`;
      }).join('')}`;
    }
    document.getElementById('overlay').classList.add('show');
  }catch(e){alert('失败:'+e.message);}
}
async function installMarketSkill(skillId){
  try{
    const r=await apiFetch('/api/station/skills/market/install',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({skill_id:skillId})});
    const d=await r.json();
    if(d.ok){showToast(`🛒 插件「${skillId}」已安装`,'ok');closeOverlayForce();refreshSkills();}
    else{alert('安装失败: '+(d.detail||d.message||'未知错误'));}
  }catch(e){alert('失败:'+e.message);}
}
async function uninstallSkill(skillId){
  if(!confirm(`确定要卸载第三方插件「${skillId}」吗？将删除其文件与全部权限分配。`))return;
  try{
    const r=await apiFetch(`/api/station/skills/${skillId}`,{method:'DELETE'});
    const d=await r.json();
    if(d.ok){showToast(`🗑️ 插件「${skillId}」已卸载`,'info');refreshSkills();}
    else{alert('卸载失败: '+(d.detail||d.message||'未知错误'));}
  }catch(e){alert('失败:'+e.message);}
}

// ── 手机通道 (Bot) ──
let botChannels=[];
async function refreshBotChannels(){
  try{
    const r=await apiFetch('/api/station/bot/channels');botChannels=await r.json();
    const list=document.getElementById('bot-channel-list');
    document.getElementById('bot-channel-count').textContent=`共 ${botChannels.length} 个通道`;
    if(!botChannels.length){list.innerHTML='<div class="empty"><div class="icon">📱</div><p>暂无通道配置</p><p class="hint">添加企业微信群机器人或 Telegram Bot</p></div>';return;}
    list.innerHTML=botChannels.map(c=>{
      const typeName=c.channel_type==='wechat_webhook'?'企业微信群机器人':'Telegram Bot';
      const icon=c.channel_type==='wechat_webhook'?'💬':'✈️';
      const statusBadge=c.enabled?'<span class="badge active">启用</span>':'<span class="badge suspended">禁用</span>';
      const detail=c.channel_type==='wechat_webhook'?`Webhook: ${c.webhook_url}`:`Token: ${c.bot_token} | Chat: ${c.chat_id}`;
      return `<div class="card" style="cursor:default">
        <div class="card-head"><div class="card-title">${icon} ${typeName} ${statusBadge}</div></div>
        <div class="card-meta">${esc(detail)}<br>最低优先级: <span style="color:var(--accent)">${esc(c.min_priority)}</span></div>
        <div style="display:flex;gap:8px;margin-top:8px">
          <button class="btn sm primary" onclick="testBotChannel('${c.channel_type}')">📤 测试</button>
          <button class="btn sm" onclick="editBotChannel('${c.channel_type}')">✏️ 编辑</button>
          <button class="btn sm danger" onclick="removeBotChannel('${c.channel_type}')">🗑️ 删除</button>
        </div>
      </div>`;
    }).join('');
  }catch(e){console.error('Bot通道加载失败:',e);}
}
function showAddBotChannel(){
  document.getElementById('modal').innerHTML=`<span class="modal-close" onclick="closeOverlayForce()">&times;</span><h2>添加 Bot 通道</h2>
  <div class="modal-sec">
    <div class="form-row"><label>通道类型</label>
      <select id="bot-type" onchange="toggleBotFields()">
        <option value="wechat_webhook">企业微信群机器人</option>
        <option value="telegram">Telegram Bot</option>
      </select>
    </div>
    <div class="form-row" id="field-webhook"><label>Webhook URL</label><input id="bot-webhook" placeholder="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx"></div>
    <div class="form-row" id="field-token" style="display:none"><label>Bot Token</label><input id="bot-token" placeholder="123456789:ABCdefGHI..."></div>
    <div class="form-row" id="field-chatid" style="display:none"><label>Chat ID</label><input id="bot-chatid" placeholder="你的 Telegram Chat ID"></div>
    <div class="form-row"><label>最低推送优先级</label>
      <select id="bot-priority"><option value="low">全部 (含低)</option><option value="normal" selected>常规及以上</option><option value="high">仅重要</option></select>
    </div>
    <div class="form-row"><label><input type="checkbox" id="bot-enabled" checked> 启用此通道</label></div>
  </div>
  <div class="modal-sec"><button class="btn primary" onclick="saveBotChannel()">💾 保存</button></div>`;
  document.getElementById('overlay').classList.add('show');
}
function toggleBotFields(){
  const type=document.getElementById('bot-type').value;
  document.getElementById('field-webhook').style.display=type==='wechat_webhook'?'':'none';
  document.getElementById('field-token').style.display=type==='telegram'?'':'none';
  document.getElementById('field-chatid').style.display=type==='telegram'?'':'none';
}
function editBotChannel(type){
  const c=botChannels.find(x=>x.channel_type===type);if(!c)return;
  document.getElementById('modal').innerHTML=`<span class="modal-close" onclick="closeOverlayForce()">&times;</span><h2>编辑 ${type==='wechat_webhook'?'企业微信':'Telegram'} Bot</h2>
  <div class="modal-sec">
    <div class="form-row" id="field-webhook" style="${type==='wechat_webhook'?'':'display:none'}"><label>Webhook URL</label><input id="bot-webhook" value="${esc(c.webhook_url||'')}" placeholder="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx"></div>
    <div class="form-row" id="field-token" style="${type==='telegram'?'':'display:none'}"><label>Bot Token</label><input id="bot-token" placeholder="保持不变" ></div>
    <div class="form-row" id="field-chatid" style="${type==='telegram'?'':'display:none'}"><label>Chat ID</label><input id="bot-chatid" value="${esc(c.chat_id||'')}" placeholder="Chat ID"></div>
    <div class="form-row"><label>最低推送优先级</label>
      <select id="bot-priority"><option value="low" ${c.min_priority==='low'?'selected':''}>全部 (含低)</option><option value="normal" ${c.min_priority==='normal'?'selected':''}>常规及以上</option><option value="high" ${c.min_priority==='high'?'selected':''}>仅重要</option></select>
    </div>
    <div class="form-row"><label><input type="checkbox" id="bot-enabled" ${c.enabled?'checked':''}> 启用此通道</label></div>
  </div>
  <div class="modal-sec"><button class="btn primary" onclick="saveBotChannel('${type}')">💾 保存</button></div>`;
  document.getElementById('overlay').classList.add('show');
}
async function saveBotChannel(existingType){
  const type=existingType||document.getElementById('bot-type').value;
  const payload={
    channel_type:type,
    enabled:document.getElementById('bot-enabled').checked,
    webhook_url:document.getElementById('bot-webhook')?.value||'',
    bot_token:document.getElementById('bot-token')?.value||'',
    chat_id:document.getElementById('bot-chatid')?.value||'',
    min_priority:document.getElementById('bot-priority').value,
  };
  try{
    const r=await apiFetch('/api/station/bot/channels',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
    const d=await r.json();if(d.ok){closeOverlayForce();refreshBotChannels();alert(d.message||'已保存');}else{alert('失败: '+(d.detail||'未知错误'));}
  }catch(e){alert('失败:'+e.message);}
}
async function removeBotChannel(type){
  if(!confirm(`确定要删除 ${type} 通道吗？`))return;
  try{
    const r=await apiFetch(`/api/station/bot/channels/${type}`,{method:'DELETE'});
    const d=await r.json();if(d.ok){refreshBotChannels();}else{alert('失败: '+(d.detail||'未知错误'));}
  }catch(e){alert('失败:'+e.message);}
}
async function testBotChannel(type){
  try{
    const r=await apiFetch(`/api/station/bot/test/${type}`,{method:'POST'});
    const d=await r.json();
    alert(d.ok?d.message||'测试成功':d.detail||'测试失败');
  }catch(e){alert('失败:'+e.message);}
}

