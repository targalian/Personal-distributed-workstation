'use strict';
function initBackupUi(){
 $('export-backup').disabled=mesh.getRole()!=='boss';
 $('export-backup').onclick=()=>action(async()=>{if(!conv)throw Error('请先选择要备份的对话');const r=await request('/api/station/memory-backups/export?conv_id='+encodeURIComponent(conv),{});$('status').textContent='快照已创建：'+r.bundle_id.slice(0,12);await loadBackups();});
 $('refresh-backups').onclick=()=>action(loadBackups);
}
async function loadBackups(){
 const [snapshots,peers]=await Promise.all([request('/api/station/private-bundles'),request('/api/station/private-bundle-peers')]);
 const selected=$('backup-peer').value;$('backup-peer').replaceChildren(new Option('选择已登记工作站',''));
 for(const p of peers.items||[])$('backup-peer').append(new Option((p.name||p.device_id)+' · '+p.ip,p.device_id));$('backup-peer').value=selected;
 $('backups').replaceChildren();
 for(const item of snapshots.items||[]){
  if(item.kind!=='memory')continue;
  const card=row('backups',`${new Date(item.created_at*1000).toLocaleString()} · ${(item.bytes/1024).toFixed(1)} KiB\n项目：${item.scope.project_id||'无项目'} · ${(item.scope.conv_ids||[]).length}个对话\n快照 ${item.bundle_id}`);
  const copy=button(card,'复制到所选工作站',async()=>{const peer=$('backup-peer').value;if(!peer)throw Error('请选择目标工作站');await request('/api/station/private-bundles/'+item.bundle_id+'/push',{peer_id:peer});$('status').textContent='目标工作站已确认完整快照；可在目标实例恢复';},true);copy.disabled=mesh.getRole()!=='boss';
  const restore=button(card,'在本机恢复',async()=>{if(!confirm('确认恢复这份快照？目标必须没有同项目会话；原始任务不会自动执行。'))return;await request('/api/station/memory-backups/'+item.bundle_id+'/restore',{confirmed:true});const d=await request('/api/conversations');conversations=d.conversations||[];$('conversations').replaceChildren(new Option('选择对话',''));for(const c of conversations)$('conversations').append(new Option(c.title||c.id,c.id));$('status').textContent='恢复完成，可选择对话查看来源和记忆';},true);restore.disabled=mesh.getRole()!=='boss';
  const remove=button(card,'删除此快照',async()=>{if(!confirm('删除这份备份文件？已恢复的对话与原消息会保留。'))return;await request('/api/station/private-bundles/'+item.bundle_id+'/delete',{confirmed:true});await loadBackups();},true);remove.disabled=mesh.getRole()!=='boss';
 }
 if(!(snapshots.items||[]).some(s=>s.kind==='memory'))row('backups','尚无记忆快照');
}
