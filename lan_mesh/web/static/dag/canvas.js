/* SVG interaction surface. All task labels enter the DOM as text, never markup. */
(function (root) {
  'use strict';
  const NS='http://www.w3.org/2000/svg', W=216, H=86;
  function svg(tag, attrs={}, text) {
    const el=document.createElementNS(NS,tag);
    for (const [k,v] of Object.entries(attrs)) el.setAttribute(k,v);
    if (text!==undefined) el.textContent=text;
    return el;
  }
  const color=status=>({completed:'#6edbb0',running:'#83b6ff',assigned:'#83b6ff',failed:'#ff9caa',timeout:'#ff9caa',paused:'#f0cd83'}[status]||'#a7b6ca');
  class DagCanvas {
    constructor(element,onSelect) {
      this.el=element; this.onSelect=onSelect; this.positions=new Map(); this.graph={nodes:[],edges:[]}; this.selected=null; this.query=''; this.vertical=false;
      this.view={x:0,y:0,scale:1}; this.drag=null;
      this.resize=new ResizeObserver(()=>this.transform()); this.resize.observe(element);
      element.addEventListener('wheel',e=>{e.preventDefault();this.zoom(e.deltaY<0?1.12:1/1.12,e.clientX,e.clientY);},{passive:false});
      element.addEventListener('pointerdown',e=>this.start(e));
      element.addEventListener('pointermove',e=>this.move(e));
      element.addEventListener('pointerup',e=>this.end(e));
      element.addEventListener('pointercancel',()=>{this.drag=null;});
      element.addEventListener('keydown',e=>this.key(e));
    }
    dimensions(){return {w:this.el.clientWidth||600,h:this.el.clientHeight||440};}
    transform(){const {w,h}=this.dimensions();this.el.setAttribute('viewBox',`${this.view.x} ${this.view.y} ${w/this.view.scale} ${h/this.view.scale}`);document.getElementById('zoom-level').textContent=Math.round(this.view.scale*100)+'%';}
    point(x,y){const box=this.el.getBoundingClientRect();return {x:this.view.x+(x-box.left)/this.view.scale,y:this.view.y+(y-box.top)/this.view.scale};}
    zoom(factor,x,y){const box=this.el.getBoundingClientRect();x??=box.left+box.width/2;y??=box.top+box.height/2;const p=this.point(x,y);this.view.scale=Math.max(.2,Math.min(2.5,this.view.scale*factor));this.view.x=p.x-(x-box.left)/this.view.scale;this.view.y=p.y-(y-box.top)/this.view.scale;this.transform();}
    fit(){if(!this.graph.nodes.length)return;const ps=this.graph.nodes.map(n=>this.positions.get(n.id));const x=Math.min(...ps.map(p=>p.x)),y=Math.min(...ps.map(p=>p.y)),width=Math.max(...ps.map(p=>p.x))+W-x,height=Math.max(...ps.map(p=>p.y))+H-y;const {w,h}=this.dimensions();this.view.scale=Math.max(.2,Math.min(1.1,(w-64)/width,(h-80)/height));this.view.x=x-(w/this.view.scale-width)/2;this.view.y=y-(h/this.view.scale-height)/2;this.transform();}
    focus(id){const p=this.positions.get(id);if(!p)return;const {w,h}=this.dimensions();this.view.scale=Math.max(.8,this.view.scale);this.view.x=p.x+W/2-w/this.view.scale/2;this.view.y=p.y+H/2-h/this.view.scale/2;this.transform();}
    reset(graph){this.vertical=this.dimensions().w<800;this.positions=DagModel.layout(graph,this.vertical);this.set(graph,null,'');this.fit();if(this.view.scale<.65){this.view={x:0,y:0,scale:.8};this.transform();}}
    arrange(){this.vertical=this.dimensions().w<800;this.positions=DagModel.layout(this.graph,this.vertical);this.render();this.fit();}
    set(graph,selected,query){this.graph=graph;this.selected=selected;this.query=query.toLowerCase();const defaults=DagModel.layout(graph,this.vertical);for(const n of graph.nodes)if(!this.positions.has(n.id))this.positions.set(n.id,defaults.get(n.id));this.render();}
    render(){
      const focused=document.activeElement?.dataset;const focusNode=focused?.nodeId,focusEdge=focused?.edgeIndex;
      const edges=document.getElementById('edges'), nodes=document.getElementById('nodes');edges.replaceChildren();nodes.replaceChildren();
      const related=new Set();const id=this.selected?.type==='node'?this.selected.id:null;
      for(const e of this.graph.edges)if(e.source===id||e.target===id){related.add(e.source);related.add(e.target);}
      this.graph.edges.forEach((edge,i)=>this.edge(edges,edge,i));
      for(const n of this.graph.nodes)this.node(nodes,n,related);
      const focus=[...nodes.children,...edges.children].find(el=>(focusNode&&el.dataset.nodeId===focusNode)||(focusEdge!==undefined&&el.dataset.edgeIndex===focusEdge));
      focus?.focus({preventScroll:true});this.transform();
    }
    edge(container,e,index){
      const a=this.positions.get(e.source),b=this.positions.get(e.target);if(!a||!b)return;
      const x=a.x+W,y=a.y+H/2,tx=b.x,ty=b.y+H/2,delta=Math.max(55,Math.abs(tx-x)*.5);
      const vx=a.x+W/2,vy=a.y+H,vtx=b.x+W/2,vty=b.y,vd=Math.max(30,Math.abs(vty-vy)*.5);
      const d=this.vertical?`M${vx},${vy} C${vx},${vy+vd} ${vtx},${vty-vd} ${vtx},${vty}`:`M${x},${y} C${x+delta},${y} ${tx-delta},${ty} ${tx},${ty}`;
      const active=this.selected?.type==='edge'&&this.selected.index===index;
      const g=svg('g',{class:'edge-group',tabindex:0,role:'button','aria-label':`依赖：${this.graph.nodes.find(n=>n.id===e.source)?.name} → ${this.graph.nodes.find(n=>n.id===e.target)?.name}`});
      g.dataset.edgeIndex=index;g.append(svg('path',{d,class:'edge'+(e.condition?' condition':'')+(active?' selected':''),'marker-end':'url(#arrow)'}),svg('path',{d,class:'edge-hit'}));
      if(e.condition)g.append(svg('text',{x:this.vertical?(vx+vtx)/2+10:(x+tx)/2,y:this.vertical?(vy+vty)/2:(y+ty)/2-10,'text-anchor':'middle',class:'edge-label'},String(e.condition).slice(0,22)));
      g.append(svg('title',{},e.condition||'前置步骤完成后继续'));container.append(g);
    }
    node(container,n,related){
      const p=this.positions.get(n.id),selected=this.selected?.type==='node'&&this.selected.id===n.id;
      const match=this.query&&`${n.name} ${n.skill} ${n.description}`.toLowerCase().includes(this.query);
      const cls='graph-node'+(selected?' selected':'')+(related.has(n.id)?' related':'')+(match?' match':'')+(this.query&&!match?' muted':'');
      const status=DagModel.labels[n.status]||n.status||'待开始';
      const g=svg('g',{transform:`translate(${p.x},${p.y})`,class:cls,tabindex:0,role:'button','aria-label':`${n.name}，${status}`,'aria-pressed':String(selected)});g.dataset.nodeId=n.id;
      g.append(svg('rect',{class:'card',width:W,height:H,rx:12}),svg('rect',{x:0,y:16,width:3,height:H-32,rx:2,fill:color(n.status)}));
      const name=Array.from(n.name||'未命名步骤');const first=name.slice(0,12).join(''),second=name.slice(12,24).join('')+(name.length>24?'…':'');
      g.append(svg('text',{x:16,y:25,class:'node-title'},first));if(second)g.append(svg('text',{x:16,y:43,class:'node-title'},second));
      g.append(svg('text',{x:16,y:68,class:'node-meta'},String(n.skill||'未指定技能').slice(0,19)),svg('text',{x:W-12,y:68,'text-anchor':'end',class:'node-status',fill:color(n.status)},status),svg('title',{},`${n.name}\n${n.description||''}`));
      container.append(g);
    }
    start(e){
      if(e.button!==0||this.drag)return;const target=e.target.closest('[data-node-id],[data-edge-index]');const point=this.point(e.clientX,e.clientY);
      this.drag={pointer:e.pointerId,startX:e.clientX,startY:e.clientY,point,view:{...this.view},target:target?.dataset.nodeId?{type:'node',id:target.dataset.nodeId}:target?.dataset.edgeIndex!==undefined?{type:'edge',index:Number(target.dataset.edgeIndex)}:null,moved:false};
      if(this.drag.target?.type==='node')this.drag.pos={...this.positions.get(this.drag.target.id)};
      this.el.setPointerCapture(e.pointerId);e.preventDefault();
    }
    move(e){
      const d=this.drag;if(!d||d.pointer!==e.pointerId)return;
      const dx=(e.clientX-d.startX)/this.view.scale,dy=(e.clientY-d.startY)/this.view.scale;
      if(Math.hypot(e.clientX-d.startX,e.clientY-d.startY)<4&&!d.moved)return;d.moved=true;
      if(d.target?.type==='node'){this.positions.set(d.target.id,{x:d.pos.x+dx,y:d.pos.y+dy});this.render();}
      else{this.view.x=d.view.x-dx;this.view.y=d.view.y-dy;this.transform();}
    }
    end(e){const d=this.drag;if(!d||d.pointer!==e.pointerId)return;this.drag=null;if(this.el.hasPointerCapture(e.pointerId))this.el.releasePointerCapture(e.pointerId);if(!d.moved)this.onSelect(d.target);}
    key(e){
      const target=e.target.closest('[data-node-id],[data-edge-index]');
      if(target&&(e.key==='Enter'||e.key===' ')){e.preventDefault();this.onSelect(target.dataset.nodeId?{type:'node',id:target.dataset.nodeId}:{type:'edge',index:Number(target.dataset.edgeIndex)});return;}
      const moves={ArrowLeft:[-50,0],ArrowRight:[50,0],ArrowUp:[0,-50],ArrowDown:[0,50]};
      if(!target&&moves[e.key]){e.preventDefault();this.view.x+=moves[e.key][0]/this.view.scale;this.view.y+=moves[e.key][1]/this.view.scale;this.transform();}
    }
  }
  root.DagCanvas=DagCanvas;
})(globalThis);
