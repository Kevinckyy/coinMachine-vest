// Real V8/DOM collection in isolated Edge; mocked Chrome and local fixture only.
// No authenticated website, user service, account or trade is accessed.
import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {createServer} from 'node:http';
import {existsSync} from 'node:fs';
import {mkdir,readFile,writeFile} from 'node:fs/promises';
import {fileURLToPath} from 'node:url';
import path from 'node:path';
import {randomUUID} from 'node:crypto';

const root=fileURLToPath(new URL('../',import.meta.url));
const edge=process.env.EDGE_PATH||['C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe','C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe'].find(existsSync);
assert.ok(edge,'Edge must be installed');
const run=path.join(root,'.runtime','extension-memory-'+randomUUID());await mkdir(run,{recursive:true});
const fixture=await readFile(path.join(root,'tests','extension-memory-fixture.html'));
const api=createServer((request,response)=>{response.writeHead(200,{'Content-Type':'text/html; charset=utf-8'});response.end(fixture);});
await new Promise(resolve=>api.listen(0,'127.0.0.1',resolve));
const sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms));
async function until(fn,label){for(let i=0;i<100;i++){try{const value=await fn();if(value)return value;}catch{}await sleep(100);}throw new Error('Timeout: '+label);}
let browser,socket;const pending=new Map();let seq=0;
const command=(method,params={})=>new Promise((resolve,reject)=>{
  const id=++seq,timer=setTimeout(()=>{pending.delete(id);reject(new Error('CDP timeout: '+method));},90000);
  pending.set(id,{resolve,reject,timer});socket.send(JSON.stringify({id,method,params}));
});
const evaluate=async expression=>{
  const result=await command('Runtime.evaluate',{expression,awaitPromise:true,returnByValue:true});
  if(result.exceptionDetails)throw new Error(result.exceptionDetails.exception?.description||result.exceptionDetails.text);
  return result.result.value;
};
const samples=[];
async function sample(label){
  // Separate protocol tasks allow weak references to leave the current JS job.
  await command('HeapProfiler.collectGarbage');await evaluate('Promise.resolve()');
  await command('HeapProfiler.collectGarbage');
  const heap=await command('Runtime.getHeapUsage'),dom=await command('Memory.getDOMCounters');
  const stats=await evaluate('memoryFixture.stats()');
  const result={label,heap,dom,stats};samples.push(result);return result;
}
try{
  browser=spawn(edge,['--headless=new','--disable-gpu','--no-first-run','--disable-extensions',
    '--disable-background-networking','--remote-debugging-port=0','--user-data-dir='+path.join(run,'edge'),'about:blank'],{windowsHide:true,stdio:'ignore'});
  const port=await until(async()=>Number((await readFile(path.join(run,'edge','DevToolsActivePort'),'utf8')).split('\n')[0]),'debugging port');
  const page=await until(async()=>(await(await fetch(`http://127.0.0.1:${port}/json/list`)).json()).find(x=>x.type==='page'),'isolated page');
  socket=new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((resolve,reject)=>{socket.onopen=resolve;socket.onerror=reject;});
  socket.onmessage=event=>{
    const data=JSON.parse(event.data),entry=pending.get(data.id);if(!entry)return;
    pending.delete(data.id);clearTimeout(entry.timer);data.error?entry.reject(new Error(JSON.stringify(data.error))):entry.resolve(data.result);
  };
  await command('Page.navigate',{url:`http://127.0.0.1:${api.address().port}/trade/NQ-PERP`});
  await until(()=>evaluate("document.readyState==='complete'&&!!globalThis.memoryFixture"),'memory fixture');
  const core=await readFile(path.join(root,'extension','core.js'),'utf8');
  const content=await readFile(process.env.VEST_MEMORY_CONTENT_PATH||path.join(root,'extension','content.js'),'utf8');
  const inject=async()=>{await evaluate(core);await evaluate(content);await evaluate('memoryFixture.ready()');};
  await inject();await sample('baseline');
  await evaluate('memoryFixture.dismiss(1000000)');
  await sample('after-dismissal-and-account-change');
  const retainsDismissedDialog=await evaluate('!!memoryDialog.deref()');
  await evaluate('memoryFixture.runTicks(500)');await sample('warm-idle-observation');
  await evaluate('memoryFixture.runTicks(5000)');await sample('5000-idle-ticks');
  await evaluate('memoryFixture.runTicks(1000,true)');await sample('1000-monitoring-ticks');
  await evaluate('memoryFixture.closures(100)');await sample('100-closures');
  for(let i=0;i<50;i++)await inject();
  const reinjected=await sample('50-reinjections');
  await evaluate('globalThis.__vestControlBridgeController.dispose()');
  const disposed=await sample('disposed');
  const report={retainsDismissedDialog,samples};await writeFile(path.join(run,'memory.json'),JSON.stringify(report,null,2));
  console.log(JSON.stringify({report:path.join(run,'memory.json'),...report},null,2));
  assert.equal(retainsDismissedDialog,false,'plugin must not retain a detached closure dialog after account/mode changes');
  assert.equal(reinjected.stats.listeners,1);assert.equal(reinjected.stats.storageListeners,1);assert.equal(reinjected.stats.timers,1);
  assert.equal(disposed.stats.listeners,0);assert.equal(disposed.stats.storageListeners,0);assert.equal(disposed.stats.timers,0);
  assert.ok(samples[3].stats.observations>2000,'idle ticks must actually publish observations');
  assert.ok(samples[4].stats.polls>=1000,'monitoring ticks must actually poll the selected account');
  const warm=samples[2],steady=samples[3];
  assert.ok(steady.heap.usedSize-warm.heap.usedSize<2*1024*1024,'steady observation retained more than 2 MB');
  assert.equal(steady.dom.nodes,warm.dom.nodes,'steady observation leaked DOM nodes');
  assert.equal(steady.dom.jsEventListeners,warm.dom.jsEventListeners,'steady observation leaked DOM event listeners');
}finally{
  if(socket){try{await command('Browser.close');}catch{}socket.close();}
  for(const entry of pending.values()){clearTimeout(entry.timer);entry.reject(new Error('Test closed'));}pending.clear();
  browser?.kill();await new Promise(resolve=>api.close(resolve));
}
