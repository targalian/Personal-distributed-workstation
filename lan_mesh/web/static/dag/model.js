/* Shared graph rules, independent from the DOM and HTTP transport. */
(function (root) {
  'use strict';
  const copy = value => JSON.parse(JSON.stringify(value));
  const labels = {pending:'待开始',assigned:'已分配',running:'进行中',completed:'已完成',failed:'失败',cancelled:'已取消',paused:'已暂停',skipped:'已跳过',timeout:'超时',awaiting_input:'等待回复'};
  function normalize(value) {
    const graph = copy(value || {});
    graph.nodes = Array.isArray(graph.nodes) ? graph.nodes : [];
    graph.edges = Array.isArray(graph.edges) ? graph.edges : [];
    return graph;
  }
  function validate(graph) {
    const ids = new Set(graph.nodes.map(n => n.id));
    if (ids.size !== graph.nodes.length || graph.nodes.some(n => !n.id || !String(n.name || '').trim())) return '每个步骤都需要名称和独立标识。';
    const seen = new Set(), degrees = new Map([...ids].map(id => [id,0]));
    const next = new Map([...ids].map(id => [id,[]]));
    for (const e of graph.edges) {
      if (!ids.has(e.source) || !ids.has(e.target)) return '依赖连接了不存在的步骤，请先移除该连线。';
      if (e.source === e.target) return '步骤不能依赖自己。';
      const key = JSON.stringify([e.source,e.target]);
      if (seen.has(key)) return '这两个步骤之间已经有依赖。';
      seen.add(key); degrees.set(e.target,degrees.get(e.target)+1); next.get(e.source).push(e.target);
    }
    const queue = [...ids].filter(id => degrees.get(id) === 0);
    let visited = 0;
    for (let i=0;i<queue.length;i++) {
      visited++;
      for (const id of next.get(queue[i])) { degrees.set(id,degrees.get(id)-1); if (!degrees.get(id)) queue.push(id); }
    }
    return visited === ids.size ? '' : '这条依赖会形成循环，请调整先后顺序。';
  }
  function layout(graph, vertical=false) {
    const degrees = new Map(graph.nodes.map(n => [n.id,0])), next = new Map(graph.nodes.map(n => [n.id,[]]));
    const levels = new Map(graph.nodes.map(n => [n.id,0]));
    for (const e of graph.edges) if (next.has(e.source) && degrees.has(e.target)) {
      next.get(e.source).push(e.target); degrees.set(e.target,degrees.get(e.target)+1);
    }
    const queue = graph.nodes.filter(n => !degrees.get(n.id)).map(n => n.id);
    for (let i=0;i<queue.length;i++) for (const id of next.get(queue[i])) {
      levels.set(id,Math.max(levels.get(id),levels.get(queue[i])+1));
      degrees.set(id,degrees.get(id)-1); if (!degrees.get(id)) queue.push(id);
    }
    const rows = new Map(), positions = new Map();
    for (const n of graph.nodes) {
      const level = levels.get(n.id), row = rows.get(level) || 0;
      positions.set(n.id,vertical?{x:48+row*252,y:48+level*142}:{x:48+level*292,y:48+row*132}); rows.set(level,row+1);
    }
    return positions;
  }
  function canonical(value) {
    if (Array.isArray(value)) return value.map(canonical);
    if (value && typeof value === 'object') return Object.fromEntries(Object.keys(value).sort().map(k => [k,canonical(value[k])]));
    return value;
  }
  function signature(graph) {
    // Positions are presentation only: the existing server regenerates them.
    return JSON.stringify(canonical({nodes:graph.nodes.map(({x,y,...n}) => n).sort((a,b)=>a.id.localeCompare(b.id)),edges:[...graph.edges].sort((a,b)=>JSON.stringify([a.source,a.target]).localeCompare(JSON.stringify([b.source,b.target])))}));
  }
  function connect(graph, source, target) {
    const candidate = copy(graph); candidate.edges.push({source,target,condition:'',description:''});
    const error = validate(candidate); if (error) throw Error(error);
    return candidate;
  }
  const api = {copy,labels,normalize,validate,layout,signature,connect};
  root.DagModel = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(globalThis);
