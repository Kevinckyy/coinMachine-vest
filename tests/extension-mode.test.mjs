import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import { webcrypto } from 'node:crypto';

const source = fs.readFileSync(new URL('../extension/background.js', import.meta.url), 'utf8');
const EXTENSION_ID = 'abcdefghijklmnopabcdefghijklmnop';
const URL_VEST = 'https://next.vestmarkets.com/trade/NQ-PERP';
const PAIRING = 'fixture-mode-pairing-not-a-credential';
const clone = value => value === undefined ? undefined : structuredClone(value);
const observed = [
  { name: 'Alpha', category: 'eval', identity: 'eval:Alpha', equity: 987.5, balance: 1000, unrealized_pnl: -12.5, current: true, observed_at: Date.now() / 1000 },
  { name: 'Beta', category: 'funded', identity: 'funded:Beta', equity: 1275.5, balance: 1250, unrealized_pnl: 25.5, current: false, observed_at: Date.now() / 1000 },
];
const automatic = observed.map((row, index) => ({ ...row, id: `auto-${index + 1}`, auto_discovered: true, mode: 'browser' }));
const popupSender = { id: EXTENSION_ID, url: `chrome-extension://${EXTENSION_ID}/popup.html`, origin: `chrome-extension://${EXTENSION_ID}` };
const pageSender = { id: EXTENSION_ID, url: URL_VEST, origin: 'https://next.vestmarkets.com', tab: { id: 42, url: URL_VEST } };

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
const flush = () => new Promise(resolve => setImmediate(resolve));

function createHarness(options = {}) {
  const state = {
    local: { 'observation:42': { enabled: true, token: PAIRING, accounts: clone(observed) } },
    session: {}, requests: [], messages: [], backend: new Map(), timers: new Set(), windows: [], tabUrls: {}, removedTabs: [],
    ...options,
  };
  const events = () => ({ addListener: () => {}, removeListener: () => {} });
  const storage = name => ({
    get: async keys => {
      const data = state[name];
      if (keys == null) return clone(data);
      const names = typeof keys === 'string' ? [keys] : Array.isArray(keys) ? keys : Object.keys(keys);
      return Object.fromEntries(names.filter(key => key in data).map(key => [key, clone(data[key])]));
    },
    set: async values => { Object.assign(state[name], clone(values)); },
    remove: async keys => { for (const key of Array.isArray(keys) ? keys : [keys]) delete state[name][key]; },
    clear: async () => { state[name] = {}; },
  });
  const chrome = {
    runtime: { id: EXTENSION_ID, getManifest: () => ({ version: '0.3.0' }), getURL: file => `chrome-extension://${EXTENSION_ID}/${file}`,
      onMessage: { addListener: listener => { state.listener = listener; } }, onInstalled: events(), onStartup: events() },
    windows: { create: async info => { const id=100+state.windows.length; state.windows.push(clone(info)); state.tabUrls[id]=info.url; return {id:id+100,tabs:[{id}]}; } },
    tabs: { get: async id => ({ id, url: state.tabUrls[id] || URL_VEST, status: 'complete' }),
      update: async (id,info) => { state.tabUrls[id]=info.url; return {id,...info}; },
      remove: async id => { state.removedTabs.push(id); delete state.tabUrls[id]; },
      sendMessage: async (tabId, message) => {
        state.messages.push({ tabId, message: clone(message) });
        if (state.commandHandler) {
          const custom = await state.commandHandler(message, tabId);
          if (custom !== undefined) return custom;
        }
        if (message.type === 'bridge-ping') return { ok: true, protocol: 4, version: '0.3.0' };
        if (message.type === 'observe-start-scan') return { ok: true, started: true, scan_id: 'scan-fixture' };
        return { ok: true, mode: message.mode, mode_epoch: message.mode_epoch, session_id: message.session_id, accounts: clone(automatic), snapshot: { equity: 987.5, balance: 1000, unrealized_pnl: -12.5, price: 20100, position_open: false, observed_at: Date.now() / 1000 } };
      }, onRemoved: {addListener:listener=>{state.onRemoved=listener;}}, onUpdated: events() },
    scripting: { executeScript: async () => [{ frameId: 0 }] },
    storage: { local: storage('local'), session: storage('session'), onChanged: events() },
  };
  const response = body => ({ ok: true, status: 200, json: async () => clone(body) });
  const defaultFetch = async (url, init, body) => {
    if (url.endsWith('/api/bridge/accounts')) return response({ accounts: [] });
    if (url.endsWith('/api/bridge/session')) {
      const previous = state.backend.get(body.session_id) || { mode: 'readonly', mode_epoch: 0 };
      let next = previous;
      if (body.mode_epoch >= previous.mode_epoch) {
        if (body.mode === 'readonly') next = { mode: 'readonly', mode_epoch: body.mode_epoch };
        else if (body.mode === 'execute' && (body.authorize === true || previous.mode === 'execute')) next = { mode: 'execute', mode_epoch: body.mode_epoch };
      }
      state.backend.set(body.session_id, next);
      return response({ ok: true, session_id: body.session_id, ...next, accounts: clone(automatic), next_account: next.mode === 'execute' ? automatic[0] : null });
    }
    if (url.endsWith('/api/bridge/observe')) return response({ ok: true, accounts_received: body.accounts?.length || 0, accounts: clone(automatic) });
    if (url.endsWith('/api/bridge/poll')) return response({ job: null });
    if (url.endsWith('/api/bridge/result')) return response({ ok: true });
    throw new Error('Fixture forbids unexpected API ' + url);
  };
  const sandbox = {
    chrome, URL, AbortController, console, crypto: webcrypto,
    Date: class extends Date { static now() { return state.now ?? Date.now(); } },
    setTimeout: (callback, duration, ...args) => { const id = setTimeout(() => { state.timers.delete(id); callback(...args); }, duration); state.timers.add(id); return id; },
    clearTimeout: id => { clearTimeout(id); state.timers.delete(id); },
    fetch: async (url, init = {}) => {
      const body = init.body ? JSON.parse(init.body) : null;
      const request = { url: String(url), init, body: clone(body) };
      state.requests.push(request);
      if (state.fetchHandler) {
        const result = await state.fetchHandler(request, response);
        if (result !== undefined) return result;
      }
      return defaultFetch(String(url), init, body);
    },
  };
  sandbox.importScripts = file => { if(file !== 'core.js') throw new Error('Unexpected worker script'); vm.runInContext(fs.readFileSync(new URL('../extension/core.js', import.meta.url), 'utf8'),sandbox); };
  vm.createContext(sandbox);
  vm.runInContext(source, sandbox, { filename: 'extension/background.js' });
  const dispatch = (message, sender = popupSender) => new Promise((resolve, reject) => {
    const timeout = setTimeout(() => reject(new Error('Background mode request did not answer')), 4000);
    try { state.listener(message, sender, value => { clearTimeout(timeout); resolve(value); }); }
    catch (error) { clearTimeout(timeout); reject(error); }
  });
  return { state, sandbox, dispatch, response, dispose: () => { for (const timer of state.timers) clearTimeout(timer); } };
}

function harness(t, options) { const h = createHarness(options); t.after(h.dispose); return h; }
const mode = (h, value) => h.dispatch({ type: 'control-mode', tabId: 42, token: PAIRING, mode: value });
const modeMessages = h => h.state.messages.filter(item => item.message.type === 'control-mode').map(item => item.message);

test('the single execute toggle automatically uses discovered accounts without a manual trading record', async t => {
  const h = harness(t);
  const result = await mode(h, 'execute');
  assert.equal(result.ok, true, result.error);
  assert.equal(result.data.mode, 'execute');
  assert.equal(result.data.accounts.length, 2);
  assert.ok(h.state.requests.some(request => request.url.endsWith('/api/bridge/session') && request.body.mode === 'execute' && request.body.authorize === true));
  assert.equal(h.state.requests.some(request => /\/api\/accounts(?:$|\/)/.test(request.url)), false, 'mode switching must never require manual account creation');
  assert.ok(modeMessages(h).some(message => message.mode === 'execute' && message.accounts?.length === 2));
});

test('Live activation is reserved once and only for the current authorized second-stage account',async t=>{
  const h=harness(t);await mode(h,'execute');
  const control=(await h.dispatch({type:'control-status',tabId:42})).data;
  h.state.local['observation:42'].registrations[0].phase='2/2';
  const message={type:'live-activation-reserve',account_id:automatic[0].id,name:automatic[0].name,mode_epoch:control.mode_epoch};
  assert.equal((await h.dispatch({...message,mode_epoch:control.mode_epoch-1},pageSender)).ok,false);
  assert.equal((await h.dispatch({...message,name:'Other'},pageSender)).ok,false);
  const first=await h.dispatch(message,pageSender);assert.equal(first.ok,true,first.error);
  assert.equal(first.data.status,'pending');
  const second=await h.dispatch(message,pageSender);assert.equal(second.data.duplicate,true);
  assert.equal(second.data.started_at,first.data.started_at);
  h.state.local['observation:42'].registrations[1].phase='2/2';
  const other={...message,account_id:automatic[1].id,name:automatic[1].name};
  const pair=await Promise.all([h.dispatch(other,pageSender),h.dispatch(other,pageSender)]);
  assert.equal(pair.filter(r=>r.ok&&!r.data.duplicate).length,1,'concurrent reservations must only reserve once');
  assert.equal(pair.filter(r=>r.ok&&r.data.duplicate).length,1);
  const requestCount=h.state.requests.length;
  h.state.tabUrls[42]='https://next.vestmarkets.com/portfolio';
  const portfolio={...pageSender,url:'https://next.vestmarkets.com/portfolio',tab:{id:42,url:'https://next.vestmarkets.com/portfolio'}};
  const heartbeat=await h.dispatch({type:'mode-heartbeat'},portfolio);
  assert.equal(heartbeat.data.mode,'execute');assert.equal(heartbeat.data.phase_activation_pending,true);
  assert.equal(h.state.requests.length,requestCount,'Portfolio must not send a fictitious NQ heartbeat');
  assert.equal((await h.dispatch({type:'execution-next'},portfolio)).ok,false,'Portfolio never dispatches');
  h.state.local['live-activation-tab:42'].started_at=Date.now()-21000;
  assert.equal((await h.dispatch({type:'mode-heartbeat'},portfolio)).ok,false,'bounded transition cannot keep permission indefinitely');
  h.state.tabUrls[42]=URL_VEST;
  const complete={...message,type:'live-activation-complete'};
  assert.equal((await h.dispatch(complete,pageSender)).ok,false,'completion requires backend Live confirmation');
  h.state.local['observation:42'].registrations[0].phase='live';
  assert.equal((await h.dispatch(complete,pageSender)).ok,true);
  assert.equal(h.state.local['live-activation-tab:42'],null);
  await mode(h,'readonly');
  assert.equal((await h.dispatch(message,pageSender)).ok,false,'read-only cannot activate');
});

test('a delayed actor-restore heartbeat cannot borrow the new preflight epoch and revoke execute',async t=>{
  const h=harness(t);
  const startRestore=deferred(),grantReached=deferred(),allowGrant=deferred(),oldReached=deferred(),allowOld=deferred();
  t.after(()=>{startRestore.resolve();allowGrant.resolve();allowOld.resolve();});
  const original=h.sandbox.sessionRequest;
  let captured=false;
  h.sandbox.sessionRequest=async(tabId,options={})=>{
    if(!captured&&options.heartbeat_only&&!options.authorize){captured=true;await startRestore.promise;}
    return original(tabId,options);
  };
  h.state.fetchHandler=async request=>{
    if(!request.url.endsWith('/api/bridge/session'))return;
    if(request.body.authorize){grantReached.resolve();await allowGrant.promise;}
    else if(request.body.mode==='readonly'){oldReached.resolve(request.body);await allowOld.promise;}
  };
  const restored=await h.dispatch({type:'observation-restore'},pageSender);
  assert.equal(restored.data.mode,'readonly');
  const enabling=mode(h,'execute');
  await grantReached.promise;
  startRestore.resolve();
  const oldBody=await oldReached.promise;
  allowGrant.resolve();
  const granted=await enabling;
  assert.equal(granted.data.mode,'execute');
  allowOld.resolve();
  for(let i=0;i<12;i++)await flush();
  assert.equal(oldBody.mode_epoch,restored.data.mode_epoch,'restore must retain its old epoch even while a newer readonly preflight exists');
  assert.equal(h.state.session['control-mode:42'].mode,'execute');
  assert.equal(h.state.backend.get(granted.data.session_id).mode,'execute');
});

test('read-only revokes the local actor before waiting for an HTTP acknowledgement', async t => {
  const h = harness(t);
  assert.equal((await mode(h, 'execute')).ok, true);
  const waiting = deferred();
  h.state.fetchHandler = (request, response) => request.url.endsWith('/api/bridge/session') && request.body.mode === 'readonly' ? waiting.promise.then(() => response({ mode: 'readonly', mode_epoch: request.body.mode_epoch, session_id: request.body.session_id, accounts: automatic, next_account: null })) : undefined;
  const revoking = mode(h, 'readonly');
  await flush(); await flush();
  assert.equal(modeMessages(h).at(-1)?.mode, 'readonly', 'local actor must stop while the backend is still waiting');
  waiting.resolve();
  const result = await revoking;
  assert.equal(result.ok, true, result.error);
  assert.equal(result.data.mode, 'readonly');
});

test('a late execute authorization cannot overwrite a newer read-only selection', async t => {
  const waiting = deferred();
  const h = harness(t, {
    fetchHandler: (request, response) => request.url.endsWith('/api/bridge/session') && request.body.mode === 'execute' && request.body.authorize === true
      ? waiting.promise.then(() => response({ mode: 'execute', mode_epoch: request.body.mode_epoch, session_id: request.body.session_id, accounts: automatic, next_account: automatic[0] })) : undefined,
  });
  const enabling = mode(h, 'execute');
  await flush(); await flush();
  const readonly = await mode(h, 'readonly');
  assert.equal(readonly.ok, true, readonly.error);
  waiting.resolve();
  await enabling;
  assert.equal(modeMessages(h).at(-1)?.mode, 'readonly', 'late HTTP reply must not reactivate the page');
  assert.ok(!Object.values(h.state.session).some(value => value && value.mode === 'execute'), 'no executable grant survives in session storage');
});

test('execution permission is session-only and a worker with only local observations starts read-only', async t => {
  const h = harness(t);
  assert.equal((await mode(h, 'execute')).ok, true);
  assert.ok(Object.values(h.state.session).some(value => value && value.mode === 'execute'), 'active permission belongs in chrome.storage.session');
  const next = harness(t, { local: clone(h.state.local), session: {} });
  const result = await next.dispatch({ type: 'execution-next' }, pageSender);
  assert.ok(result.ok === false || result.data?.mode !== 'execute', 'stored observation metadata cannot grant execution after reload');
  assert.equal(next.state.requests.some(request => request.body?.authorize === true), false);
});

test('normal execution heartbeats never create a new explicit authorization', async t => {
  const h = harness(t);
  assert.equal((await mode(h, 'execute')).ok, true);
  const requestCount = h.state.requests.length;
  const result = await h.dispatch({ type: 'execution-next' }, pageSender);
  assert.equal(result.ok, true, result.error);
  const heartbeatRequests = h.state.requests.slice(requestCount);
  assert.ok(heartbeatRequests.some(request => request.url.endsWith('/api/bridge/session')));
  assert.equal(heartbeatRequests.some(request => request.body?.authorize === true), false);
});

test('page content cannot authorize itself through the popup-only mode endpoint', async t => {
  const h = harness(t);
  const result = await h.dispatch({ type: 'control-mode', tabId: 42, token: PAIRING, mode: 'execute' }, pageSender);
  assert.equal(result.ok, false);
  assert.equal(h.state.requests.some(request => request.body?.authorize === true), false);
  assert.equal(modeMessages(h).some(message => message.mode === 'execute'), false);
});

test('unsupported mode values cannot create an execution lease', async t => {
  const h = harness(t);
  const result = await mode(h, 'paper');
  assert.equal(result.ok, false);
  assert.equal(h.state.requests.some(request => request.body?.authorize === true), false);
});

test('a failed execution grant leaves the actor read-only and exposes the failure', async t => {
  const h = harness(t, { fetchHandler: request => request.url.endsWith('/api/bridge/session') && request.body.mode === 'execute'
    ? { ok: false, status: 409, json: async () => ({ error: 'Fixture: ambiguous account cannot execute' }) } : undefined });
  const result = await mode(h, 'execute');
  assert.equal(result.ok, false);
  assert.match(result.error, /ambiguous|账户|执行/);
  assert.ok(!Object.values(h.state.session).some(value => value && value.mode === 'execute'));
  assert.equal(modeMessages(h).at(-1)?.mode, 'readonly');
  assert.match(h.state.local['execution-error:42'].message,/ambiguous/,'failure remains available after reopening the popup');
});

test('preflight failure persists its reason and a later successful grant clears it', async t => {
  let fail=true;
  const h=harness(t,{commandHandler:message=>message.type==='prepare-execution'&&fail ? {ok:false,error:'Fixture preflight failed '+PAIRING}:undefined});
  const first=await mode(h,'execute');
  assert.equal(first.ok,false);
  assert.match(h.state.local['execution-error:42'].message,/preflight failed/);
  assert.equal(h.state.local['execution-error:42'].message.includes(PAIRING),false,'stored errors redact the pairing code');
  fail=false;
  assert.equal((await mode(h,'execute')).ok,true);
  assert.equal(h.state.local['execution-error:42'].message,'');
});

test('a delayed failure from an old actor cannot revoke a newer execute switch', async t => {
  const h=harness(t);
  await mode(h,'execute');
  const old=(await h.dispatch({type:'control-status',tabId:42})).data;
  await mode(h,'readonly');
  await mode(h,'execute');
  const result=await h.dispatch({type:'execution-failed',mode_epoch:old.mode_epoch,error:'late old failure'},pageSender);
  assert.equal(result.data.mode,'execute');
  assert.equal(h.state.local['execution-error:42'].message,'');
});

test('an old heartbeat network failure cannot revoke a newer execute permission', async t => {
  const h=harness(t);
  await mode(h,'execute');
  const waiting=deferred();
  h.state.fetchHandler=request=>request.body?.mode==='execute' && !request.body.authorize ? waiting.promise : undefined;
  const heartbeat=h.dispatch({type:'mode-heartbeat'},pageSender);
  for(let i=0;i<4;i++) await flush();
  await mode(h,'readonly');
  await mode(h,'execute');
  waiting.reject(new Error('old heartbeat disconnected'));
  assert.equal((await heartbeat).ok,false);
  assert.equal((await h.dispatch({type:'control-status',tabId:42})).data.mode,'execute');
  assert.equal(h.state.local['execution-error:42'].message,'');
});

test('legacy preparation plans never create a window or send preparation requests', async t => {
  const plan={active:true,run_id:'old-run',plan_id:'old-plan',accounts:[automatic[1]]};
  const h=harness(t,{fetchHandler:(request,response)=>request.url.endsWith('/api/bridge/session') && request.body.mode==='execute' && !request.body.authorize ? response({mode:'execute',mode_epoch:request.body.mode_epoch,accounts:automatic,preparation:plan}) : undefined});
  await mode(h,'execute');
  await h.dispatch({type:'mode-heartbeat'},pageSender);
  for(let i=0;i<8;i++)await flush();
  assert.equal(h.state.windows.length,0);
  assert.equal(h.state.requests.some(request=>request.url.endsWith('/api/bridge/preparation')),false);
});

test('preparation tabs cannot elevate, poll, report trades or use the legacy binding relay', async t => {
  const h = harness(t);
  h.state.local['preparation-tab:99']={ownerTabId:42,tabId:99};
  const sender = {...pageSender,tab:{id:99,url:URL_VEST}};
  for(const message of [{type:'poll',session_id:'forged'}, {type:'result'}, {type:'size'}, {type:'execution-next'}, {type:'observe'},
    {type:'control-mode',tabId:99,mode:'execute',token:PAIRING}, {type:'page-command',tabId:99,message:{type:'configure',config:{armed:true}}}]) {
    const result = await h.dispatch(message,['control-mode','page-command'].includes(message.type)?popupSender:sender);
    assert.equal(result.ok,false,message.type+' must be denied');
  }
  assert.equal(h.state.requests.length,0,'forbidden messages never reach the trading API');
  assert.equal(h.state.messages.length,0,'no trade configuration reaches a preparation page');
});

test('page-calculated sizing is sent only with the current execution session and stops after revocation', async t => {
  const h=harness(t,{fetchHandler:(request,response)=>request.url.endsWith('/api/bridge/size')?response({ok:true,job_id:request.body.job_id,size_percent:request.body.size_percent,quantity:request.body.quantity}):undefined});
  await mode(h,'execute');
  const control=(await h.dispatch({type:'control-status',tabId:42})).data;
  const message={type:'size',job_id:'pct-job',account_id:automatic[0].id,identity:automatic[0].identity,session_id:control.session_id,mode_epoch:control.mode_epoch,size_percent:50,quantity:3.25,snapshot:{position_open:false},stop_loss:null,drawdown:{current:801,limit:1000,percent:null,observed_at:Date.now()/1000}};
  const accepted=await h.dispatch(message,pageSender);
  assert.equal(accepted.ok,true,accepted.error);
  assert.equal(accepted.data.quantity,3.25);
  const forwarded=h.state.requests.find(request=>request.url.endsWith('/api/bridge/size')).body;
  assert.equal(forwarded.stop_loss,null);
  assert.deepEqual(forwarded.drawdown,message.drawdown,'the fresh drawdown evidence reaches the backend');
  const count=h.state.requests.filter(request=>request.url.endsWith('/api/bridge/size')).length;
  await mode(h,'readonly');
  assert.equal((await h.dispatch(message,pageSender)).ok,false);
  assert.equal(h.state.requests.filter(request=>request.url.endsWith('/api/bridge/size')).length,count);
});

test('URL-marked orphan preparation tabs remain read-only after role storage is lost', async t => {
  const h=harness(t);
  h.state.tabUrls[99]=URL_VEST+'#vest-preparation=restored';
  const status=await h.dispatch({type:'control-status',tabId:99});
  assert.equal(status.data.preparation,true);
  assert.equal((await h.dispatch({type:'control-mode',tabId:99,mode:'execute',token:PAIRING})).ok,false);
  assert.equal(h.state.requests.length,0);
});

test('upgrading closes owned old preparation windows and never reopens them', async t => {
  const h=harness(t,{local:{'observation:42':{enabled:true,token:PAIRING,accounts:clone(observed)},'preparation-owner:42':{ownerTabId:42,tabId:100},'preparation-tab:100':{ownerTabId:42,tabId:100}}});
  for(let i=0;i<12;i++)await flush();
  assert.ok(h.state.removedTabs.includes(100));
  await mode(h,'execute');await h.dispatch({type:'mode-heartbeat'},pageSender);
  assert.equal(h.state.windows.length,0);
});


test('a transient local fetch failure blocks delivery and recovers only the existing execution epoch', async t => {
  const h=harness(t);await mode(h,'execute');
  const original=(await h.dispatch({type:'control-status',tabId:42})).data;
  const authorizations=()=>h.state.requests.filter(r=>r.body?.authorize===true).length;
  const grants=authorizations();
  h.state.fetchHandler=r=>{if(r.url.endsWith('/api/bridge/session'))throw new TypeError('Failed to fetch');};
  const failed=await h.dispatch({type:'mode-heartbeat'},pageSender);
  assert.equal(failed.ok,true);assert.equal(failed.data.connection_pending,true);
  assert.equal(failed.data.mode,'execute');assert.equal(failed.data.mode_epoch,original.mode_epoch);
  assert.ok(h.state.messages.some(r=>r.message.type==='connection-state'&&r.message.connection_pending));
  const count=h.state.requests.length;
  const blocked=await h.dispatch({type:'poll',account_id:automatic[0].id,identity:automatic[0].identity,
    session_id:original.session_id,mode_epoch:original.mode_epoch,ready:true},pageSender);
  assert.equal(blocked.data.job,null);assert.equal(blocked.data.connection_pending,true);
  assert.equal(h.state.requests.length,count,'no job polling during a connection hold');
  h.state.fetchHandler=null;
  const recovered=await h.dispatch({type:'mode-heartbeat'},pageSender);
  assert.equal(recovered.data.connection_pending,false);assert.equal(recovered.data.mode_epoch,original.mode_epoch);
  assert.equal(authorizations(),grants,'recovery never creates another authorization');
  assert.equal(h.state.local['execution-error:42'].message,'');
});

test('a late failed heartbeat cannot block a newer successful check of the same epoch',async t=>{
  const h=harness(t);await mode(h,'execute');
  const waiting=deferred();let once=true;
  h.state.fetchHandler=r=>{if(once&&r.url.endsWith('/api/bridge/session')){once=false;return waiting.promise;}};
  const old=h.dispatch({type:'mode-heartbeat'},pageSender);
  for(let i=0;i<5;i++)await flush();
  const fresh=await h.dispatch({type:'mode-heartbeat'},pageSender);
  waiting.reject(new TypeError('Failed to fetch'));
  await old;
  const current=(await h.dispatch({type:'control-status',tabId:42})).data;
  assert.equal(current.mode,'execute');assert.equal(current.connection_pending,false);
  assert.equal(current.connection_revision,fresh.data.connection_revision);
});

test('a sustained outage revokes execution and carries the original failure to later observations',async t=>{
  const h=harness(t,{now:Date.now()});await mode(h,'execute');
  const original=(await h.dispatch({type:'control-status',tabId:42})).data;
  h.state.fetchHandler=r=>{if(r.url.endsWith('/api/bridge/session'))throw new TypeError('Failed to fetch');};
  h.state.now+=1000;
  assert.equal((await h.dispatch({type:'mode-heartbeat'},pageSender)).data.connection_pending,true);
  h.state.now+=20000;
  assert.equal((await h.dispatch({type:'mode-heartbeat'},pageSender)).ok,false);
  const stopped=(await h.dispatch({type:'control-status',tabId:42})).data;
  assert.equal(stopped.mode,'readonly');assert.equal(stopped.mode_epoch,original.mode_epoch+1);
  assert.equal(stopped.revocation_source,'local-service-unavailable');
  assert.match(h.state.local['execution-error:42'].message,/20 秒.*Failed to fetch/);
  h.state.fetchHandler=null;
  await h.dispatch({type:'observe',accounts:observed},pageSender);
  const reported=h.state.requests.filter(r=>r.body?.mode==='readonly').at(-1).body;
  assert.match(reported.execution_error,/Failed to fetch/);
  assert.equal(reported.revocation_source,'local-service-unavailable');
  assert.equal((await h.dispatch({type:'control-status',tabId:42})).data.mode,'readonly');
});

test('HTTP authorization denial and an actual readonly lease bypass connection recovery',async t=>{
  const h=harness(t);await mode(h,'execute');
  h.state.fetchHandler=()=>({ok:false,status:401,json:async()=>({error:'Execution permission denied'})});
  assert.equal((await h.dispatch({type:'mode-heartbeat'},pageSender)).ok,false);
  assert.equal((await h.dispatch({type:'control-status',tabId:42})).data.mode,'readonly');
  h.state.fetchHandler=null;await mode(h,'execute');
  h.state.fetchHandler=(r,response)=>r.url.endsWith('/api/bridge/session')?response({mode:'readonly',mode_epoch:r.body.mode_epoch,accounts:automatic}):undefined;
  assert.equal((await h.dispatch({type:'mode-heartbeat'},pageSender)).data.mode,'readonly');
  assert.equal((await h.dispatch({type:'control-status',tabId:42})).data.mode,'readonly');
});

test('an observation transport failure holds execution without authorizing or replaying an order',async t=>{
  const h=harness(t);await mode(h,'execute');
  h.state.fetchHandler=r=>{if(r.url.endsWith('/api/bridge/observe'))throw new TypeError('Failed to fetch');};
  const reply=await h.dispatch({type:'observe',accounts:observed},pageSender);
  assert.equal(reply.data.connection_pending,true);assert.equal(reply.data.mode,'execute');
  h.state.fetchHandler=null;
  assert.equal((await h.dispatch({type:'mode-heartbeat'},pageSender)).data.connection_pending,false);
});

test('a lost order poll response is never retried or softened into an observation hold',async t=>{
  const h=harness(t);await mode(h,'execute');
  const original=(await h.dispatch({type:'control-status',tabId:42})).data;
  h.state.fetchHandler=r=>{if(r.url.endsWith('/api/bridge/poll'))throw new TypeError('Failed to fetch');};
  const reply=await h.dispatch({type:'poll',account_id:automatic[0].id,identity:automatic[0].identity,
    session_id:original.session_id,mode_epoch:original.mode_epoch,ready:true},pageSender);
  assert.equal(reply.ok,false);assert.equal(reply.code,'LOCAL_SERVICE_UNAVAILABLE');
  assert.equal(h.state.requests.filter(r=>r.url.endsWith('/api/bridge/poll')).length,1);
  assert.equal((await h.dispatch({type:'control-status',tabId:42})).data.connection_pending,false);
});

test('a truncated successful HTTP response holds execution but malformed JSON stops it',async t=>{
  const h=harness(t);await mode(h,'execute');
  h.state.fetchHandler=r=>r.url.endsWith('/api/bridge/session')?{ok:true,status:200,json:async()=>{throw new TypeError('network stream interrupted');}}:undefined;
  assert.equal((await h.dispatch({type:'mode-heartbeat'},pageSender)).data.connection_pending,true);
  h.state.fetchHandler=r=>r.url.endsWith('/api/bridge/session')?{ok:true,status:200,json:async()=>{throw new SyntaxError('invalid JSON');}}:undefined;
  assert.equal((await h.dispatch({type:'mode-heartbeat'},pageSender)).ok,false);
  assert.equal((await h.dispatch({type:'control-status',tabId:42})).data.mode,'readonly');
});

test('closed tabs release execution control records instead of retaining one entry per tab forever',async t=>{
  const h=harness(t);
  for(let id=100;id<130;id++){
    h.state.local['observation:'+id]={enabled:true,token:PAIRING,accounts:clone(observed)};
    assert.equal((await h.dispatch({type:'control-mode',tabId:id,token:PAIRING,mode:'execute'})).ok,true);
    h.state.onRemoved(id);
    for(let i=0;i<20&&h.state.local['observation:'+id];i++)await flush();
    assert.equal(h.state.local['observation:'+id],undefined);
    assert.equal(h.state.session['control-mode:'+id],undefined);
    assert.equal(h.state.session['connection-health:'+id],undefined);
  }
  assert.equal(vm.runInContext('controlChanges.size',h.sandbox),0,'closed-tab control identifiers must be collectible');
});


test('profit claim Portfolio heartbeats check only the reserved claim and never authorize or fake an NQ session',async t=>{
  const h=harness(t);await mode(h,'execute');
  const state=(await h.dispatch({type:'control-status',tabId:42})).data;
  const claim={claim_id:'a'.repeat(32),account_id:automatic[0].id,name:automatic[0].name,identity:automatic[0].identity,phase:'live'};
  h.state.tabUrls[42]='https://next.vestmarkets.com/portfolio';
  const sender={...pageSender,url:h.state.tabUrls[42],tab:{id:42,url:h.state.tabUrls[42]}};
  h.state.fetchHandler=(r,response)=>r.url.endsWith('/api/bridge/profit-claim')?response({mode:'execute',mode_epoch:state.mode_epoch,claim_id:claim.claim_id}):undefined;
  const before=h.state.requests.length;
  const reply=await h.dispatch({type:'mode-heartbeat',profit_claim:claim},sender);
  assert.equal(reply.data.mode,'execute');assert.equal(reply.data.mode_epoch,state.mode_epoch);
  const requests=h.state.requests.slice(before);
  assert.equal(requests.length,1);assert.ok(requests[0].url.endsWith('/api/bridge/profit-claim'));
  assert.equal(requests[0].body.action,'heartbeat');assert.equal(requests[0].body.tab_url,sender.url);
  assert.equal(requests[0].body.authorize,undefined);
  assert.equal((await h.dispatch({type:'execution-next'},sender)).ok,false,'Portfolio never receives order polling authority');
  assert.equal((await h.dispatch({type:'mode-heartbeat',profit_claim:claim},{...sender,url:'https://evil.example/portfolio'})).ok,false);
});

test('a lost profit intent response is sent once and a later readonly mode prevents another submission',async t=>{
  const h=harness(t);await mode(h,'execute');
  const state=(await h.dispatch({type:'control-status',tabId:42})).data;
  const payload={type:'profit-claim',action:'intent',claim_id:'a'.repeat(32),account_id:automatic[0].id,
    name:automatic[0].name,identity:automatic[0].identity,phase:'live',mode_epoch:state.mode_epoch,
    preview:{available:40,amount:40,received:32,recipient:'main'}};
  h.state.fetchHandler=r=>{if(r.url.endsWith('/api/bridge/profit-claim'))throw new TypeError('Lost intent response');};
  const reply=await h.dispatch(payload,pageSender);
  assert.equal(reply.ok,false);assert.equal(reply.code,'LOCAL_SERVICE_UNAVAILABLE');
  assert.equal(h.state.requests.filter(r=>r.url.endsWith('/api/bridge/profit-claim')).length,1);
  h.state.fetchHandler=null;await mode(h,'readonly');
  assert.equal((await h.dispatch(payload,pageSender)).ok,false);
  assert.equal(h.state.requests.filter(r=>r.url.endsWith('/api/bridge/profit-claim')).length,1);
});


test('verified payout balances replace stale observation rows before the next heartbeat',async t=>{
  const h=harness(t);await mode(h,'execute');
  const state=(await h.dispatch({type:'control-status',tabId:42})).data;
  h.state.local['observation:42'].accounts[0].balance=5040;
  h.state.fetchHandler=(r,response)=>r.url.endsWith('/api/bridge/profit-claim')?response({verified:true,claim:{status:'verified'}}):undefined;
  const snapshot={phase:'live',position_open:false,balance:5000,equity:5000,unrealized_pnl:0,observed_at:Date.now()/1000};
  assert.equal((await h.dispatch({type:'profit-claim',action:'verify',mode_epoch:state.mode_epoch,
    claim_id:'a'.repeat(32),account_id:automatic[0].id,name:automatic[0].name,identity:automatic[0].identity,phase:'live',snapshot},pageSender)).ok,true);
  h.state.fetchHandler=null;
  await h.dispatch({type:'mode-heartbeat'},pageSender);
  const last=h.state.requests.filter(r=>r.url.endsWith('/api/bridge/session')).at(-1).body;
  assert.equal(last.accounts[0].balance,5000,'a heartbeat cannot restore the pre-withdrawal balance');
  assert.equal(last.accounts[0].phase_snapshot.balance,5000);
});
