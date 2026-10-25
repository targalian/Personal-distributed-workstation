// ── 秘书聊天 ──
let chatMessages=[];
let _chatSending=false;
function _showTyping(){
  const container=document.getElementById('chat-messages');
  const bubble=document.createElement('div');
  bubble.id='typing-indicator';
  bubble.className='typing-bubble';
  bubble.innerHTML='<span class="typing-dot"></span><span class="typing-dot"></span><span class="typing-dot"></span><span class="typing-label">秘书思考中...</span>';
  container.appendChild(bubble);
  container.scrollTop=container.scrollHeight;
}
function _hideTyping(){
  const el=document.getElementById('typing-indicator');
  if(el)el.remove();
}
function _setChatLock(locked){
  _chatSending=locked;
  const input=document.getElementById('chat-input');
  const btn=document.getElementById('chat-send-btn');
  if(input)input.disabled=locked;
  if(btn){btn.disabled=locked;btn.textContent=locked?'思考中...':'发送';}
}
async function sendChatMessage(){
  if(_chatSending)return;
  const input=document.getElementById('chat-input');
  const text=input.value.trim();
  if(!text)return;
  input.value='';
  appendChatMessage('user',text,Date.now()/1000);
  // 工作站优化快捷入口: 命中则本地处理并写回对话, 不再发送秘书
  if(handleOptChatCommand(text))return;
  _setChatLock(true);
  _showTyping();
  const t0=Date.now();
  const abortCtrl=new AbortController();
  const timeout=setTimeout(()=>abortCtrl.abort(),60000);
  try{
    const r=await apiFetch('/api/secretary/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:text,conv_id:_activeConvId}),signal:abortCtrl.signal});
    clearTimeout(timeout);
    const d=await r.json();
    _hideTyping();
    if(!ws||ws.readyState!==WebSocket.OPEN){appendChatMessage('assistant',d.reply,d.timestamp);}
    const elapsed=((Date.now()-t0)/1000).toFixed(1);
    showToast(`秘书已回复 (${elapsed}s)`, 'info', 2500);
    loadConversations(); // 刷新侧边栏时间
  }catch(e){
    clearTimeout(timeout);
    _hideTyping();
    const msg=e.name==='AbortError'?'[超时] 秘书响应超过60s,请重试':'[通信失败] '+e.message;
    appendChatMessage('assistant',msg,Date.now()/1000);
    showToast(e.name==='AbortError'?'秘书响应超时':'秘书通信失败', 'err');
  }finally{
    _setChatLock(false);
    document.getElementById('chat-input').focus();
  }
}
function appendChatMessage(role,content,timestamp,extra){
  const container=document.getElementById('chat-messages');
  const empty=container.querySelector('.empty');
  if(empty)empty.remove();
  const msg=document.createElement('div');
  // 工作站优化: 聊天消息内渲染优化项卡片组 (带确认/拒绝/补充按钮)
  if(extra&&extra.type==='opt_items'){
    msg.className='chat-msg sys-card';
    let h=renderMarkdown(content);
    h+='<div class="opt-chat-cards">';
    (extra.items||[]).forEach(it=>{h+=optItemHtml(it,{inChat:true});});
    h+='</div>';
    msg.innerHTML=h;
    container.appendChild(msg);
    container.scrollTop=container.scrollHeight;
    chatMessages.push({role,content,timestamp});
    return;
  }
  // 方案C: L1 系统通知卡片 (PM 决策请求)
  const msgType=extra&&extra.type?extra.type:'';
  if(role==='system'&&msgType==='pm_clarification'){
    msg.className='chat-msg sys-card';
    const pmId=extra.pm_id||'';
    const options=extra.options||[];
    let html=renderMarkdown(content);
    if(options.length||pmId){
      html+='<div class="sys-actions">';
      options.forEach(o=>{html+=`<span class="quick-chip" onclick="l1QuickReply('${pmId}','${escJs(o)}')">${esc(o)}</span>`;});
      if(pmId)html+=`<span class="quick-chip" style="border-color:var(--accent);color:var(--accent);background:rgba(91,140,255,.1)" onclick="openThreadFromL1('${pmId}')">💬 展开线程对话</span>`;
      html+='</div>';
    }
    msg.innerHTML=html;
  }else{
    msg.className='chat-msg '+role;
    const rendered=role==='assistant'?renderMarkdown(content):esc(content);
    msg.innerHTML=rendered+'<div class="msg-meta">'+(role==='user'?'Boss':(role==='system'?'':('秘书')))+' · '+fmtTime(timestamp)+'</div>';
  }
  container.appendChild(msg);
  container.scrollTop=container.scrollHeight;
  chatMessages.push({role,content,timestamp});
}
// 方案C: L1 快捷回复 → 直接注入 PM 线程
async function l1QuickReply(pmId,option){
  if(!pmId)return;
  try{
    const r=await apiFetch(`/api/pm-threads/${pmId}/messages`,{
      method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({message:option,conv_id:_activeConvId})
    });
    const d=await r.json();
    showToast(d.ok?`✅ 决策「${option}」已发送给 PM`:(d.reply||'发送失败'),d.ok?'ok':'err');
    if(_openThreadId===pmId)appendThreadMsgLocal(pmId,{role:'user',content:option,timestamp:Date.now()/1000});
  }catch(e){showToast('发送失败:'+e.message,'err');}
}
// 方案C: 从 L1 通知卡片展开对应 PM 线程
async function openThreadFromL1(pmId){
  if(!_pmThreads.find(t=>t.pm_id===pmId)){await loadPMThreads();}
  _openThreadId=pmId;
  renderPMThreadDock();
  await openPMThread(pmId);
}
function renderMarkdown(text){
  // 简易 Markdown 渲染: 代码块 + 行内代码 + 加粗 + 列表
  let h=esc(text);
  // 代码块 ```...```
  h=h.replace(/```(\w*)\n([\s\S]*?)```/g,(m,lang,code)=>'<pre style="background:var(--bg);padding:10px;border-radius:6px;overflow-x:auto;font-size:12px;margin:6px 0"><code>'+code+'</code></pre>');
  // 行内代码
  h=h.replace(/`([^`]+)`/g,'<code style="background:var(--bg);padding:2px 5px;border-radius:3px;font-size:12px">$1</code>');
  // 加粗
  h=h.replace(/\*\*([^*]+)\*\*/g,'<strong>$1</strong>');
  // 标题
  h=h.replace(/^### (.+)$/gm,'<strong style="font-size:14px">$1</strong>');
  h=h.replace(/^## (.+)$/gm,'<strong style="font-size:15px">$1</strong>');
  // 列表
  h=h.replace(/^- (.+)$/gm,'• $1');
  h=h.replace(/^\d+\. (.+)$/gm,(m,p1,i)=>'• '+p1);
  return h;
}
async function loadChatHistory(){
  // 加载当前活跃对话的消息
  if(!_activeConvId){return;}
  try{
    const r=await apiFetch(`/api/conversations/${_activeConvId}/messages?limit=100`);
    const d=await r.json();
    const container=document.getElementById('chat-messages');
    container.innerHTML='';
    const raw=d.messages||[];
    // iter-74: 讨论消息 (action_taken=opt_discuss) 不进主聊天流 — 后端会将其与
    // 普通对话一同落库, 历史回放时需过滤, 否则讨论回复串入主聊天窗口
    const msgs=[];
    for(const m of raw){
      if(m.role==='assistant'&&m.action_taken==='opt_discuss'){
        const last=msgs[msgs.length-1];
        if(last&&last.role==='user')msgs.pop(); // 配对 user 提问一并移除 (后端成对落库)
        continue;
      }
      msgs.push(m);
    }
    if(!msgs.length){
      chatMessages=[];
      container.innerHTML='<div class="empty"><div class="icon">💬</div><p>向秘书发送消息开始对话</p><p class="hint">例如: "当前状态如何?" 或 "提交一个代码审查任务"</p></div>';
      return;
    }
    chatMessages=[];
    msgs.forEach(m=>appendChatMessage(m.role,m.content,m.timestamp,{type:m.type,pm_id:m.pm_id,options:m.options}));
  }catch(e){console.error('加载对话消息失败:',e);}
}
function clearChat(){
  if(!confirm('确定要清空当前对话记录吗？'))return;
  apiFetch('/api/secretary/chat/history',{method:'DELETE'})
    .then(r=>r.json())
    .then(()=>{
      document.getElementById('chat-messages').innerHTML='<div class="empty"><div class="icon">💬</div><p>向秘书发送消息开始对话</p></div>';
      chatMessages=[];
    })
    .catch(e=>{showToast('清空失败:'+e.message,'err');});
}

// ══ 工作站优化 (常驻自我优化工作流) ══
// API 契约 (后端由 Codex 实现, 前端先行以本地 mock 适配闭环):
//   GET  /api/workstation-optimization/summary
//   GET  /api/workstation-optimization/items
//   POST /api/workstation-optimization/items  {source,title,description,priority}
//   POST /api/workstation-optimization/items/{id}/decision  {decision:"approve|reject|clarify",reply}
//   WS: workstation_optimization_created|updated|waiting_boss|completed
// 后端未实现 (404/405/501) 时回退 localStorage mock 并显示「本地演示数据」徽标。
const OPT_LS_KEY='lan_mesh_opt_items_v1';
const OPT_SRC_LABEL={boss:'Boss',bottleneck:'瓶颈',agent:'Agent'};
const OPT_PRI_LABEL={low:'低',normal:'普通',high:'高',urgent:'紧急'};
const OPT_ST_LABEL={candidate:'候选',waiting_boss:'待决策',queued:'已排队',running:'执行中',completed:'已完成',rejected:'已拒绝'};
const OPT_PRI_ORDER={urgent:0,high:1,normal:2,low:3};
let optMockMode=false;        // true=后端未实现, 本地 mock
let optLoadState='loading';   // loading | ok | error
let optLoadError='';
let optItems=[];              // 内存镜像 (以 id 为键去重)
let optRenderSnap='';         // 渲染快照 (无变化不重绘, 防闪烁)
let optGuardTimer=null;       // mock 守护模拟器

function optSeedItems(){
  const now=Date.now()/1000;
  return [
    {id:'opt-demo-1',source:'agent',title:'模型路由评分缺少 TTFB 反馈回路',description:'评分公式未纳入实际首 token 延迟, 长期可能选型偏慢模型。建议迭代加入 TTFB 反馈因子。',priority:'normal',status:'waiting_boss',created_at:now-7200,updated_at:now-7200,decision_reply:''},
    {id:'opt-demo-2',source:'bottleneck',title:'任务并发 3+ 时 SQLite 写入阻塞',description:'开发任务运行中遇到: 多 PM 并发写进度时出现 database is locked, 建议改 WAL 模式或写入队列。',priority:'high',status:'waiting_boss',created_at:now-3600,updated_at:now-3600,decision_reply:''},
    {id:'opt-demo-3',source:'boss',title:'仪表盘首屏加载超过 2 秒',description:'Boss 要求: 优化静态资源加载与缓存策略, 首屏目标 1s 内。',priority:'normal',status:'queued',created_at:now-1800,updated_at:now-1800,decision_reply:''},
    {id:'opt-demo-4',source:'agent',title:'心跳 3s 间隔在 10+ 节点时 UDP 风暴',description:'已执行完成: 心跳合并为批量上报, 网络包数下降 40%。',priority:'low',status:'completed',created_at:now-86400,updated_at:now-80000,decision_reply:''},
  ];
}
function optLoadLS(){
  try{
    const raw=localStorage.getItem(OPT_LS_KEY);
    if(raw){const arr=JSON.parse(raw);if(Array.isArray(arr))return arr;}
  }catch(e){/* 损坏忽略 */}
  return null;
}
function optSaveLS(){
  try{localStorage.setItem(OPT_LS_KEY,JSON.stringify(optItems));}catch(e){/* 配额满忽略 */}
}
function optUid(){return 'opt-'+(Date.now().toString(36)+Math.random().toString(36).slice(2,8));}
function optIsApiMissing(r){return r.status===404||r.status===405||r.status===501;}

async function loadOptData(){
  // 拉取 summary + items; 后端未实现则回退 localStorage mock (首次写入种子)。
  try{
    const r=await apiFetch('/api/workstation-optimization/items');
    if(r.ok){
      const d=await r.json();
      const arr=Array.isArray(d)?d:(d.items||[]);
      if(Array.isArray(arr)){
        optMockMode=false;
        optLoadState='ok';
        optItems=arr;
        optSaveLS(); // 真实数据也镜像到本地, 断网时不丢展示
        optScheduleRender();
        return;
      }
    }else if(!optIsApiMissing(r)){
      optLoadState='error';
      optLoadError='接口错误: HTTP '+r.status;
      optScheduleRender();
      return;
    }
  }catch(e){
    optLoadState='error';
    optLoadError='接口异常: '+e.message;
    optScheduleRender();
    return;
  }
  // 后端未实现 → mock
  optMockMode=true;
  const ls=optLoadLS();
  if(ls){optItems=ls;}   // 键存在: 含空数组 (用户清空) 也尊重, 不重新播种
  else{optItems=optSeedItems();optSaveLS();} // 仅首次 (键不存在) 播种
  optLoadState='ok';
  optScheduleRender();
  optMockGuardianStart();
}

async function createOptItem(payload,{direct=false}={}){
  // 返回 {ok,item,usedMock}; direct=true 表示 Boss 命令式提交 (直接入队)。
  try{
    const r=await apiFetch('/api/workstation-optimization/items',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
    if(r.ok){
      const d=await r.json();
      optMockMode=false;
      return {ok:true,item:d.item||d,usedMock:false};
    }else if(!optIsApiMissing(r)){
      return {ok:false,error:'接口错误: HTTP '+r.status,usedMock:false};
    }
  }catch(e){return {ok:false,error:'接口异常: '+e.message,usedMock:false};}
  // mock 创建
  optMockMode=true;
  const now=Date.now()/1000;
  const item={
    id:optUid(),source:payload.source||'boss',
    title:String(payload.title||'').slice(0,120),
    description:String(payload.description||''),
    priority:OPT_PRI_ORDER[payload.priority]!==undefined?payload.priority:'normal',
    status:direct?'queued':'waiting_boss',
    created_at:now,updated_at:now,decision_reply:'',
  };
  optItems.push(item);optSaveLS();
  optDispatch('workstation_optimization_created',{item:item});
  return {ok:true,item:item,usedMock:true};
}

async function decideOptItem(id,decision,reply){
  // decision: approve | reject | clarify
  const it=optItems.find(x=>x.id===id);
  try{
    const r=await apiFetch('/api/workstation-optimization/items/'+encodeURIComponent(id)+'/decision',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({decision:decision,reply:reply||''})});
    if(r.ok){
      const d=await r.json();
      optMockMode=false;
      return {ok:true,item:d.item||d,usedMock:false};
    }else if(!optIsApiMissing(r)){
      return {ok:false,error:'接口错误: HTTP '+r.status,usedMock:false};
    }
  }catch(e){return {ok:false,error:'接口异常: '+e.message,usedMock:false};}
  // mock 决策
  optMockMode=true;
  if(!it)return {ok:false,error:'优化项不存在: '+id,usedMock:true};
  const now=Date.now()/1000;
  if(decision==='approve'){it.status='queued';it.updated_at=now;}
  else if(decision==='reject'){it.status='rejected';it.updated_at=now;}
  else{it.decision_reply=reply||'';it.updated_at=now;} // clarify: 保持 waiting_boss
  optSaveLS();
  optDispatch('workstation_optimization_updated',{item:it,decision:decision});
  return {ok:true,item:it,usedMock:true};
}

function optDispatch(type,data){
  // 统一事件入口: 本地操作与后端 WS 事件同构处理
  handleOptEvent({type:type,data:data||{}});
}
function handleOptEvent(evt){
  const t=evt.type||'';
  const d=evt.data||{};
  if(t==='workstation_optimization_created'){
    if(d.item&&!optItems.find(x=>x.id===d.item.id)){optItems.push(d.item);optSaveLS();}
    else if(!d.item&&!optMockMode){loadOptData();return;}
    showToast('🛠️ 新增优化项: '+(d.item?d.item.title:''),'info');
  }else if(t==='workstation_optimization_updated'){
    if(d.item){const i=optItems.findIndex(x=>x.id===d.item.id);if(i>=0){optItems[i]=d.item;}else if(!optMockMode){loadOptData();return;}optSaveLS();}
    else if(!optMockMode){loadOptData();return;}
  }else if(t==='workstation_optimization_waiting_boss'){
    if(d.item){const i=optItems.findIndex(x=>x.id===d.item.id);if(i>=0){optItems[i]=d.item;}else{optItems.push(d.item);}optSaveLS();}
    showToast('⏳ 优化项待 Boss 决策: '+(d.item?d.item.title:''),'info');
  }else if(t==='workstation_optimization_completed'){
    if(d.item){const i=optItems.findIndex(x=>x.id===d.item.id);if(i>=0){optItems[i]=d.item;optSaveLS();}}
    showToast('✅ 优化项完成: '+(d.item?d.item.title:''),'ok');
  }else{return;}
  optScheduleRender();
}

let _optRenderPending=false;
function optScheduleRender(){
  if(_optRenderPending)return;
  _optRenderPending=true;
  requestAnimationFrame(()=>{_optRenderPending=false;renderOptPanel();renderOptStatusCard();});
}
function optSorted(){
  return optItems.slice().sort((a,b)=>
    (OPT_PRI_ORDER[a.priority]!==undefined?OPT_PRI_ORDER[a.priority]:2)-
    (OPT_PRI_ORDER[b.priority]!==undefined?OPT_PRI_ORDER[b.priority]:2)||
    (a.created_at||0)-(b.created_at||0));
}
function optGroup(items,status){return items.filter(x=>x.status===status);}
function optBadges(it){
  return '<span class="opt-badge src-'+esc(it.source)+'">'+esc(OPT_SRC_LABEL[it.source]||it.source)+'</span>'+
    '<span class="opt-badge pri-'+esc(it.priority)+'">'+esc(OPT_PRI_LABEL[it.priority]||it.priority)+'</span>'+
    '<span class="opt-badge st-'+esc(it.status)+'">'+esc(OPT_ST_LABEL[it.status]||it.status)+'</span>';
}
function optActionsHtml(it,{inChat=false}={}){
  if(it.status!=='waiting_boss'&&it.status!=='candidate')return '';
  const cls=inChat?'opt-chat-actions':'opt-item-actions';
  const id=esc(it.id);
  return '<div class="'+cls+'">'+
    '<button class="btn sm" style="border-color:var(--green);color:var(--green)" onclick="'+(inChat?'optChatDecide':'optPanelDecide')+'(' + "'" + id + "'" + ',\'approve\')">✅ 确认执行</button>'+
    '<button class="btn sm danger" onclick="'+(inChat?'optChatDecide':'optPanelDecide')+'(' + "'" + id + "'" + ',\'reject\')">🚫 拒绝</button>'+
    '<button class="btn sm" onclick="'+(inChat?'optChatClarify':'optPanelClarify')+'(' + "'" + id + "'" + ')">💬 补充说明</button>'+
    '<span class="opt-clarify-box" id="opt-clarify-'+id+'" style="display:none"><input type="text" placeholder="补充说明…" onkeydown="if(event.key===\'Enter\')'+(inChat?'optChatSendClarify':'optPanelSendClarify')+'(' + "'" + id + "'" + ')"><button class="btn sm primary" onclick="'+(inChat?'optChatSendClarify':'optPanelSendClarify')+'(' + "'" + id + "'" + ')">提交</button></span>'+
    '</div>';
}
function optItemHtml(it,{inChat=false}={}){
  const cls=inChat?'opt-chat-card':'opt-item';
  const id=esc(it.id);
  const topicCls=(!inChat&&optDiscussTopicKey()===it.id)?' topic-active':'';
  let h='<div class="'+cls+' '+esc(it.status)+topicCls+'" data-opt-id="'+id+'">';
  h+='<div class="opt-item-head"><span class="opt-item-title">'+esc(it.title)+'</span>'+optBadges(it)+'</div>';
  if(it.description)h+='<div class="opt-item-desc">'+esc(it.description)+'</div>';
  if(it.decision_reply)h+='<div class="opt-item-desc" style="color:var(--yellow)">💬 Boss 补充: '+esc(it.decision_reply)+'</div>';
  h+='<div class="opt-item-meta"><span>🕐 '+fmtTime(it.created_at)+'</span>';
  if(!inChat){
    h+='<span style="cursor:pointer;color:var(--accent)" onclick="showOptDetail(' + "'" + id + "'" + ')">👁 详情</span>';
    h+='<span style="cursor:pointer;color:var(--accent)" onclick="optDiscussSelect(' + "'" + id + "'" + ')">💬 讨论</span>';
  }
  h+='</div>'+optActionsHtml(it,{inChat:inChat});
  h+='</div>';
  return h;
}
function optPanelBodyHtml(){
  if(optLoadState==='loading')return '<div class="opt-loading">⏳ 加载优化队列中...</div>';
  if(optLoadState==='error')return '<div class="opt-error">❌ '+esc(optLoadError)+'</div><div style="text-align:center;padding-bottom:8px"><button class="btn sm" onclick="optRetry()">🔄 重试</button></div>';
  if(!optItems.length){
    return '<div class="opt-empty"><div class="icon">🛠️</div><p>优化队列为空</p>'+
      '<p class="hint">对话中发送「优化工作站: 需求」或点击下方快速添加</p>'+
      '<div class="quick-chips" style="justify-content:center">'+
      '<span class="quick-chip" onclick="optQuickAdd(\'boss\')">➕ Boss 优化要求</span>'+
      '<span class="quick-chip" onclick="optQuickAdd(\'bottleneck\')">⚠️ 记录瓶颈</span>'+
      '<span class="quick-chip" onclick="optQuickAdd(\'agent\')">🤖 Agent 建议</span></div></div>';
  }
  const sorted=optSorted();
  const groups=[
    ['waiting_boss','⏳ 待 Boss 决策',null],
    ['running','▶️ 执行中',null],
    ['queued','📋 队列中',null],
    ['candidate','💡 候选建议',null],
    ['completed','✅ 已完成',3],
    ['rejected','🚫 已拒绝',3],
  ];
  let h='';
  groups.forEach(([st,label,limit])=>{
    let list=optGroup(sorted,st);
    if(!list.length)return;
    let tail='';
    if(limit&&list.length>limit){tail='<span style="color:var(--dim);font-size:11px">(最近 '+limit+' / 共 '+list.length+' 条)</span>';list=list.slice(0,limit);}
    h+='<div class="opt-group-title">'+label+' <span class="cnt">'+list.length+'</span> '+tail+'</div>';
    list.forEach(it=>{h+=optItemHtml(it);});
  });
  return h;
}
function renderOptPanel(){
  const panel=document.getElementById('opt-panel');
  if(!panel||panel.style.display==='none')return;
  const snap=optLoadState+'|'+optMockMode+'|'+JSON.stringify(optItems);
  if(snap===optRenderSnap)return;
  optRenderSnap=snap;
  document.getElementById('opt-panel-body').innerHTML=optPanelBodyHtml();
  renderOptDiscuss(); // iter-73: 面板打开/重绘时同步讨论区
  const stats=document.getElementById('opt-panel-stats');
  const w=optGroup(optItems,'waiting_boss').length;
  const q=optGroup(optItems,'queued').length;
  const r=optGroup(optItems,'running').length;
  stats.textContent='待决策 '+w+' · 队列 '+q+' · 执行中 '+r;
  document.getElementById('opt-mock-badge').style.display=optMockMode?'inline-block':'none';
}
function renderOptStatusCard(){
  const card=document.getElementById('opt-status-card');
  if(!card)return;
  card.style.display='block';
  if(optLoadState==='loading'){
    document.getElementById('opt-kpi-guard').textContent='…';
    return;
  }
  const waiting=optGroup(optItems,'waiting_boss').length;
  const queued=optGroup(optItems,'queued').length;
  const running=optGroup(optItems,'running')[0];
  const done=optGroup(optItems,'completed').sort((a,b)=>(b.updated_at||0)-(a.updated_at||0))[0];
  document.getElementById('opt-guard-dot').classList.toggle('off',optLoadState==='error');
  document.getElementById('opt-kpi-guard').textContent=optLoadState==='error'?'异常':'运行中';
  document.getElementById('opt-kpi-queue').textContent=queued;
  document.getElementById('opt-kpi-running').textContent=running?running.title:'-';
  const w=document.getElementById('opt-kpi-waiting');
  w.textContent=waiting;
  w.className=waiting>0?'warn':'';
  document.getElementById('opt-kpi-done').textContent=done?done.title:'-';
  // 秘书页按钮角标
  const dot=document.getElementById('opt-pending-dot');
  if(dot){dot.textContent=waiting;dot.style.display=waiting>0?'flex':'none';}
}
function toggleOptPanel(force){
  const panel=document.getElementById('opt-panel');
  const show=force===undefined?panel.style.display==='none':!!force;
  panel.style.display=show?'flex':'none';
  if(show){optRenderSnap='';renderOptPanel();}
}
function openOptFromStation(){
  switchTab('chat');
  toggleOptPanel(true);
  showToast('🛠️ 已打开工作站优化面板','info');
}
function optRetry(){
  optLoadState='loading';optLoadError='';optRenderSnap='';
  renderOptPanel();
  loadOptData();
}
function optQuickAdd(source){
  // 面板内快速添加 (弹窗表单, 不走原生 prompt)
  const priOpts={urgent:'紧急',high:'高',normal:'普通',low:'低'};
  let sel='';
  Object.entries(priOpts).forEach(([k,v])=>{sel+='<option value="'+k+'">'+v+'</option>';});
  document.getElementById('modal').innerHTML=
    '<span class="modal-close" onclick="closeOverlayForce()">✕</span>'+
    '<h2>➕ 添加优化项</h2>'+
    '<div class="form-row" style="margin-top:14px"><label>来源</label><select id="opt-add-source">'+
    '<option value="boss"'+(source==='boss'?' selected':'')+'>Boss 优化要求</option>'+
    '<option value="bottleneck"'+(source==='bottleneck'?' selected':'')+'>运行中瓶颈</option>'+
    '<option value="agent"'+(source==='agent'?' selected':'')+'>Agent 建议</option></select></div>'+
    '<div class="form-row"><label>标题</label><input id="opt-add-title" placeholder="一句话概括"></div>'+
    '<div class="form-row"><label>说明</label><textarea id="opt-add-desc" rows="4" placeholder="详细描述优化内容或背景"></textarea></div>'+
    '<div class="form-row"><label>优先级</label><select id="opt-add-pri">'+sel+'</select></div>'+
    '<button class="btn primary" style="width:100%" onclick="optAddSubmit()">提交</button>';
  document.getElementById('overlay').classList.add('show');
}
function optAddSubmit(){
  const source=document.getElementById('opt-add-source').value;
  const title=document.getElementById('opt-add-title').value.trim();
  const desc=document.getElementById('opt-add-desc').value.trim();
  const pri=document.getElementById('opt-add-pri').value;
  if(!title){showToast('请填写标题','err');return;}
  createOptItem({source:source,title:title,description:desc||title,priority:pri}).then(res=>{
    closeOverlayForce();
    if(res.ok)showToast('🛠️ 已创建优化项: '+(res.item?res.item.title:'')+(res.usedMock?' (本地演示)':''),'ok');
    else showToast('创建失败: '+res.error,'err');
  });
}
function showOptDetail(id){
  const it=optItems.find(x=>x.id===id);
  if(!it)return;
  document.getElementById('modal').innerHTML=
    '<span class="modal-close" onclick="closeOverlayForce()">✕</span>'+
    '<h2>'+esc(it.title)+'</h2>'+
    '<div style="margin-top:10px">'+optBadges(it)+'</div>'+
    '<div class="modal-sec"><h3>说明</h3><div style="font-size:14px;line-height:1.7;white-space:pre-wrap">'+esc(it.description||'无')+'</div></div>'+
    (it.decision_reply?'<div class="modal-sec"><h3>Boss 补充说明</h3><div style="font-size:14px">'+esc(it.decision_reply)+'</div></div>':'')+
    '<div class="modal-sec"><h3>时间线</h3><div class="kv-grid">'+
    '<div class="k">创建</div><div class="v">'+fmtTime(it.created_at)+'</div>'+
    '<div class="k">更新</div><div class="v">'+fmtTime(it.updated_at)+'</div>'+
    '<div class="k">ID</div><div class="v">'+esc(it.id)+'</div></div></div>'+
    (it.status==='waiting_boss'||it.status==='candidate'?'<div class="modal-sec">'+optActionsHtml(it)+'</div>':'');
  document.getElementById('overlay').classList.add('show');
}
function optPanelDecide(id,decision){
  decideOptItem(id,decision,'').then(res=>{
    if(!res.ok){showToast('决策失败: '+res.error,'err');return;}
    const it=res.item||{};
    if(decision==='approve')showToast('✅ 已确认执行: '+it.title,'ok');
    else if(decision==='reject')showToast('🚫 已拒绝: '+it.title,'info');
    closeOverlayForce();
  });
}
function optPanelClarify(id){
  const box=document.getElementById('opt-clarify-'+id);
  if(box){box.style.display='flex';box.querySelector('input').focus();}
}
function optPanelSendClarify(id){
  const box=document.getElementById('opt-clarify-'+id);
  const input=box?box.querySelector('input'):null;
  const reply=input?input.value.trim():'';
  if(!reply){showToast('请填写补充说明','err');return;}
  decideOptItem(id,'clarify',reply).then(res=>{
    if(res.ok)showToast('💬 补充说明已记录: '+reply,'ok');
    else showToast('提交失败: '+res.error,'err');
  });
}
function optChatDecide(id,decision){
  decideOptItem(id,decision,'').then(res=>{
    if(!res.ok){showToast('决策失败: '+res.error,'err');return;}
    const it=res.item||{};
    const msg=decision==='approve'?('✅ Boss 已确认执行优化项「'+it.title+'」'):('🚫 Boss 已拒绝优化项「'+it.title+'」');
    appendChatMessage('system',msg,Date.now()/1000);
    showToast(msg,'ok');
  });
}
function optChatClarify(id){
  const box=document.getElementById('opt-clarify-'+id);
  if(box){box.style.display='flex';box.querySelector('input').focus();}
}
function optChatSendClarify(id){
  const box=document.getElementById('opt-clarify-'+id);
  const input=box?box.querySelector('input'):null;
  const reply=input?input.value.trim():'';
  if(!reply){showToast('请填写补充说明','err');return;}
  decideOptItem(id,'clarify',reply).then(res=>{
    if(!res.ok){showToast('提交失败: '+res.error,'err');return;}
    appendChatMessage('system','💬 Boss 补充说明「'+((res.item||{}).title||'')+'」: '+reply,Date.now()/1000);
    showToast('💬 补充说明已记录','ok');
  });
}
function appendOptChatItems(items,contextText){
  // 聊天流中渲染优化项卡片组 (contextText 为引导文案)
  const container=document.getElementById('chat-messages');
  const msg=document.createElement('div');
  msg.className='chat-msg sys-card';
  let h=renderMarkdown(contextText||'工作站优化项: ');
  h+='<div class="opt-chat-cards">';
  items.forEach(it=>{h+=optItemHtml(it,{inChat:true});});
  h+='</div>';
  msg.innerHTML=h;
  container.appendChild(msg);
  container.scrollTop=container.scrollHeight;
}
function handleOptChatCommand(text){
  // 秘书对话快捷入口: 命中返回 true (消息已被本地处理, 不再发给秘书)
  let m=text.match(/^优化工作站[:：]\s*([\s\S]+)/);
  if(m){
    createOptItem({source:'boss',title:m[1].trim().slice(0,60),description:m[1].trim(),priority:'normal'},{direct:true}).then(res=>{
      if(res.ok)appendChatMessage('system','🛠️ 优化要求已入队「'+(res.item.title)+'」'+(res.usedMock?' (本地演示)':''),Date.now()/1000);
      else appendChatMessage('system','❌ 创建优化项失败: '+res.error,Date.now()/1000);
    });
    return true;
  }
  m=text.match(/^遇到瓶颈[:：]\s*([\s\S]+)/);
  if(m){
    createOptItem({source:'bottleneck',title:m[1].trim().slice(0,60),description:m[1].trim(),priority:'high'}).then(res=>{
      if(res.ok)appendChatMessage('system','⚠️ 瓶颈已登记为优化项「'+(res.item.title)+'」, 等待 Boss 决策'+(res.usedMock?' (本地演示)':''),Date.now()/1000);
      else appendChatMessage('system','❌ 登记瓶颈失败: '+res.error,Date.now()/1000);
    });
    return true;
  }
  m=text.match(/^添加优化建议[:：]\s*([\s\S]+)/);
  if(m){
    createOptItem({source:'boss',title:m[1].trim().slice(0,60),description:m[1].trim(),priority:'normal'}).then(res=>{
      if(res.ok)appendChatMessage('system','💡 优化建议已登记「'+(res.item.title)+'」, 等待确认'+(res.usedMock?' (本地演示)':''),Date.now()/1000);
      else appendChatMessage('system','❌ 登记建议失败: '+res.error,Date.now()/1000);
    });
    return true;
  }
  if(/^(查看优化|优化队列|优化状态|当前优化)/.test(text)){
    const active=optSorted().filter(x=>['waiting_boss','running','queued','candidate'].includes(x.status));
    if(active.length){appendOptChatItems(active,'📋 当前优化队列 ('+active.length+' 项):');}
    else{appendChatMessage('system','📋 优化队列为空。可发送「优化工作站: 需求」或「遇到瓶颈: 描述」添加。',Date.now()/1000);}
    return true;
  }
  return false;
}
// ── iter-73/74: 优化讨论窗口 (面板内嵌; iter-74 发送通道点亮真实后端) ──
const OPT_DISCUSS_LS_KEY='lan_mesh_opt_discuss_v1';
let _optDiscuss={active:'__all__',topics:{}}; // topics: {topicKey:[{role,content,ts}]}
let _optDiscussCollapsed=false;
let _optDiscussSending=false;   // 请求 in-flight 锁 (禁用输入/按钮)
let _optDiscussInflight=false;  // 思考态气泡开关 (仅请求期间显示)
function optDiscussLoad(){
  try{
    const raw=localStorage.getItem(OPT_DISCUSS_LS_KEY);
    if(raw){const d=JSON.parse(raw);if(d&&d.topics)_optDiscuss={active:d.active||'__all__',topics:d.topics};}
  }catch(e){/* 损坏忽略 */}
}
function optDiscussSave(){
  try{localStorage.setItem(OPT_DISCUSS_LS_KEY,JSON.stringify(_optDiscuss));}catch(e){/* 配额满忽略 */}
}
function optDiscussTopicKey(){return _optDiscuss.active||'__all__';}
function optDiscussTopicLabel(){
  const key=optDiscussTopicKey();
  if(key==='__all__')return '总体优化';
  const it=optItems.find(x=>x.id===key);
  return it?it.title.slice(0,14):'总体优化';
}
function optDiscussMsgs(){return _optDiscuss.topics[optDiscussTopicKey()]||[];}
function optDiscussSelect(id){
  // 点优化项「💬 讨论」: 切换话题上下文并聚焦输入框
  _optDiscuss.active=id||'__all__';
  optDiscussSave();
  optRenderSnap='';           // 强制面板重绘 (刷新 topic-active 高亮)
  optScheduleRender();
  renderOptDiscuss();
  const input=document.getElementById('opt-discuss-input');
  if(input)input.focus();
}
function optDiscussToggle(){
  _optDiscussCollapsed=!_optDiscussCollapsed;
  const el=document.getElementById('opt-discuss');
  if(el)el.classList.toggle('collapsed',_optDiscussCollapsed);
}
function renderOptDiscuss(){
  const log=document.getElementById('opt-discuss-log');
  if(!log)return;
  const topic=document.getElementById('opt-discuss-topic');
  if(topic){
    topic.textContent=optDiscussTopicLabel();
    topic.className='opt-discuss-topic'+(optDiscussTopicKey()==='__all__'?' all':'');
  }
  const input=document.getElementById('opt-discuss-input');
  if(input){
    if(optDiscussTopicKey()==='__all__')input.placeholder='和秘书讨论总体优化…';
    else{const it=optItems.find(x=>x.id===optDiscussTopicKey());input.placeholder='和秘书讨论「'+(it?it.title.slice(0,16):'该优化项')+'」…';}
  }
  const msgs=optDiscussMsgs();
  let h=msgs.map(m=>{
    const who=m.role==='user'?'Boss':'秘书';
    return '<div class="opt-discuss-msg '+esc(m.role)+'">'+esc(m.content)+'<span class="ts">'+who+' · '+fmtTime(m.ts)+'</span></div>';
  }).join('');
  if(!msgs.length){
    h='<div class="opt-discuss-msg assistant">💬 就优化项向秘书提问或讨论, 例如: 「这条建议的详细执行方案是什么?」</div>';
  }else if(_optDiscussInflight&&msgs[msgs.length-1].role==='user'){
    // 真实思考态: 仅请求 in-flight 期间显示, 请求结束即撤
    h+='<div class="opt-discuss-msg pending">⏳ 秘书思考中…</div>';
  }
  log.innerHTML=h;
  log.scrollTop=log.scrollHeight;
}
function _optDiscussLock(locked){
  // 讨论区自己的输入锁 (勿复用 _setChatLock, 那是主聊天的)
  const input=document.getElementById('opt-discuss-input');
  const btn=document.getElementById('opt-discuss-send');
  if(input)input.disabled=locked;
  if(btn){btn.disabled=locked;btn.textContent=locked?'思考中…':'发送';}
}
async function optDiscussSend(){
  // iter-74: 调真实后端 POST /api/secretary/chat (discuss_context 通道, Codex iter-73 已落地)。
  // 讨论回复仅由 HTTP 响应渲染 (WS chat_reply 已按 action_taken=opt_discuss 分流), 防双渲染。
  if(_optDiscussSending)return;
  const input=document.getElementById('opt-discuss-input');
  const text=input.value.trim();
  if(!text)return;
  input.value='';
  const key=optDiscussTopicKey();
  if(!_optDiscuss.topics[key])_optDiscuss.topics[key]=[];
  _optDiscuss.topics[key].push({role:'user',content:text,ts:Date.now()/1000});
  optDiscussSave();
  _optDiscussSending=true;
  _optDiscussInflight=true;
  _optDiscussLock(true);
  renderOptDiscuss();
  const t0=Date.now();
  const abortCtrl=new AbortController();
  const timeout=setTimeout(()=>abortCtrl.abort(),60000);
  try{
    const r=await apiFetch('/api/secretary/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:text,conv_id:_activeConvId,discuss_context:{topic:key}}),signal:abortCtrl.signal});
    clearTimeout(timeout);
    if(r.ok){
      const d=await r.json();
      const reply=(d&&d.reply)?d.reply:'[秘书返回空回复]';
      _optDiscuss.topics[key].push({role:'assistant',content:reply,ts:(d&&d.timestamp)||Date.now()/1000});
      optDiscussSave();
      const elapsed=((Date.now()-t0)/1000).toFixed(1);
      showToast(`💬 秘书已回复 (${elapsed}s)`, 'info', 2500);
      // 不调 loadConversations(): 会触发 switchConversation→loadChatHistory 重绘
      // 主聊天窗口, 讨论场景下副作用明显; 侧边栏时间下次自然刷新
    }else if(r.status===503){
      // 秘书未激活 (check_secretary): 明确提示, 不静默失败不落空回复
      const msg='🔕 秘书未激活, 无法讨论。请在 Station 面板点击「启动秘书」后重试。';
      _optDiscuss.topics[key].push({role:'assistant',content:msg,ts:Date.now()/1000});
      optDiscussSave();
      showToast('秘书未激活, 无法讨论','err');
    }else{
      const msg='[接口错误] HTTP '+r.status;
      _optDiscuss.topics[key].push({role:'assistant',content:msg,ts:Date.now()/1000});
      optDiscussSave();
      showToast('讨论接口错误: HTTP '+r.status,'err');
    }
  }catch(e){
    clearTimeout(timeout);
    const msg=e.name==='AbortError'?'[超时] 秘书响应超过60s,请重试':'[通信失败] '+e.message;
    _optDiscuss.topics[key].push({role:'assistant',content:msg,ts:Date.now()/1000});
    optDiscussSave();
    showToast(e.name==='AbortError'?'秘书响应超时':'讨论通信失败','err');
  }finally{
    _optDiscussSending=false;
    _optDiscussInflight=false;
    _optDiscussLock(false);
    renderOptDiscuss();
    if(!_optDiscussCollapsed)input.focus();
  }
}
optDiscussLoad(); // iter-73: 脚本执行时载入讨论历史 (声明区已就绪, 避免 TDZ)
function optMockGuardianStart(){
  // mock 守护: 周期推进 queued → running → completed, 演示完整状态流转
  if(optGuardTimer||!optMockMode)return;
  optGuardTimer=setInterval(()=>{
    if(!optMockMode){clearInterval(optGuardTimer);optGuardTimer=null;return;}
    const now=Date.now()/1000;
    let changed=false;
    const running=optGroup(optItems,'running')[0];
    if(running&&now-(running.updated_at||0)>=4){running.status='completed';running.updated_at=now;changed=true;optDispatch('workstation_optimization_completed',{item:running});}
    if(!optGroup(optItems,'running').length){
      const next=optGroup(optItems,'queued').sort((a,b)=>(OPT_PRI_ORDER[a.priority]||2)-(OPT_PRI_ORDER[b.priority]||2))[0];
      if(next){next.status='running';next.updated_at=now;changed=true;optDispatch('workstation_optimization_updated',{item:next});}
    }
    if(changed)optSaveLS();
  },5000);
}

