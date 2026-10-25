// ── P2P 主机通讯 ──
let p2pCurrentTarget=null;
async function refreshP2PHosts(){
  try{
    const r=await apiFetch('/api/hosts');const d=await r.json();
    const hosts=(d.hosts||[]).filter(h=>h.online&&h.device_id!=='');
    const sel=document.getElementById('p2p-host-select');
    const currentVal=sel.value;
    sel.innerHTML='<option value="">选择目标主机...</option>';
    hosts.forEach(h=>{
      const name=h.device_name||h.hostname||'未知';
      sel.innerHTML+=`<option value="${h.device_id}">${esc(name)} (${esc(h.ip||'-')})</option>`;
    });
    if(currentVal)sel.value=currentVal;
  }catch(e){console.error('刷新P2P主机列表失败:',e);}
}
async function loadP2PMessages(){
  const sel=document.getElementById('p2p-host-select');
  const deviceId=sel.value;
  p2pCurrentTarget=deviceId||null;
  const info=document.getElementById('p2p-target-info');
  const container=document.getElementById('p2p-messages');
  if(!deviceId){
    info.textContent='';
    container.innerHTML='<div class="empty"><div class="icon">📡</div><p>选择一台主机开始通讯</p><p class="hint">支持文字聊天和文件传输</p></div>';
    return;
  }
  const host=allHosts.find(h=>h.device_id===deviceId);
  info.textContent=host?`目标: ${host.device_name||host.hostname||'未知'} (${host.ip||'-'})`:'';
  try{
    const r=await apiFetch(`/api/p2p/messages/${encodeURIComponent(deviceId)}`);
    const d=await r.json();
    container.innerHTML='';
    const msgs=d.messages||[];
    if(!msgs.length){
      container.innerHTML='<div class="empty"><div class="icon">📡</div><p>暂无消息记录</p><p class="hint">发送一条消息开始对话</p></div>';
      return;
    }
    msgs.forEach(m=>appendP2PMessage(m));
  }catch(e){
    console.error('加载P2P消息失败:',e);
    container.innerHTML='<div class="empty"><div class="icon">📡</div><p>加载失败</p></div>';
  }
}
function appendP2PMessage(msg){
  const container=document.getElementById('p2p-messages');
  const empty=container.querySelector('.empty');
  if(empty)empty.remove();
  const div=document.createElement('div');
  if(msg.type==='system'){
    div.className='chat-msg system';
    div.innerHTML=esc(msg.content);
  }else if(msg.type==='file'){
    const role=msg.direction==='out'?'user':'assistant';
    div.className='chat-msg '+role;
    const statusIcon=msg.status==='success'?'✅':'❌';
    div.innerHTML=`<div class="file-info"><span class="file-icon">📎</span><span class="file-name">${esc(msg.filename||'文件')}</span><span class="file-size">${fmtSize(msg.size||0)}</span> ${statusIcon}</div><div class="msg-meta">${msg.direction==='out'?'本机':esc(msg.from_name||'对方')} · ${fmtTime(msg.timestamp)}</div>`;
  }else{
    const role=msg.direction==='out'?'user':'assistant';
    div.className='chat-msg '+role;
    const senderName=msg.direction==='out'?'本机':(msg.from_name||'对方');
    div.innerHTML=esc(msg.content)+'<div class="msg-meta">'+senderName+' · '+fmtTime(msg.timestamp)+'</div>';
  }
  container.appendChild(div);
  container.scrollTop=container.scrollHeight;
}
function onP2PChatMessage(data){
  const targetId=data.direction==='out'?data.to_device_id:data.from_device_id;
  if(targetId===p2pCurrentTarget){
    appendP2PMessage(data);
  }else{
    showToast(`📡 收到来自 ${data.from_name||'未知主机'} 的消息`,'info');
  }
}
async function sendP2PMessage(){
  const input=document.getElementById('p2p-input');
  const text=input.value.trim();
  if(!text)return;
  if(!p2pCurrentTarget){alert('请先选择目标主机');return;}
  input.value='';
  try{
    const r=await apiFetch('/api/p2p/send',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({target_device_id:p2pCurrentTarget,message:text})});
    const d=await r.json();
    if(!d.ok){alert('发送失败: '+(d.detail||'未知错误'));}
  }catch(e){alert('发送失败: '+e.message);}
}
async function sendP2PFile(){
  const fileInput=document.getElementById('p2p-file-input');
  const file=fileInput.files[0];
  if(!file)return;
  if(!p2pCurrentTarget){alert('请先选择目标主机');fileInput.value='';return;}
  const container=document.getElementById('p2p-messages');
  const empty=container.querySelector('.empty');
  if(empty)empty.remove();
  const uploadDiv=document.createElement('div');
  uploadDiv.className='chat-msg user';
  uploadDiv.innerHTML=`<div class="file-info"><span class="file-icon">📎</span><span class="file-name">${esc(file.name)}</span><span class="file-size">${fmtSize(file.size)}</span> ⏳ 上传中...</div>`;
  container.appendChild(uploadDiv);
  container.scrollTop=container.scrollHeight;
  const formData=new FormData();
  formData.append('file',file);
  formData.append('target_device_id',p2pCurrentTarget);
  try{
    const r=await apiFetch('/api/p2p/transfer',{method:'POST',body:formData});
    const d=await r.json();
    uploadDiv.remove();
    if(!d.ok&&d.detail){alert('传输失败: '+d.detail);}
  }catch(e){
    uploadDiv.remove();
    alert('传输失败: '+e.message);
  }finally{fileInput.value='';}
}

