/* One document drives both the form and the graph. Drafts survive conversation switches. */
window.ProjectBriefUI = (() => {
  const panel = document.getElementById('project-brief-panel');
  const drafts = new Map();
  const generationJobs = new Map(), pendingStarts = new Map();
  let generationTimer = null, focusReview = false, generationEpoch = 0;
  let cid = '', state = null, tab = 'document', selected = null, edgeSelected = -1;
  let connecting = false, connectSource = null, drag = null, boxes = new Map();
  let view = {x:0,y:0,w:1100,h:680}, requestId = 0, busy = false;
  let editGroup = null;
  const historyLimit = 50, inputGroupMs = 1000;
  const copy = value => JSON.parse(JSON.stringify(value));
  const escape = value => String(value ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
  const statuses = {discussion:'待讨论',proposed:'建议',confirmed:'已确认'};
  const taskStatus = value => ({pending:'待执行',running:'执行中',completed:'已完成',failed:'失败',
    delivered:'已交付',accepted:'已验收',rejected:'已退回',paused:'已暂停',cancelled:'已取消',
    unknown:'待核实',unavailable:'记录不可用（尚未创建或已删除）'}[value]||value);
  panel.tabIndex=-1;
  panel.innerHTML = `<div class="brief-toolbar"><strong>项目描述.md</strong>
    <button class="btn" data-action="generate">从对话整理</button><button class="btn primary" data-action="save">保存</button>
    <button class="btn" data-action="export">导出 .md</button><button class="btn" data-action="reload">重新加载</button>
    <button class="btn" data-action="undo" title="撤销（Ctrl/Cmd+Z）" disabled>撤销</button>
    <button class="btn" data-action="redo" title="重做（Ctrl/Cmd+Shift+Z 或 Ctrl+Y）" disabled>重做</button>
    <button class="btn" data-action="dispatch">确认并下发</button>
    </div><div class="brief-status" role="status" aria-live="polite"></div>
    <div class="brief-generation" role="status" aria-live="polite" hidden></div>
    <div class="brief-tabs" role="tablist" aria-label="项目描述视图">
      <button class="btn" role="tab" data-tab="document">文档</button><button class="btn" role="tab" data-tab="graph">需求 DAG</button>
      <button class="btn" role="tab" data-tab="preview">Markdown 预览</button>
      <button class="btn" role="tab" data-tab="review">AI 建议</button>
      <button class="btn" role="tab" data-tab="history">版本历史</button>
      <button class="btn" role="tab" data-tab="handoffs">下发记录</button>
    </div><div class="brief-content"></div>`;
  const content = panel.querySelector('.brief-content');
  const status = panel.querySelector('.brief-status');
  const generationStatus = panel.querySelector('.brief-generation');
  function note(message) { status.textContent = message; status.classList.toggle('dirty', !!state?.dirty); }
  function summary() {
    if (!state) return note('选择一个对话，开始沉淀项目描述。');
    const pending = state.document.nodes.filter(n => n.status === 'discussion').length;
    note(`v${state.revision} · ${state.dirty?'有未保存修改':'已同步'} · ${pending} 个待讨论节点${state.proposal?' · 有 AI 建议待审阅':''} · 需求草稿`);
  }
  function remember() { if (cid && state) drafts.set(cid, state); }
  function same(a,b) {
    // API serialization may reorder node fields; equality must compare their values.
    const stable=value=>JSON.stringify(value,(_,item)=>item&&typeof item==='object'&&!Array.isArray(item)
      ?Object.fromEntries(Object.keys(item).sort().map(key=>[key,item[key]])):item);
    return stable(a)===stable(b);
  }
  function pushHistory(stack,document) { stack.push(copy(document)); if(stack.length>historyLimit)stack.shift(); }
  function historyButtons() {
    for(const action of ['undo','redo'])panel.querySelector(`[data-action="${action}"]`).disabled=!state?.[action].length;
  }
  function checkpoint(element) {
    const now=Date.now();
    if(!editGroup || editGroup.state!==state || editGroup.element!==element || now-editGroup.time>inputGroupMs){
      pushHistory(state.undo,state.document);
    }
    editGroup={state,element,time:now};state.redo=[];
  }
  function changed() { focusReview=false; state.dirty=!same(state.document,state.savedDocument); remember(); summary(); historyButtons();panel.querySelector('[data-action="dispatch"]').disabled=!state.revision||state.dirty; }
  function travel(action) {
    editGroup=null;
    const source=state[action],destination=state[action==='undo'?'redo':'undo'];
    if(!source.length)return;
    const focused=document.activeElement;
    const focusedNode=focused?.closest('[data-node]')?.dataset.node;
    const field=['field','section','nodeField'].find(key=>focused?.dataset[key]);
    const value=field?focused.dataset[field]:null,caret=focused?.selectionStart;
    const scroll=content.scrollTop;
    pushHistory(destination,state.document);state.document=source.pop();
    if(!state.document.nodes.some(n=>n.id===selected))selected=null;
    edgeSelected=-1;connectSource=null;drag=null;
    changed();render();
    if(field){
      const attr=field==='nodeField'?'node-field':field;
      const input=content.querySelector(`[data-${attr}="${value}"]`);
      input?.focus({preventScroll:true});
      if(input?.setSelectionRange && caret!==null)input.setSelectionRange(caret,caret);
    }else if(focusedNode)(content.querySelector(`[data-node="${focusedNode}"]`)||panel).focus({preventScroll:true});
    content.scrollTop=scroll;
  }
  async function request(suffix='', options={}, target=cid) {
    const response = await apiFetch(`/api/conversations/${encodeURIComponent(target)}/brief${suffix}`, options);
    if (!response.ok) {
      let detail; try {detail=(await response.json()).detail;} catch {detail=response.statusText;}
      const error = new Error(response.status===409?`版本冲突：${typeof detail==='string'?detail:'项目描述已更新'}。当前修改已保留，请重新加载后审阅或合并。`:typeof detail==='string'?detail:JSON.stringify(detail));
      error.httpStatus=response.status;throw error;
    }
    return response;
  }
  function pendingStart(id) {
    if(pendingStarts.has(id))return pendingStarts.get(id);
    try {const item=JSON.parse(localStorage.getItem(`brief-generation:${id}`));
      if(item?.request_id&&Number.isInteger(item.expected_revision)){pendingStarts.set(id,item);return item;}
    } catch { /* In-memory recovery still works when browser storage is unavailable. */ }
    return null;
  }
  function keepStart(id, item) {
    if(item)pendingStarts.set(id,item);else pendingStarts.delete(id);
    try {if(item)localStorage.setItem(`brief-generation:${id}`,JSON.stringify(item));else localStorage.removeItem(`brief-generation:${id}`);}catch{}
  }
  function generationSummary(message='') {
    const job=generationJobs.get(cid), pending=pendingStart(cid);
    const active=['queued','running'].includes(job?.status);
    const labels={queued:'已登记',running:'正在整理',review:'整理完成',no_changes:'无新增建议',
      conflict:'结果已过期',failed:'整理失败',interrupted:'结果待核实'};
    generationStatus.hidden=!cid||(!job&&!pending&&!message);
    generationStatus.innerHTML=`<span>${escape(message||(job?`${labels[job.status]||job.status} · 基于 v${job.base_revision} · ${job.message}`:pending?'正在核实上次整理请求；再次点击会沿用同一请求，不重复提交。':''))}</span>
      ${job?.status==='conflict'?'<button class="btn" data-action="generation-export">导出过期结果</button>':''}`;
    const button=panel.querySelector('[data-action="generate"]');
    button.disabled=active;
    button.textContent=active?'正在整理…':pending?'核实／重试整理请求':'从对话整理';
  }
  function pollLater(id, token) {
    clearTimeout(generationTimer);
    if(id===cid&&token===requestId)generationTimer=setTimeout(()=>pollGeneration(id,token),1200);
  }
  async function pollGeneration(id, token) {
    if(id!==cid||token!==requestId)return;
    const pending=pendingStart(id),epoch=generationEpoch;
    try {
      const data=await(await request('/generation'+(pending?`?request_id=${encodeURIComponent(pending.request_id)}`:''),{},id)).json();
      if(id!==cid||token!==requestId||epoch!==generationEpoch)return;
      const previous=generationJobs.get(id),job=data.job;
      generationJobs.set(id,job);
      if(pending&&job?.id===pending.request_id)keepStart(id,null);
      generationSummary();
      if(job?.status==='review' && (previous?.id!==job.id || previous?.status!==job.status || !state?.proposal)){
        if(busy){pollLater(id,token);return;}
        const snapshot=state,revision=state?.revision;
        const brief=await(await request('',{},id)).json();
        if(id!==cid||token!==requestId||epoch!==generationEpoch)return;
        // Only merge proposal metadata. Never replace an editing buffer, history,
        // focus, or a newer save/review response with a background GET.
        if(!busy&&snapshot===state&&revision===state?.revision&&brief.revision===revision){
          const old=state.proposal?.id;
          state.proposal=brief.proposal;
          if(old!==brief.proposal?.id)state.reviewSelection=[];
          remember();summary();
          panel.querySelector('[data-tab="review"]').textContent=state.proposal?`AI 建议 (${state.proposal.changes.length})`:'AI 建议';
          if(focusReview&&!state.dirty&&state.proposal?.id===job.proposal_id){tab='review';render();}
          else if(tab==='review')renderReview();
          focusReview=false;
        }else if(!busy){generationSummary('整理已完成；本地内容已保留，请重新加载已保存版本后审阅。');}
        else {pollLater(id,token);return;}
      }
      if(['queued','running'].includes(job?.status))pollLater(id,token);
    } catch(error) {
      if(id!==cid||token!==requestId||epoch!==generationEpoch)return;
      generationSummary(error.httpStatus===404&&pending?'尚未找到上次请求，可点击「核实／重试整理请求」沿用原编号重试。':`整理状态暂不可用：${error.message}；将继续查询。`);
      pollLater(id,token);
    }
  }
  async function startGeneration() {
    generationEpoch++;
    const id=cid,token=requestId;
    let payload=pendingStart(id);
    if(!payload){
      const previous=generationJobs.get(id);
      if(['failed','interrupted'].includes(previous?.status)&&!confirm('上次整理可能已经产生模型费用。开始一次新的整理？'))return;
      await save();if(id!==cid||token!==requestId)return;
      payload={request_id:globalThis.crypto?.randomUUID?.()||`brief_${Date.now()}_${Math.random().toString(36).slice(2)}`,expected_revision:state.revision};
      keepStart(id,payload);
    }
    focusReview=true;
    try {
      const data=await(await request('/generation',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)},id)).json();
      generationJobs.set(id,data.job);keepStart(id,null);
    } catch(error) {
      if(error.httpStatus===409||error.httpStatus===422)keepStart(id,null);
      throw error;
    } finally {
      if(id===cid&&token===requestId){generationSummary();pollLater(id,token);}
    }
  }
  function received(data, previous=null) {
    const sameProposal=previous?.proposal?.id===data.proposal?.id;
    return {...data,dirty:false,savedDocument:copy(data.document),undo:previous?.undo||[],redo:previous?.redo||[],
      reviewSelection:sameProposal?previous?.reviewSelection||[]:[]};
  }
  function accept(data,previous=null) {
    editGroup=null;state=received(data,previous); remember(); render();
  }
  async function open(id) {
    remember(); editGroup=null;cid=id; state=drafts.get(id)||null; selected=null; edgeSelected=-1; drag=null; connectSource=null;
    const token=++requestId;
    clearTimeout(generationTimer);focusReview=false;
    render();if(id)pollLater(id,token);
    if (!id || state?.dirty) return;
    try { const data=await (await request()).json(); if(token===requestId && !state?.dirty){
      const previous=state?.revision===data.revision && same(state.document,data.document)?state:null;
      accept(data,previous);
    } }
    catch(error) {if(token===requestId)note(error.message);}
  }
  async function refresh(id) {
    if(id!==cid)return;
    if(state?.dirty){note('对话可能更新了项目描述；本地修改已保留。保存如遇版本冲突，请先导出草稿再合并。');return;}
    await open(id);
  }
  async function save() {
    if(!state)return;
    if(!state.dirty && state.revision)return;
    generationEpoch++;
    const target=cid, token=requestId,previous=state;
    editGroup=null;
    const data=await (await request('', {method:'PUT',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({document:state.document,expected_revision:state.revision})})).json();
    if(target===cid && token===requestId)accept(data,previous);
    else drafts.set(target,received(data,previous));
  }
  async function flush() {
    if(busy)throw new Error('项目描述正在保存或整理，请稍后发送。');
    if(!state?.dirty)return;
    busy=true;panel.setAttribute('aria-busy','true');panel.inert=true;
    try {await save();} finally {busy=false;panel.setAttribute('aria-busy','false');panel.inert=false;pollLater(cid,requestId);}
  }
  function render() {
    summary(); generationSummary(); historyButtons();panel.querySelectorAll('[data-tab]').forEach(el=>el.setAttribute('aria-selected',el.dataset.tab===tab));
    panel.querySelector('[data-action="dispatch"]').disabled=!state?.revision||state.dirty;
    panel.querySelector('[data-tab="review"]').textContent=state?.proposal?`AI 建议 (${state.proposal.changes.length})`:'AI 建议';
    if(!state){content.innerHTML='<div class="brief-empty">与秘书讨论项目背景、需求和想法，内容将整理到这里。<br>文档、模块框图与依赖关系可以共同编辑。</div>';return;}
    if(tab==='document')renderDocument(); else if(tab==='graph')renderGraph(); else if(tab==='review')renderReview(); else if(tab==='history')renderHistory(); else if(tab==='handoffs')renderHandoffs(); else renderPreview();
  }
  function renderDocument() {
    const opened=new Set([...content.querySelectorAll('details[open] textarea')].map(el=>el.dataset.section));
    content.innerHTML=`<label>项目名称<input data-field="title" maxlength="160" value="${escape(state.document.title)}"></label>
      <p class="brief-hint">空白信息保留为待讨论。模块、功能和验收点可在「需求 DAG」中编辑。顶部撤销/重做覆盖两种视图，保存后仍可撤销最近 50 步；刷新页面或加载新版本会清空历史。</p>`+
      Object.entries(state.section_labels).map(([key,label],i)=>`<details ${i<2||opened.has(key)?'open':''}><summary>${escape(label)} ${state.document.sections[key]?.trim()?'':'· 待讨论'}</summary>
      <label>${escape(label)}<textarea data-section="${key}" maxlength="12000" placeholder="待讨论：事实、建议、负责人及确认结论">${escape(state.document.sections[key]||'')}</textarea></label></details>`).join('');
  }
  // Local preview also works during conflicts, so unsaved work can always be exported.
  function markdown() {
    const d=state.document;
    let out=`# ${d.title}\n\n> 项目描述 · 版本 ${state.revision}${state.dirty?'（本地未保存草稿）':''} · 讨论草稿\n\n`;
    for(const [key,label] of Object.entries(state.section_labels))out+=`## ${label}\n\n${d.sections[key]?.trim()||'待讨论'}\n\n`;
    out+='## 模块与功能分解\n\n';
    const emit=(parent=null,level=0)=>d.nodes.filter(n=>n.parent_id===parent).forEach(n=>{
      out+=`${'  '.repeat(level)}- **${n.title.replace(/\n/g,' ')}** (${n.id}) · ${n.priority} · ${statuses[n.status]}\n`;
      for(const [label,val] of [['说明',n.description],['验收',n.acceptance],['负责人',n.owner]])if(val)out+=`${'  '.repeat(level+1)}- ${label}：${val.replace(/\n/g,' ')}\n`;
      emit(n.id,level+1);
    });emit();
    out+='\n## 依赖 DAG\n\n箭头表示前置依赖，框内嵌套表示包含关系，最多三层。\n\n```mermaid\nflowchart LR\n';
    const ids=new Map(d.nodes.map((n,i)=>[n.id,`n${i}`]));
    const label=s=>[...s.replace(/[\r\n]/g,' ')].map(c=>/[\p{L}\p{N} ]/u.test(c)?c:`#${c.codePointAt(0)};`).join('');
    const graph=(parent=null,level=1)=>d.nodes.filter(n=>n.parent_id===parent).forEach(n=>{
      const pad='  '.repeat(level);
      if(d.nodes.some(c=>c.parent_id===n.id)){out+=`${pad}subgraph ${ids.get(n.id)}["${label(n.title)}"]\n`;graph(n.id,level+1);out+=`${pad}end\n`;}
      else out+=`${pad}${ids.get(n.id)}["${label(n.title)}"]\n`;
    });graph();if(!d.nodes.length)out+='  empty["待讨论"]\n';
    for(const e of d.edges)out+=`  ${ids.get(e.source)} --> ${ids.get(e.target)}\n`;
    return out+'```\n';
  }
  function renderPreview(){content.innerHTML='<p class="brief-hint">预览当前编辑内容；保存后可导出正式版本。模块和 DAG 自动生成。</p><pre></pre>';content.querySelector('pre').textContent=markdown();}
  function reviewValue(value) {
    if(value?.nodes && value?.edges){
      const names=new Map(value.nodes.map(node=>[node.id,node.title]));
      const nodes=value.nodes.map(node=>`${node.title} · ${node.priority} · ${statuses[node.status]}\n所属框：${names.get(node.parent_id)||'顶层'}\n说明：${node.description||'待讨论'}\n验收：${node.acceptance||'待讨论'}\n负责人：${node.owner||'待讨论'}\n位置：${node.x}, ${node.y}${node.collapsed?' · 已折叠':''}`);
      const edges=value.edges.map(edge=>`${names.get(edge.source)} → ${names.get(edge.target)}`);
      return `模块与功能\n${nodes.join('\n\n')||'暂无节点'}\n\n前置依赖\n${edges.join('\n')||'暂无依赖'}`;
    }
    if(value && typeof value==='object')return JSON.stringify(value,null,2);
    if(typeof value==='boolean')return value?'是':'否';
    return Object.hasOwn(statuses,value)?statuses[value]:String(value??'')||'（空）';
  }
  function renderReview() {
    const proposal=state.proposal;
    if(!proposal){content.innerHTML='<div class="brief-empty">暂无待审阅建议。与秘书讨论或点击「从对话整理」后，先在这里比较差异，再选择要保存的修改。</div>';return;}
    const stale=proposal.base_revision!==state.revision;
    const selection=new Set(state.reviewSelection);
    content.innerHTML=`<p class="brief-hint">建议基于 v${proposal.base_revision}。勾选要采纳的内容；保存所选后，其余建议将被忽略。已保存文档在采纳前保持原样。结构变化整体采纳，确保节点、嵌套与依赖保持一致。</p>
      ${stale?'<p class="brief-review-warning" role="alert">文档版本已变化，这份建议已过期。请忽略后重新整理。</p>':''}
      ${state.dirty?'<p class="brief-review-warning" role="alert">有未保存修改，暂不能采纳。可先导出或撤销本地修改；若保存了新版本，请重新整理建议。</p>':''}
      <div class="brief-review-tools"><button class="btn" data-action="review-all" ${stale||state.dirty?'disabled':''}>全选</button>
      <button class="btn" data-action="review-none">清空选择</button>
      <button class="btn primary" data-action="review-apply" ${stale||state.dirty||!selection.size?'disabled':''}>保存所选，忽略其余</button>
      <button class="btn" data-action="review-discard">忽略全部建议</button><span>${selection.size}/${proposal.changes.length} 项已选</span></div>`+
      proposal.changes.map((change,index)=>`<details class="brief-change" open><summary>
        <label><input type="checkbox" data-review-index="${index}" ${selection.has(change.id)?'checked':''} ${stale||state.dirty?'disabled':''}>${escape(change.label)}</label></summary>
        <div class="brief-diff"><div><strong>当前内容</strong><pre>${escape(reviewValue(change.before))}</pre></div>
        <div><strong>AI 建议</strong><pre>${escape(reviewValue(change.after))}</pre></div></div></details>`).join('');
  }
  async function reviewProposal(discard) {
    if(!state.proposal)return;
    if(!discard && state.dirty)throw new Error('请先处理未保存修改，避免覆盖本地草稿。');
    generationEpoch++;
    const target=cid,token=requestId,previous=state;
    const data=await(await request('/review',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({
      proposal_id:state.proposal.id,expected_revision:state.revision,selected:discard?[]:state.reviewSelection,
    })})).json();
    if(discard && previous.dirty){
      previous.proposal=null;previous.reviewSelection=[];drafts.set(target,previous);
      if(target===cid&&token===requestId){state=previous;render();}
      return;
    }
    let history=previous;
    if(!discard && !same(previous.document,data.document)){
      pushHistory(history.undo,previous.document);history.redo=[];
    }else if(previous.revision!==data.revision||!same(previous.document,data.document))history=null;
    if(target===cid&&token===requestId)accept(data,history);
    else drafts.set(target,received(data,history));
  }
  async function loadHistory(more=false) {
    const current=state,target=cid,token=requestId;
    const suffix=more&&current.historyNext?`?before_revision=${current.historyNext}`:'';
    const data=await(await request('/history'+suffix)).json();
    current.historyList=more?[...(current.historyList||[]),...data.revisions]:data.revisions;
    current.historyNext=data.next_before;
    if(target===cid&&token===requestId&&state===current&&tab==='history')renderHistory();
  }
  async function loadHandoffs() {
    const current=state,target=cid,token=requestId;
    const data=await(await request('/handoffs')).json();current.handoffs=data.handoffs;
    if(target===cid&&token===requestId&&state===current&&tab==='handoffs')renderHandoffs();
  }
  function renderHandoffs() {
    const records=state.handoffs||[];
    const labels={prepared:'下发已登记，结果待核实',submitted:'已关联任务',uncertain:'结果待核实，不会自动重试'};
    content.innerHTML='<p class="brief-hint">每个已保存版本下发一次。确认记录保留当时的内容和任务 ID；之后改稿不改变执行依据。遇到结果待核实，请先在任务面板按 ID 检查，避免重复执行。</p><button class="btn" data-action="handoffs-refresh">刷新记录</button>'+
      records.map(record=>`<details class="brief-change" open><summary>v${record.revision} · ${escape(record.title)}</summary>
        <p>${escape(labels[record.status]||record.status)} · 任务状态：${escape(taskStatus(record.task_status))}</p>
        <p>任务 ID：<code>${escape(record.task_id)}</code></p><p>确认时间：${escape(new Date(record.confirmed_at*1000).toLocaleString())}</p>
        ${record.error?`<p class="brief-review-warning">${escape(record.error)}</p>`:''}
        <button class="btn" data-action="handoff-view" data-revision="${record.revision}">查看下发依据</button>
        <button class="btn" data-action="handoff-task" data-task="${escape(record.task_id)}">查看任务与产物</button></details>`).join('')+
      (!records.length?'<p class="brief-empty">暂无下发记录。先保存项目描述，再点击「确认并下发」。</p>':'')+
      (state.handoffDetail?`<details open><summary>下发依据 v${state.handoffDetail.revision}</summary><pre>${escape(state.handoffDetail.markdown)}</pre></details>`:'');
  }
  async function dispatchBrief() {
    if(state.dirty||!state.revision)throw new Error('请先保存并审阅当前内容，再下发。');
    if(!confirm(`确认下发已保存的项目描述 v${state.revision}「${state.document.title}」给 PM 执行？未采纳的 AI 建议不包含在内。`))return;
    const target=cid,token=requestId;
    const result=await(await request('/handoffs',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({expected_revision:state.revision})})).json();
    if(target===cid&&token===requestId){
      tab='handoffs';render();await loadHandoffs();
      if(target===cid&&token===requestId)note(`项目描述 v${result.revision} 已登记 · 任务 ${result.task_id} · ${taskStatus(result.task_status)}`);
    }
  }
  async function inspectHistory(revision) {
    const current=state,target=cid,token=requestId;
    const data=await(await request(`/history/${revision}`)).json();
    current.historyDetail=data;
    if(target===cid&&token===requestId&&state===current&&tab==='history')renderHistory();
  }
  function renderHistory() {
    const entries=state.historyList||[],detail=state.historyDetail;
    const sources={manual:'手工保存',ai_review:'采纳 AI 建议',restore:'恢复历史',legacy:'升级前版本'};
    const time=value=>value?new Date(value*1000).toLocaleString():'时间未知';
    const blocked=state.dirty||!detail||detail.current_revision!==state.revision||!detail.changes.length;
    content.innerHTML=`<p class="brief-hint">保存和采纳建议会保留版本。选择旧版本与当前已保存内容比较；恢复会创建新版本，历史记录不变。升级前只能保留当时的最新版本。</p>
      ${state.dirty?'<p class="brief-review-warning" role="alert">当前有未保存修改，请先保存或导出并处理草稿，再恢复历史。</p>':''}
      <div class="brief-review-tools"><button class="btn" data-action="history-refresh">刷新列表</button>
      ${state.historyNext?'<button class="btn" data-action="history-more">加载更早版本</button>':''}</div>
      ${entries.length?`<label>历史版本<select data-history-choice>${entries.map(entry=>`<option value="${entry.revision}" ${detail?.revision===entry.revision?'selected':''}>v${entry.revision} · ${escape(sources[entry.source]||entry.source)}${entry.restored_from?`（来自 v${entry.restored_from}）`:''} · ${escape(time(entry.updated_at))} · ${escape(entry.title)}</option>`).join('')}</select></label>
      <div class="brief-review-tools"><button class="btn" data-action="history-view">查看版本与差异</button>
      <button class="btn primary" data-action="history-restore" ${blocked?'disabled':''}>恢复所看版本为新版本</button></div>`:'<p class="brief-empty">暂无已保存的历史版本。</p>'}
      ${detail?`<p>正在查看 v${detail.revision}，对比已保存 v${detail.current_revision} · ${detail.changes.length} 项差异</p>
        ${detail.current_revision!==state.revision?'<p class="brief-review-warning" role="alert">当前页面版本与服务端不同，请重新加载后比较。</p>':''}
        <details><summary>查看该版本 Markdown</summary><pre>${escape(detail.markdown)}</pre></details>`+
        detail.changes.map(change=>`<details class="brief-change" open><summary>${escape(change.label)}</summary><div class="brief-diff"><div><strong>当前已保存内容</strong><pre>${escape(reviewValue(change.before))}</pre></div><div><strong>历史版本内容</strong><pre>${escape(reviewValue(change.after))}</pre></div></div></details>`).join(''):''}`;
  }
  async function restoreHistory() {
    generationEpoch++;
    const detail=state.historyDetail;
    if(state.dirty)throw new Error('请先处理未保存修改，再恢复历史。');
    if(!detail||detail.current_revision!==state.revision)throw new Error('请重新加载并查看历史版本。');
    if(!confirm(`将 v${detail.revision} 的内容保存为新版本？当前及旧版本都会保留。`))return;
    const target=cid,token=requestId,previous=state;
    const data=await(await request(`/history/${detail.revision}/restore`,{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({expected_revision:state.revision})})).json();
    pushHistory(previous.undo,previous.document);previous.redo=[];
    if(target===cid&&token===requestId){accept(data,previous);await loadHistory();}
    else drafts.set(target,received(data,previous));
  }
  function download(text) {
    const url=URL.createObjectURL(new Blob([text],{type:'text/markdown;charset=utf-8'}));
    const a=document.createElement('a');a.href=url;a.download='项目描述.md';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
  }
  function ancestors(id) { const result=[];let n=state.document.nodes.find(n=>n.id===id);while(n?.parent_id){result.push(n.parent_id);n=state.document.nodes.find(p=>p.id===n.parent_id);}return result; }
  function descendants(id) {return state.document.nodes.filter(n=>ancestors(n.id).includes(id));}
  function validate() {
    const d=state.document;
    for(const n of d.nodes)if(ancestors(n.id).length>2)throw new Error('最多三层：模块 → 功能 → 子功能。');
    const adjacency=new Map(d.nodes.map(n=>[n.id,[]]));const seen=new Set();
    for(const e of d.edges){
      if(e.source===e.target || ancestors(e.source).includes(e.target) || ancestors(e.target).includes(e.source))throw new Error('包含关系不能再作为依赖连线。');
      const key=e.source+'>'+e.target;if(seen.has(key))throw new Error('这条依赖已存在。');seen.add(key);adjacency.get(e.source).push(e.target);
    }
    const active=new Set(),done=new Set();
    function visit(id){if(active.has(id))throw new Error('该连线会形成循环，请保持 DAG 无环。');if(done.has(id))return;active.add(id);adjacency.get(id).forEach(visit);active.delete(id);done.add(id);}
    d.nodes.forEach(n=>visit(n.id));
  }
  function mutate(fn){editGroup=null;const before=copy(state.document),previousSelected=selected;try{fn();validate();if(!same(before,state.document)){pushHistory(state.undo,before);state.redo=[];changed();}render();}catch(e){state.document=before;selected=previousSelected;note(e.message);}}
  function sizes() {
    const result=new Map();
    const measure=n=>{const children=state.document.nodes.filter(c=>c.parent_id===n.id);let w=230,h=85;
      if(children.length&&!n.collapsed){w=280;h=140;for(const c of children){const size=measure(c);w=Math.max(w,c.x+size.w+20);h=Math.max(h,c.y+size.h+20);}}
      const value={w,h};result.set(n.id,value);return value;};
    state.document.nodes.filter(n=>!n.parent_id).forEach(measure);return result;
  }
  function renderGraph() {
    content.innerHTML=`<div class="brief-graph-wrap"><div class="brief-graph-tools">
      <button class="btn" data-action="add">＋ 模块</button><button class="btn" data-action="child">＋ 子节点</button>
      <button class="btn ${connecting?'active':''}" data-action="connect">依赖连线</button><button class="btn" data-action="delete">删除选中</button>
      <button class="btn" data-action="layout">自动布局</button><button class="btn" data-action="fit">适配</button></div>
      <p class="brief-hint">拖动节点调整位置，拖动空白平移，滚轮缩放。选中框后添加子节点，最多三层；双击框折叠。箭头：前置 → 后续。</p>
      <svg class="brief-canvas" aria-label="可编辑项目需求 DAG" role="img"></svg><div class="brief-node-editor" hidden></div></div>`;
    draw();renderNodeEditor();
    const svg=content.querySelector('svg');
    svg.addEventListener('pointerdown',pointerDown);svg.addEventListener('pointermove',pointerMove);
    svg.addEventListener('pointerup',pointerUp);svg.addEventListener('pointercancel',pointerUp);
    svg.addEventListener('wheel',event=>{event.preventDefault();const p=point(event);const scale=event.deltaY>0?1.12:1/1.12;const w=Math.min(12000,Math.max(220,view.w*scale));const factor=w/view.w;view={x:p.x-(p.x-view.x)*factor,y:p.y-(p.y-view.y)*factor,w,h:view.h*factor};draw();},{passive:false});
    svg.addEventListener('dblclick',event=>{const id=event.target.closest('[data-node]')?.dataset.node;if(id)mutate(()=>{const n=state.document.nodes.find(n=>n.id===id);n.collapsed=!n.collapsed;});});
    svg.addEventListener('keydown',event=>{const id=event.target.closest('[data-node]')?.dataset.node;if(!id)return;
      if(event.key==='Enter'){selected=id;renderNodeEditor();draw();}
      const offsets={ArrowLeft:[-10,0],ArrowRight:[10,0],ArrowUp:[0,-10],ArrowDown:[0,10]};
      if(offsets[event.key]){event.preventDefault();mutate(()=>{const n=state.document.nodes.find(n=>n.id===id);n.x=Math.max(16,n.x+offsets[event.key][0]);n.y=Math.max(n.parent_id?55:16,n.y+offsets[event.key][1]);});content.querySelector(`[data-node="${id}"]`)?.focus();}
    });
  }
  function draw() {
    const svg=content.querySelector('svg');if(!svg)return;
    const dims=sizes();boxes=new Map();let nodes='';
    function emit(parent=null,ox=0,oy=0,level=0){for(const n of state.document.nodes.filter(n=>n.parent_id===parent)){
      const {w,h}=dims.get(n.id);const x=ox+n.x,y=oy+n.y;boxes.set(n.id,{x,y,w,h});
      const hasChildren=state.document.nodes.some(c=>c.parent_id===n.id);const color=n.status==='confirmed'?'#4ade80':n.status==='proposed'?'#5b8cff':'#fbbf24';
      nodes+=`<g class="brief-node" data-node="${n.id}" tabindex="0" role="button" aria-label="${escape(n.title)}" transform="translate(${x},${y})">
        <rect width="${w}" height="${h}" rx="10" fill="${['#191f30','#232d43','#303d56'][level]}" stroke="${selected===n.id?'#ffffff':color}" stroke-width="${selected===n.id?3:1.5}"/>
        <text x="12" y="23">${escape((hasChildren?(n.collapsed?'▸ ':'▾ '):'')+n.title.slice(0,24))}</text>
        <text x="12" y="43" style="font-size:11px;fill:${color}">${n.priority} · ${statuses[n.status]} · 第 ${level+1} 层${n.collapsed?' · 已折叠':''}</text>
        ${!hasChildren?`<text x="12" y="65" style="font-size:11px;fill:#a9b4cc">${escape((n.description||'点击编辑描述与验收').slice(0,25))}</text>`:''}</g>`;
      if(!n.collapsed)emit(n.id,x,y,level+1);
    }}emit();
    const representative=id=>{while(!boxes.has(id)){id=state.document.nodes.find(n=>n.id===id)?.parent_id;if(!id)return null;}return id;};
    let edges='';state.document.edges.forEach((e,i)=>{const aId=representative(e.source),bId=representative(e.target);if(!aId||!bId||aId===bId)return;
      const a=boxes.get(aId),b=boxes.get(bId);const ax=a.x+a.w/2,ay=a.y+a.h/2,bx=b.x+b.w/2,by=b.y+b.h/2;
      let path;
      if(Math.abs(bx-ax)>Math.abs(by-ay)){
        const x1=bx>ax?a.x+a.w:a.x,x2=bx>ax?b.x:b.x+b.w,mid=(x1+x2)/2;
        path=`M${x1},${ay} C${mid},${ay} ${mid},${by} ${x2},${by}`;
      }else{
        const y1=by>ay?a.y+a.h:a.y,y2=by>ay?b.y:b.y+b.h,mid=(y1+y2)/2;
        path=`M${ax},${y1} C${ax},${mid} ${bx},${mid} ${bx},${y2}`;
      }
      edges+=`<g data-edge="${i}" style="cursor:pointer"><path d="${path}" fill="none" stroke="transparent" stroke-width="16"/><path d="${path}" fill="none" stroke="${edgeSelected===i?'#f87171':'#8baaff'}" stroke-width="2" marker-end="url(#brief-arrow)"/></g>`;
    });
    svg.setAttribute('viewBox',`${view.x} ${view.y} ${view.w} ${view.h}`);
    svg.innerHTML=`<defs><marker id="brief-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0 0 L10 5 L0 10 Z" fill="#8baaff"/></marker></defs>${nodes}${edges}`;
    if(!state.document.nodes.length)svg.innerHTML+='<text x="35" y="65">从对话整理，或点击「＋ 模块」开始绘制。</text>';
  }
  function point(event){const svg=content.querySelector('svg');const p=svg.createSVGPoint();p.x=event.clientX;p.y=event.clientY;return p.matrixTransform(svg.getScreenCTM().inverse());}
  function pointerDown(event){if(event.button!==0)return;const element=event.target.closest('[data-node]');const edge=event.target.closest('[data-edge]');const p=point(event);
    editGroup=null;
    if(edge){edgeSelected=Number(edge.dataset.edge);selected=null;draw();renderNodeEditor();return;}
    if(element){const id=element.dataset.node;selected=id;edgeSelected=-1;
      if(connecting){if(!connectSource){connectSource=id;note('已选前置节点，请点击依赖它的后续节点。');draw();}else{const source=connectSource;connectSource=null;mutate(()=>state.document.edges.push({source,target:id}));}return;}
      const n=state.document.nodes.find(n=>n.id===id);drag={id,p,x:n.x,y:n.y,before:copy(state.document),moved:false};
    }else{drag={p,pan:true,x:view.x,y:view.y,moved:false};selected=null;edgeSelected=-1;}
    content.querySelector('svg').setPointerCapture(event.pointerId);draw();
  }
  function pointerMove(event){if(!drag)return;const p=point(event);if(drag.pan){view.x-=p.x-drag.p.x;view.y-=p.y-drag.p.y;draw();return;}
    const n=state.document.nodes.find(n=>n.id===drag.id);n.x=Math.min(20000,Math.max(16,drag.x+p.x-drag.p.x));n.y=Math.min(20000,Math.max(n.parent_id?55:16,drag.y+p.y-drag.p.y));
    drag.moved ||= Math.abs(n.x-drag.x)+Math.abs(n.y-drag.y)>1;draw();
  }
  function pointerUp(){if(drag&&!drag.pan&&!same(drag.before,state.document)){pushHistory(state.undo,drag.before);state.redo=[];changed();}drag=null;renderNodeEditor();}
  function renderNodeEditor(){const el=content.querySelector('.brief-node-editor');if(!el)return;const n=state.document.nodes.find(n=>n.id===selected);
    if(!n){el.hidden=true;return;}el.hidden=false;
    const excluded=new Set([n.id,...descendants(n.id).map(n=>n.id)]);
    el.innerHTML=`<div class="brief-fields"><label>节点名称<input data-node-field="title" maxlength="160" value="${escape(n.title)}"></label>
      <label>所属框（改变嵌套）<select data-node-field="parent_id"><option value="">顶层</option>${state.document.nodes.filter(p=>!excluded.has(p.id)).map(p=>`<option value="${p.id}" ${n.parent_id===p.id?'selected':''}>${escape(p.title)}</option>`).join('')}</select></label>
      <label>确认状态<select data-node-field="status">${Object.entries(statuses).map(([k,v])=>`<option value="${k}" ${n.status===k?'selected':''}>${v}</option>`).join('')}</select></label>
      <label>优先级<select data-node-field="priority">${['P0','P1','P2'].map(v=>`<option ${n.priority===v?'selected':''}>${v}</option>`).join('')}</select></label>
      <label>类型<select data-node-field="kind">${Object.entries({module:'模块',feature:'功能',discussion:'待讨论项'}).map(([k,v])=>`<option value="${k}" ${n.kind===k?'selected':''}>${v}</option>`).join('')}</select></label>
      <label>负责人<input data-node-field="owner" maxlength="160" value="${escape(n.owner)}"></label></div>
      <label>说明<textarea data-node-field="description" maxlength="6000">${escape(n.description)}</textarea></label>
      <label>验收标准<textarea data-node-field="acceptance" maxlength="3000">${escape(n.acceptance)}</textarea></label>`;
  }
  function fit(){const all=[...boxes.values()];if(!all.length)return;const maxX=Math.max(...all.map(b=>b.x+b.w)),maxY=Math.max(...all.map(b=>b.y+b.h));view={x:0,y:0,w:Math.max(400,maxX+40),h:Math.max(300,maxY+40)};draw();}
  function autoLayout(){
    const arrange=parent=>{let y=parent?60:24;const siblings=state.document.nodes.filter(n=>n.parent_id===parent);
      // Stable topological order among siblings; containment has its own layout.
      const ids=new Set(siblings.map(n=>n.id)),pending=new Set(ids),ordered=[];
      while(pending.size){const ready=siblings.filter(n=>pending.has(n.id)&&!state.document.edges.some(e=>e.target===n.id&&pending.has(e.source)));
        for(const n of ready){ordered.push(n);pending.delete(n.id);}if(!ready.length)break;}
      for(const n of ordered){n.collapsed=false;arrange(n.id);n.x=parent?24:40;n.y=y;y+=sizes().get(n.id).h+50;}
    };arrange(null);
  }
  panel.addEventListener('change',event=>{if(!state)return;const el=event.target;
    if(el.dataset.reviewIndex!==undefined){
      const key=state.proposal.changes[Number(el.dataset.reviewIndex)].id;
      const selection=new Set(state.reviewSelection);if(el.checked)selection.add(key);else selection.delete(key);
      state.reviewSelection=[...selection];remember();const index=el.dataset.reviewIndex;renderReview();
      content.querySelector(`[data-review-index="${index}"]`)?.focus({preventScroll:true});return;
    }
    if(el.dataset.nodeField){const key=el.dataset.nodeField;mutate(()=>{const n=state.document.nodes.find(n=>n.id===selected);n[key]=key==='parent_id'?(el.value||null):el.value;if(!n.title.trim())throw new Error('节点名称不能为空。');if(key==='parent_id'){n.x=24;n.y=n.parent_id?60:24;}});}
  });
  panel.addEventListener('input',event=>{if(!state)return;const el=event.target;
    if(el.dataset.field==='title'||el.dataset.section){
      const current=el.dataset.field?state.document.title:state.document.sections[el.dataset.section];
      if(current===el.value)return;
      checkpoint(el);if(el.dataset.field)state.document.title=el.value;else state.document.sections[el.dataset.section]=el.value;changed();
    }
  });
  panel.addEventListener('focusout',()=>{editGroup=null;});
  panel.addEventListener('keydown',event=>{
    if(!state||busy||event.isComposing||event.altKey||!(event.ctrlKey||event.metaKey))return;
    const key=event.key.toLowerCase();
    const action=key==='z'?(event.shiftKey?'redo':'undo'):key==='y'&&!event.shiftKey?'redo':null;
    if(!action)return;
    // Node fields commit on change; leave their native text history intact until then.
    if(event.target.matches('[data-node-field]'))return;
    event.preventDefault();
    travel(action);
  });
  panel.addEventListener('click',async event=>{
    const target=event.target.closest('button');if(!target||busy)return;
    editGroup=null;
    if(target.dataset.tab){focusReview=false;tab=target.dataset.tab;render();if(state)try{if(tab==='history')await loadHistory();if(tab==='handoffs')await loadHandoffs();}catch(error){note(error.message);}return;}
    const action=target.dataset.action;if(!action||!state)return;
    if(action==='generate'&&state.proposal){tab='review';render();note('请先采纳或忽略现有建议，再重新整理。');return;}
    try{
      if(['save','generate','generation-export','export','reload','review-apply','review-discard','history-refresh','history-more','history-view','history-restore','dispatch','handoffs-refresh','handoff-view','handoff-task'].includes(action)){
        busy=true;panel.setAttribute('aria-busy','true');panel.inert=true;
        if(action==='save')await save();
        if(action==='generate')await startGeneration();
        if(action==='generation-export'){const job=generationJobs.get(cid);download(await(await request(`/generation/${encodeURIComponent(job.id)}/markdown`)).text());}
        if(action==='review-apply')await reviewProposal(false);
        if(action==='review-discard'&&confirm('忽略这份 AI 建议？已保存文档和本地编辑保持不变。'))await reviewProposal(true);
        if(action==='history-refresh'||action==='history-more')await loadHistory(action==='history-more');
        if(action==='history-view')await inspectHistory(content.querySelector('[data-history-choice]').value);
        if(action==='history-restore')await restoreHistory();
        if(action==='dispatch')await dispatchBrief();
        if(action==='handoffs-refresh')await loadHandoffs();
        if(action==='handoff-view'){
          const current=state,id=cid,token=requestId;const data=await(await request(`/handoffs/${target.dataset.revision}`)).json();
          current.handoffDetail=data;if(id===cid&&token===requestId&&state===current)renderHandoffs();
        }
        if(action==='handoff-task'){
          const id=cid,token=requestId;
          const response=await apiFetch(`/api/tasks/${encodeURIComponent(target.dataset.task)}`);
          if(!response.ok)throw new Error('任务记录不可用，请核实任务是否尚未创建或已被删除。');
          const task=await response.json();if(id!==cid||token!==requestId)return;
          summary();
          const result=task.output_data||{},delivery=result._delivery;
          const acceptance=!delivery?'尚未交付':delivery.accepted===true?'已验收':delivery.accepted===false?'已退回':'待验收';
          content.innerHTML=`<button class="btn" data-action="handoffs-refresh">返回下发记录</button>
            <h3>${escape(task.name)}</h3><p>任务 ${escape(task.task_id)} · ${escape(taskStatus(task.status))}</p>
            <p>关联 PM：${escape(task.pm_agent_id||'尚未分配')}</p><p>需求依据：v${escape(task.input_data?._brief?.revision||'未知')}</p>
            <p>验收状态：${acceptance}</p>
            ${delivery?.reject_reason?`<p>退回原因：${escape(delivery.reject_reason)}</p>`:''}
            ${delivery?.summary?`<p>交付摘要：${escape(delivery.summary)}</p>`:''}
            <h4>交付内容／执行结果</h4><pre>${escape(delivery?.deliverable||result.summary||result.error||'尚无交付内容，请到任务面板查看进度和验收操作。')}</pre>`;
        }
        if(action==='export'){if(state.dirty)download(markdown());else download(await(await request('/markdown')).text());}
        if(action==='reload'){if(!state.dirty||confirm('重新加载将替换本地未保存修改。建议先导出草稿，是否继续？')){drafts.delete(cid);state=null;await open(cid);}}
      }else if(action==='review-all'||action==='review-none'){
        state.reviewSelection=action==='review-all'?state.proposal.changes.map(change=>change.id):[];remember();renderReview();
      }else if(action==='add'||action==='child'){
        if(action==='child'&&!selected)throw new Error('请先选择一个父框。');
        const parent=action==='child'?selected:null;
        mutate(()=>{if(parent)state.document.nodes.find(n=>n.id===parent).collapsed=false;
          const id='node_'+Date.now().toString(36)+'_'+Math.random().toString(36).slice(2,7);const peers=state.document.nodes.filter(n=>n.parent_id===parent);
          state.document.nodes.push({id,title:parent?'新功能':'新模块',kind:parent?'feature':'module',parent_id:parent,description:'',status:'discussion',priority:'P1',acceptance:'',owner:'',x:24,y:60+peers.length*110,collapsed:false});selected=id;});
      }else if(action==='connect'){connecting=!connecting;connectSource=null;render();if(connecting)note('依次点击前置节点与后续节点。');}
      else if(action==='delete'){
        if(edgeSelected>=0){mutate(()=>state.document.edges.splice(edgeSelected,1));edgeSelected=-1;draw();}
        else if(selected){const removed=new Set([selected,...descendants(selected).map(n=>n.id)]);if(removed.size>1&&!confirm(`将删除该框及 ${removed.size-1} 个子节点，可撤销。继续？`))return;
          mutate(()=>{state.document.nodes=state.document.nodes.filter(n=>!removed.has(n.id));state.document.edges=state.document.edges.filter(e=>!removed.has(e.source)&&!removed.has(e.target));selected=null;});}
      }else if(action==='undo'||action==='redo')travel(action);
      else if(action==='layout'){mutate(autoLayout);fit();}else if(action==='fit')fit();
    }catch(error){note(error.message);showToast(error.message,'err');}
    finally{busy=false;panel.setAttribute('aria-busy','false');panel.inert=false;if(cid)pollLater(cid,requestId);}
  });
  window.addEventListener('beforeunload',event=>{if([...drafts.values()].some(s=>s.dirty)){event.preventDefault();event.returnValue='';}});
  render();
  return {open,refresh,flush};
})();
