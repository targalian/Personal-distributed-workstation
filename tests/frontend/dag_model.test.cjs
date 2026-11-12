/** Behavioral invariants for editing a branching dependency graph without losing task data. */
const test=require('node:test'),assert=require('node:assert/strict');
const M=require('../../lan_mesh/web/static/dag/model.js');
const graph=()=>({nodes:['需求','开发','测试','发布'].map((name,i)=>({id:String(i),name,status:'pending',input_data:{context:'保留'},output_data:{files:['result.txt']},assigned_agent_id:'worker-a'})),edges:[{source:'0',target:'1'},{source:'0',target:'2'},{source:'1',target:'3'},{source:'2',target:'3'}]});
test('branch and merge layout follows dependencies, separates parallel nodes',()=>{const g=graph(),p=M.layout(g);for(const e of g.edges)assert.ok(p.get(e.source).x<p.get(e.target).x);assert.notDeepEqual(p.get('1'),p.get('2'));assert.equal(M.validate(g),'');});
test('cycles, self links, missing endpoints and duplicate links cannot enter a draft',()=>{const g=graph();for(const [a,b] of [['3','0'],['0','0'],['0','missing'],['0','1']])assert.throws(()=>M.connect(g,a,b));assert.equal(g.edges.length,4);});
test('valid new dependency returns an independent draft with execution fields intact',()=>{const original=graph(),next=M.connect(original,'1','2');assert.equal(next.edges.length,5);assert.equal(original.edges.length,4);next.nodes[0].name='调整后的需求';assert.equal(original.nodes[0].name,'需求');assert.deepEqual(next.nodes[0].output_data,{files:['result.txt']});assert.equal(next.nodes[0].assigned_agent_id,'worker-a');});
test('dirty tracking ignores view positions and order but detects content and condition changes',()=>{const a=graph(),b=M.copy(a);b.nodes.reverse();b.nodes[0].x=999;b.edges.reverse();assert.equal(M.signature(a),M.signature(b));b.nodes[0].description='新的验收要求';assert.notEqual(M.signature(a),M.signature(b));const c=M.copy(a);c.edges[0].condition='passed';assert.notEqual(M.signature(a),M.signature(c));});
test('empty graphs are valid, ambiguous or nameless nodes are not',()=>{assert.equal(M.validate(M.normalize()),'');const g=graph();g.nodes[0].name=' ';assert.ok(M.validate(g));g.nodes[0].name='需求';g.nodes[1].id='0';assert.ok(M.validate(g));});
test('secretary refresh broadcasts the requested task identity to both workbenches',()=>{
  const vm=require('node:vm'),fs=require('node:fs'),path=require('node:path'),messages=[];
  const context={location:{origin:'http://localhost'},window:{addEventListener(){}},document:{getElementById:id=>({contentWindow:{postMessage:(data,origin)=>messages.push({id,data,origin})}})}};
  vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../../lan_mesh/web/static/dashboard/dag.js'),'utf8'),context);
  context.chatDagRefresh('task-abcdef123456');
  assert.deepEqual(JSON.parse(JSON.stringify(messages)),['chat-dag-frame','workspace-dag-frame'].map(id=>({id,data:{type:'dag-refresh',task_id:'task-abcdef123456'},origin:'http://localhost'})));
});
