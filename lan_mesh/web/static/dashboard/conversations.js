// ── 多对话管理 ──
let _activeConvId='';
let _convList=[];

async function loadConversations(){
  try{
    const r=await apiFetch('/api/conversations');
    const d=await r.json();
    _convList=d.conversations||[];
    renderConvList();
    // 若无活跃对话, 选第一个
    if(!_activeConvId&&_convList.length){
      switchConversation(_convList[0].id);
    }
  }catch(e){console.error('加载对话列表失败:',e);}
}
function renderConvList(){
  const el=document.getElementById('conv-list');
  if(!el)return;
  el.innerHTML=_convList.map(c=>{
    const active=c.id===_activeConvId?'active':'';
    const t=c.updated_at?new Date(c.updated_at*1000).toLocaleString('zh-CN',{month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit'}):'';
    return `<div class="conv-item ${active}" onclick="switchConversation('${c.id}')"><div class="conv-title">${esc(c.title||'未命名')}</div><div class="conv-time">${t}</div></div>`;
  }).join('');
}
async function switchConversation(convId){
  _activeConvId=convId;
  closePMThread();
  renderConvList();
  // 更新标题
  const meta=_convList.find(c=>c.id===convId);
  document.getElementById('chat-conv-title').textContent=meta?meta.title:'对话';
  document.getElementById('chat-conv-project').textContent=meta&&meta.project_id?`项目: ${meta.project_id}`:'';
  await loadChatHistory();
  loadPMThreads();
}
async function createConversation(){
  const title=await showInputDialog('对话标题:','新对话');
  if(!title)return;
  try{
    const r=await apiFetch('/api/conversations',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({title})});
    const conv=await r.json();
    if(!r.ok){showToast('创建失败:'+(conv.detail||conv.message||`HTTP ${r.status}`),'err');return;}
    if(conv.id){
      await loadConversations();
      switchConversation(conv.id);
      showToast(`对话「${title}」已创建`,'info');
    }
  }catch(e){showToast('创建失败:'+e.message,'err');}
}
async function deleteCurrentConv(){
  if(!_activeConvId)return;
  if(!confirm('确定删除当前对话?'))return;
  try{
    await apiFetch(`/api/conversations/${_activeConvId}`,{method:'DELETE'});
    _activeConvId='';
    await loadConversations();
    if(_convList.length)switchConversation(_convList[0].id);
    else{
      document.getElementById('chat-messages').innerHTML='<div class="empty"><div class="icon">💬</div><p>点击“新建对话”开始</p></div>';
      document.getElementById('chat-conv-title').textContent='无对话';
    }
    showToast('对话已删除','info');
  }catch(e){showToast('删除失败:'+e.message,'err');}
}

// ── 方案C: PM 线程 (L2 层) ──
let _pmThreads=[];          // 当前对话的 PM 线程列表
let _openThreadId='';       // 当前展开的线程 pm_id
let _threadMessages={};     // pm_id → messages 缓存

const _PM_STATUS_LABEL={starting:'启动中',planning:'规划中',executing:'执行中',monitoring:'监控中',awaiting_input:'⚠️ 等待决策',completed:'已完成',failed:'已失败',cancelled:'已取消',paused:'已暂停'};

async function loadPMThreads(){
  if(!_activeConvId){_pmThreads=[];renderPMThreadDock();return;}
  try{
    const r=await apiFetch(`/api/conversations/${_activeConvId}/pm-threads`);
    const d=await r.json();
    _pmThreads=d.threads||[];
  }catch(e){_pmThreads=[];}
  renderPMThreadDock();
}

function renderPMThreadDock(){
  const dock=document.getElementById('pm-thread-dock');
  if(!_pmThreads.length){dock.style.display='none';dock.innerHTML='';return;}
  dock.style.display='flex';
  dock.innerHTML=_pmThreads.map(t=>{
    const st=t.status||'starting';
    const isAwait=st==='awaiting_input';
    const isActive=t.pm_id===_openThreadId;
    const label=_PM_STATUS_LABEL[st]||st;
    return `<div class="pm-thread-chip ${isActive?'active':''} ${isAwait?'awaiting':''}" onclick="togglePMThread('${t.pm_id}')" title="${esc(t.task_name||'')}">
      ${isAwait?'<span class="pm-chip-badge">!</span>':''}
      <span class="pm-chip-dot ${st}"></span>
      <span class="pm-chip-name">🧑‍💼 ${esc(t.agent_name||'PM')}</span>
      <span class="pm-chip-task">${esc(t.task_name||'')}</span>
      <span class="pm-chip-status ${st}">${label}</span>
    </div>`;
  }).join('');
}

async function togglePMThread(pmId){
  if(_openThreadId===pmId){closePMThread();return;}
  _openThreadId=pmId;
  renderPMThreadDock();
  await openPMThread(pmId);
}

function closePMThread(){
  _openThreadId='';
  document.getElementById('pm-thread-panel-wrap').innerHTML='';
  renderPMThreadDock();
}

async function openPMThread(pmId){
  const thread=_pmThreads.find(t=>t.pm_id===pmId);
  if(!thread){closePMThread();return;}
  const wrap=document.getElementById('pm-thread-panel-wrap');
  const st=thread.status||'starting';
  const isAwait=st==='awaiting_input';

  // 加载线程消息
  let msgs=[];
  try{
    const r=await apiFetch(`/api/pm-threads/${pmId}/messages?limit=80`);
    const d=await r.json();
    msgs=d.messages||[];
  }catch(e){}
  _threadMessages[pmId]=msgs;

  // 查找最新的澄清请求 (用于快捷回复)
  const lastClarification=[...msgs].reverse().find(m=>m.type==='clarification');
  const showChips=isAwait&&lastClarification&&lastClarification.options&&lastClarification.options.length;

  wrap.innerHTML=`<div class="pm-thread-panel" id="thread-panel-${pmId}">
    <div class="thread-header">
      <span class="pm-chip-dot ${st}"></span>
      <span class="th-name">🧑‍💼 ${esc(thread.agent_name||'PM')}</span>
      <span class="th-task">${esc(thread.task_name||'')} · ${_PM_STATUS_LABEL[st]||st}</span>
      <span class="th-close" onclick="closePMThread()" title="收起线程">✕</span>
    </div>
    <div class="thread-messages" id="thread-msgs-${pmId}">
      ${msgs.length?msgs.map(m=>renderThreadMsg(m)).join(''):'<div style="text-align:center;color:var(--dim);font-size:12px;padding:16px">线程暂无消息，直接输入即可与 PM 对话</div>'}
    </div>
    ${showChips?`<div class="quick-chips" id="thread-chips-${pmId}">${lastClarification.options.map(o=>`<span class="quick-chip" onclick="sendThreadQuickReply('${pmId}','${esc(o)}')">${esc(o)}</span>`).join('')}</div>`:''}
    <div class="thread-input-bar">
      <input type="text" id="thread-input-${pmId}" placeholder="直接发送给 ${esc(thread.agent_name||'PM')}…" onkeydown="if(event.key==='Enter')sendThreadMessage('${pmId}')">
      <button class="btn primary" onclick="sendThreadMessage('${pmId}')">发送</button>
    </div>
  </div>`;

  // 滚动到底部
  const msgEl=document.getElementById(`thread-msgs-${pmId}`);
  if(msgEl)msgEl.scrollTop=msgEl.scrollHeight;
  const inp=document.getElementById(`thread-input-${pmId}`);
  if(inp)inp.focus();
}

function renderThreadMsg(m){
  const role=m.role||'system';
  const cls=role==='user'?'user':(role==='pm'?'pm'+(m.type==='clarification'?' clarification':''):'system');
  const label=role==='user'?'Boss':(role==='pm'?'PM':'');
  const meta=label?`<div class="t-meta">${label} · ${fmtTime(m.timestamp)}</div>`:'';
  return `<div class="thread-msg ${cls}">${role==='user'?esc(m.content):renderMarkdown(m.content||'')}${meta}</div>`;
}

async function sendThreadMessage(pmId){
  const inp=document.getElementById(`thread-input-${pmId}`);
  if(!inp)return;
  const text=inp.value.trim();
  if(!text)return;
  inp.value='';
  // 立即显示用户消息
  appendThreadMsgLocal(pmId,{role:'user',content:text,timestamp:Date.now()/1000});
  try{
    const r=await apiFetch(`/api/pm-threads/${pmId}/messages`,{
      method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({message:text,conv_id:_activeConvId})
    });
    const d=await r.json();
    if(d.ok){
      appendThreadMsgLocal(pmId,{role:'system',content:'✅ 已发送给 PM Agent',timestamp:Date.now()/1000});
      // 发送成功后清除快捷回复 chips
      const chips=document.getElementById(`thread-chips-${pmId}`);
      if(chips)chips.remove();
    }else{
      appendThreadMsgLocal(pmId,{role:'system',content:d.reply||'发送失败',timestamp:Date.now()/1000});
    }
  }catch(e){
    appendThreadMsgLocal(pmId,{role:'system',content:'[通信失败] '+e.message,timestamp:Date.now()/1000});
  }
}

async function sendThreadQuickReply(pmId,option){
  appendThreadMsgLocal(pmId,{role:'user',content:option,timestamp:Date.now()/1000});
  const chips=document.getElementById(`thread-chips-${pmId}`);
  if(chips)chips.remove();
  try{
    const r=await apiFetch(`/api/pm-threads/${pmId}/messages`,{
      method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({message:option,conv_id:_activeConvId})
    });
    const d=await r.json();
    appendThreadMsgLocal(pmId,{role:'system',content:d.ok?'✅ 决策已发送':(d.reply||'发送失败'),timestamp:Date.now()/1000});
  }catch(e){
    appendThreadMsgLocal(pmId,{role:'system',content:'[通信失败] '+e.message,timestamp:Date.now()/1000});
  }
}

function appendThreadMsgLocal(pmId,m){
  const el=document.getElementById(`thread-msgs-${pmId}`);
  if(!el)return;
  // 清除空状态提示
  const placeholder=el.querySelector('div[style]');
  if(placeholder&&!placeholder.classList.contains('thread-msg'))placeholder.remove();
  el.insertAdjacentHTML('beforeend',renderThreadMsg(m));
  el.scrollTop=el.scrollHeight;
  if(_threadMessages[pmId])_threadMessages[pmId].push(m);
}

// WS 事件: PM 状态变更时刷新线程卡片
function onPMThreadStatusChange(data){
  const pmId=data.pm_id||'';
  const st=data.status||'';
  const t=_pmThreads.find(x=>x.pm_id===pmId);
  if(t){
    t.status=st;
    renderPMThreadDock();
    // 如果该线程正在展开, 更新头部状态
    if(_openThreadId===pmId){
      const panel=document.getElementById(`thread-panel-${pmId}`);
      if(panel){
        const dot=panel.querySelector('.pm-chip-dot');
        if(dot)dot.className='pm-chip-dot '+st;
        const task=panel.querySelector('.th-task');
        if(task)task.textContent=(t.task_name||'')+' · '+(_PM_STATUS_LABEL[st]||st);
      }
      // awaiting_input 且有选项时动态插入快捷回复
      if(st==='awaiting_input'&&data.clarification_options&&data.clarification_options.length){
        appendThreadMsgLocal(pmId,{role:'pm',content:data.clarification_question||'',timestamp:Date.now()/1000,type:'clarification',options:data.clarification_options});
        let chips=document.getElementById(`thread-chips-${pmId}`);
        if(!chips){
          const inputBar=panel.querySelector('.thread-input-bar');
          if(inputBar){
            inputBar.insertAdjacentHTML('beforebegin',`<div class="quick-chips" id="thread-chips-${pmId}">${data.clarification_options.map(o=>`<span class="quick-chip" onclick="sendThreadQuickReply('${pmId}','${escJs(o)}')">${esc(o)}</span>`).join('')}</div>`);
          }
        }
      }
    }
  }
}

// WS 事件: PM 线程新绑定
function onPMThreadAttached(data){
  if(data.conv_id===_activeConvId)loadPMThreads();
}

