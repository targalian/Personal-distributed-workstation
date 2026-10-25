/* Invoke every refactored command through a stub and compare the legacy IPC contract. */
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const ts=require('../../webui/node_modules/typescript');
const fixture=require('../fixtures/tauri_commands.json');

test('all Tauri command names and argument payloads remain compatible',async()=>{
  const actual={};const calls=[];
  for(const group of ['devices','transfers','settings','shares']){
    const source=fs.readFileSync(path.join(__dirname,'../../quicklan-main/src/api',group+'.ts'),'utf8');
    const code=ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS}}).outputText;
    const exports={};
    new Function('exports','require',code)(exports,()=>({invoke:(command,args)=>{calls.push({command,args:args||null});return Promise.resolve();}}));
    Object.assign(actual,exports);
  }
  assert.deepEqual(Object.keys(actual).sort(),fixture.map(item=>item.name).sort());
  for(const item of fixture){
    await actual[item.name](...item.args);
    assert.deepEqual(calls.pop(),item.call,item.name);
  }
});
