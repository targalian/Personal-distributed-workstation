/* Dashboard adapter; domain sources execute together with the original global contracts. */
const _meshClient = MeshTransport.create({onAuthChange: updateAuthStatus, onAuthRequired: updateAuthStatus});
function _ensureMeshToken(){ return _meshClient.ensure(); }
async function apiFetch(url,opts){
  const response=await _meshClient.request(url,opts);
  if(!response.ok){
    let detail=response.statusText;
    try{detail=(await response.clone().json()).detail||detail;}catch{}
    if(response.status===401)detail='请先登录：'+detail;
    throw new Error(`${response.status} ${detail}`);
  }
  return response;
}
function updateAuthStatus(){
  const status=_meshClient.getAuthState();
  const required=status.authEnabled&&!status.role;
  const button=document.getElementById('auth-login');
  if(button)button.textContent=status.role?`身份: ${status.role}`:'登录';
  const banner=document.getElementById('auth-status');
  if(banner){
    banner.hidden=!required;
    banner.textContent='尚未登录或令牌已失效，主机数据暂不可用。请点击右上角“登录”输入个人令牌。';
  }
  if(required){
    ['st-online','st-offline','stat-total','stat-online','stat-offline'].forEach(id=>{
      const element=document.getElementById(id);if(element)element.textContent='—';
    });
    const hostList=document.getElementById('station-hosts-body');
    if(hostList)hostList.textContent='请先登录以查看主机';
  }
  const secretaryButton=document.getElementById('secretary-btn');
  if(secretaryButton){
    secretaryButton.disabled=required||(status.authEnabled&&status.role!=='boss');
    secretaryButton.title=required?'请先登录':status.role&&status.role!=='boss'?'需要管理员身份':'';
  }
}
function showAuthLogin(){
  const overlay=document.getElementById('overlay'),modal=document.getElementById('modal');
  modal.innerHTML='<h2>登录工作站</h2><p>输入管理员分配的个人令牌。本站管理员也可使用本机 ~/.lan_mesh/mesh_token 文件中的节点令牌。</p>'+
    '<label for="auth-token-input">个人令牌</label><input id="auth-token-input" class="input" type="password" autocomplete="off" style="width:100%;margin:12px 0">'+
    '<div id="auth-login-error" role="alert"></div><button id="auth-apply" class="btn primary">登录</button> '+
    '<button id="auth-logout" class="btn">退出登录</button> <button id="auth-close" class="btn">关闭</button>';
  overlay.classList.add('show');
  const field=document.getElementById('auth-token-input');
  const apply=document.getElementById('auth-apply');
  apply.onclick=async()=>{
    if(!field.value.trim()){document.getElementById('auth-login-error').textContent='请输入令牌';return;}
    apply.disabled=true;
    try{
      _meshClient.setUserToken(field.value);await _meshClient.ensure();
      if(!_meshClient.getRole())throw new Error('令牌无效，请核对后重试');
      field.value='';overlay.classList.remove('show');updateAuthStatus();
      refreshStation();refreshAllState();if(!ws||ws.readyState>1)connectWS();
    }catch(error){document.getElementById('auth-login-error').textContent=error.message;}
    finally{apply.disabled=false;}
  };
  field.onkeydown=event=>{if(event.key==='Enter')apply.click();};
  document.getElementById('auth-logout').onclick=async()=>{
    _meshClient.setUserToken('');await _meshClient.ensure();overlay.classList.remove('show');updateAuthStatus();
    if(ws)ws.close();
  };
  document.getElementById('auth-close').onclick=()=>overlay.classList.remove('show');
  field.focus();
}
document.addEventListener('DOMContentLoaded',()=>{
  const button=document.createElement('button');button.id='auth-login';button.className='btn';
  button.style.whiteSpace='nowrap';document.querySelector('.topbar').style.flexWrap='wrap';
  button.textContent='登录';button.onclick=showAuthLogin;document.querySelector('.ws-status').prepend(button);
  const banner=document.createElement('div');banner.id='auth-status';banner.setAttribute('role','alert');
  banner.style.cssText='padding:12px 20px;background:var(--surface2);color:var(--yellow);';
  banner.hidden=true;document.querySelector('.topbar').after(banner);updateAuthStatus();
});
_ensureMeshToken();
