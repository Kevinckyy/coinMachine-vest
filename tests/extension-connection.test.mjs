import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const backgroundSource = fs.readFileSync(new URL('../extension/background.js', import.meta.url), 'utf8');
const EXTENSION_ID = 'abcdefghijklmnopabcdefghijklmnop';
const VEST_URL = 'https://next.vestmarkets.com/trade/NQ-PERP';
const clone = (value) => value == null ? value : JSON.parse(JSON.stringify(value));

// Run the real service-worker program. Only the browser transport and local HTTP
// boundary are substituted, so no account credentials or live orders are used.
function harness(options = {}) {
  const state = {
    ready: false, protocol: 4, version:'0.3.0', url: VEST_URL, injections: [], messages: [],
    storage: {}, fetches: [], listeners: [], timers: new Set(), ...options,
  };
  const timer = (callback, duration, ...args) => {
    const id = setTimeout(() => { state.timers.delete(id); callback(...args); }, options.fastTimers ? Math.min(duration, 20) : duration);
    state.timers.add(id);
    return id;
  };
  const cancelTimer = (id) => { clearTimeout(id); state.timers.delete(id); };
  const chrome = {
    runtime: {
      id: EXTENSION_ID,
      getManifest: () => ({version:state.manifestVersion || '0.3.0'}),
      getURL: (file) => `chrome-extension://${EXTENSION_ID}/${file}`,
      onMessage: { addListener: (listener) => { state.listener = listener; } },
    },
    tabs: {
      get: async (tabId) => ({ id: tabId, url: state.url, status: 'complete' }),
      sendMessage: async (tabId, message, sendOptions) => {
        state.messages.push({ tabId, message: clone(message), options: clone(sendOptions) });
        if (message.type === 'bridge-ping') {
          if (state.pingHandler) return state.pingHandler(message);
          if (!state.ready) throw new Error('Could not establish connection. Receiving end does not exist.');
          return { ok: true, protocol: state.protocol, version: state.version };
        }
        if (!state.ready) throw new Error('Could not establish connection. Receiving end does not exist.');
        if (state.commandHandler) return await state.commandHandler(message, tabId);
        if (message.type === 'observe-start-scan') return { ok: true, started: true, scan_id: 'fixture-scan-1' };
        return { ok: true, snapshot: { balance: 1000, equity: 987.5, unrealized_pnl: -12.5 } };
      },
      onRemoved: { addListener: (listener) => { state.onRemoved = listener; } },
      onUpdated: { addListener: (listener) => { state.onUpdated = listener; } },
    },
    scripting: {
      executeScript: async (injection) => {
        state.injections.push(clone(injection));
        if (state.injectHandler) await state.injectHandler(injection);
        else { state.ready = true; state.protocol = 4; state.version=state.manifestVersion || '0.3.0'; }
        return [{ frameId: 0, result: undefined }];
      },
    },
    storage: {
      local: {
        get: async (keys) => {
          if (keys == null) return clone(state.storage);
          const names = Array.isArray(keys) ? keys : typeof keys === 'string' ? [keys] : Object.keys(keys);
          return Object.fromEntries(names.filter((key) => key in state.storage).map((key) => [key, clone(state.storage[key])]));
        },
        set: async (values) => { Object.assign(state.storage, clone(values)); },
        remove: async (keys) => { for (const key of Array.isArray(keys) ? keys : [keys]) delete state.storage[key]; },
      },
    },
  };
  const sandbox = {
    chrome, URL, AbortController, console, setTimeout: timer, clearTimeout: cancelTimer,
    fetch: async (url, init = {}) => {
      state.fetches.push({ url: String(url), method: init.method || 'GET' });
      if (state.fetchHandler) return await state.fetchHandler(url, init);
      return { ok: true, status: 200, json: async () => ({ ok: true, accounts: [] }) };
    },
  };
  sandbox.importScripts = file => { if(file !== 'core.js') throw new Error('Unexpected worker script'); vm.runInContext(fs.readFileSync(new URL('../extension/core.js', import.meta.url), 'utf8'),sandbox); };
  vm.createContext(sandbox);
  vm.runInContext(backgroundSource, sandbox, { filename: 'extension/background.js' });
  const popupSender = { id: EXTENSION_ID, url: `chrome-extension://${EXTENSION_ID}/popup.html`, origin: `chrome-extension://${EXTENSION_ID}` };
  const dispatch = (message, sender = popupSender) => new Promise((resolve, reject) => {
    const id = setTimeout(() => reject(new Error('Background runtime message did not answer within 2 seconds')), 2000);
    try {
      state.listener(message, sender, (result) => { clearTimeout(id); resolve(result); });
    } catch (error) { clearTimeout(id); reject(error); }
  });
  return {
    state, sandbox, popupSender, dispatch,
    dispose: () => { for (const id of state.timers) clearTimeout(id); },
    business: () => state.messages.filter(({ message }) => message.type !== 'bridge-ping'),
  };
}

function useHarness(t, options) {
  const h = harness(options);
  t.after(h.dispose);
  return h;
}

test('a pre-existing Vest tab with no receiver gets one isolated-world injection before its command', async (t) => {
  const h = useHarness(t);
  const response = await h.sandbox.sendPageCommand(42, { type: 'capture' });
  assert.equal(response.ok, true);
  assert.equal(h.state.injections.length, 1);
  assert.deepEqual(h.state.injections[0].target, { tabId: 42, frameIds: [0] });
  assert.equal(h.state.injections[0].world, 'ISOLATED');
  assert.deepEqual(h.state.injections[0].files, ['core.js', 'content.js']);
  assert.equal(h.state.messages.filter(item => item.message.type === 'bridge-ping').length, 2);
  assert.equal(h.business().filter(item => item.message.type === 'capture').length, 1);
  assert.equal(h.business()[0].options.frameId, 0);
});

test('a healthy receiver is used without re-injecting or replaying the requested operation', async (t) => {
  const h = useHarness(t, { ready: true });
  await h.sandbox.sendPageCommand(42, { type: 'inspect' });
  assert.equal(h.state.injections.length, 0);
  assert.deepEqual(h.business().map(item => item.message.type), ['inspect']);
});

test('an outdated receiver must complete the new handshake before business messages', async (t) => {
  const h = useHarness(t, { ready: true, protocol: 2 });
  await h.sandbox.sendPageCommand(42, { type: 'capture' });
  assert.equal(h.state.injections.length, 1);
  assert.equal(h.state.protocol, 4);
  assert.deepEqual(h.business().map(item => item.message.type), ['capture']);
});

test('an old content script with the current protocol is upgraded before enabling execution', async t => {
  const h=useHarness(t,{ready:true,protocol:4,version:'0.3.6',manifestVersion:'0.3.7'});
  await h.sandbox.sendPageCommand(42,{type:'capture'});
  assert.equal(h.state.injections.length,1);
  assert.equal(h.state.version,'0.3.7');
  assert.equal(h.business().length,1,'upgrade never replays the business command');
});

test('concurrent connections to the same tab share one content-script injection', async (t) => {
  const h = useHarness(t);
  await Promise.all([
    h.sandbox.sendPageCommand(42, { type: 'capture' }),
    h.sandbox.sendPageCommand(42, { type: 'inspect' }),
  ]);
  assert.equal(h.state.injections.length, 1);
  assert.equal(h.business().filter(item => item.message.type === 'capture').length, 1);
  assert.equal(h.business().filter(item => item.message.type === 'inspect').length, 1);
});

test('missing website access produces an actionable error and no business operation', async (t) => {
  const h = useHarness(t, { injectHandler: async () => { throw new Error('Cannot access contents of url. Extension manifest must request permission to access this host.'); } });
  await assert.rejects(h.sandbox.sendPageCommand(42, { type: 'capture' }), /权限|访问|站点|permission|access/i);
  assert.equal(h.state.injections.length, 1);
  assert.equal(h.business().length, 0);
});

test('a navigated-away frame does not cause the real page operation to be retried', async (t) => {
  const h = useHarness(t, {
    ready: true,
    commandHandler: async () => { throw new Error('Frame with ID 0 was removed.'); },
  });
  await assert.rejects(h.sandbox.sendPageCommand(42, { type: 'prepare' }));
  assert.equal(h.state.injections.length, 0);
  assert.deepEqual(h.business().map(item => item.message.type), ['prepare']);
});

test('a command with an uncertain response is never replayed after receiver loss', async (t) => {
  let operations = 0;
  const h = useHarness(t, {
    ready: true,
    commandHandler: async () => { operations++; throw new Error('Could not establish connection. Receiving end does not exist.'); },
  });
  await assert.rejects(h.sandbox.sendPageCommand(42, { type: 'configure', config: { armed: true } }));
  assert.equal(operations, 1);
  assert.equal(h.state.injections.length, 0);
  assert.equal(h.business().length, 1);
});

test('a failed connection clears the reading indicator and disables read-only continuation', async (t) => {
  const h = useHarness(t, {
    storage: { 'observation-status:42': { phase: 'reading', progress: { completed: 0, total: 5 }, error: '' } },
    injectHandler: async () => { throw new Error('Cannot access contents of the page'); },
  });
  const response = await h.dispatch({ type: 'observe-start', tabId: 42, token: 'fixture-pairing-only' });
  assert.equal(response.ok, false);
  const status = h.state.storage['observation-status:42'];
  assert.equal(status.phase, 'error');
  assert.ok(status.error);
  assert.ok(status.progress == null || status.progress.done === true, 'failed scan must not leave a pending reading counter');
  assert.equal(h.state.storage['observation:42']?.enabled, false);
  assert.equal(h.business().filter(item => item.message.type === 'observe-start-scan').length, 0);
});

test('a different market is rejected before injection or account selection', async (t) => {
  const h = useHarness(t, { url: 'https://next.vestmarkets.com/trade/ES-PERP' });
  await assert.rejects(h.sandbox.sendPageCommand(42, { type: 'capture' }), /NQ|Vest|页面|标签页/i);
  assert.equal(h.state.injections.length, 0);
  assert.equal(h.business().length, 0);
});

test('webpage content cannot invoke the extension-only page-command relay', async (t) => {
  const h = useHarness(t, { ready: true });
  const response = await h.dispatch({ type: 'page-command', tabId: 42, message: { type: 'capture' } }, { id: EXTENSION_ID, tab: { id: 42 }, url: VEST_URL, origin: 'https://next.vestmarkets.com' });
  assert.equal(response.ok, false);
  assert.equal(h.business().length, 0);
});

test('the relay accepts the own-extension popup and never forwards arbitrary commands', async (t) => {
  const h = useHarness(t, { ready: true });
  const response = await h.dispatch({ type: 'page-command', tabId: 42, message: { type: 'capture' } });
  assert.equal(response.ok, true);
  assert.equal(response.data.ok, true);
  const forbidden = await h.dispatch({ type: 'page-command', tabId: 42, message: { type: 'execute-arbitrary-trade' } });
  assert.equal(forbidden.ok, false);
  assert.deepEqual(h.business().map(item => item.message.type), ['capture']);
});

test('opening the extension popup in its own full tab can still contact the Vest target', async (t) => {
  const h = useHarness(t, { ready: true });
  const response = await h.dispatch({ type: 'page-command', tabId: 42, message: { type: 'capture' } }, { ...h.popupSender, tab: { id: 99 } });
  assert.equal(response.ok, true);
  assert.deepEqual(h.business().map(item => item.message.type), ['capture']);
});

test('account scanning answers immediately and progresses independently of the initiating popup', async (t) => {
  const h = useHarness(t, { ready: true });
  const start = await h.dispatch({ type: 'observe-start', tabId: 42, token: 'fixture-pairing-only' });
  assert.equal(start.ok, true);
  assert.equal(start.data.started, true);
  assert.equal(h.state.storage['observation:42'].enabled, true);
  assert.equal(h.business().filter(item => item.message.type === 'observe-start-scan').length, 1);
  assert.equal(h.business().find(item=>item.message.type==='observe-start-scan').message.account_timeout_seconds,3);
  // The initiating popup no longer participates: the page reports its own result.
  const result = await h.dispatch({ type: 'observe', accounts: [], error: '', progress: { completed: 0, total: 0, done: true, restored: true } }, { id: EXTENSION_ID, tab: { id: 42 }, url: VEST_URL });
  assert.equal(result.ok, true);
  assert.equal(h.state.storage['observation-status:42'].progress.done, true);
  assert.equal(h.state.storage['observation:42'].enabled, true);
  assert.equal(h.business().filter(item => item.message.type === 'observe-start-scan').length, 1);
});

test('two popups starting the same scan cannot disable the one accepted scan', async (t) => {
  const h = useHarness(t, { ready: true });
  const responses = await Promise.all([
    h.dispatch({ type: 'observe-start', tabId: 42, token: 'fixture-pairing-only' }),
    h.dispatch({ type: 'observe-start', tabId: 42, token: 'fixture-pairing-only' }),
  ]);
  assert.equal(responses.filter(response => response.ok && response.data.started).length, 1);
  assert.equal(responses.filter(response => !response.ok).length, 1);
  assert.equal(h.business().filter(item => item.message.type === 'observe-start-scan').length, 1);
  assert.equal(h.state.storage['observation:42'].enabled, true, 'a rejected concurrent start must not stop the accepted background scan');
  assert.notEqual(h.state.storage['observation-status:42'].phase, 'error');
});

test('saved per-account scan timeout is passed to the page and long reads retain the active scan',async t=>{
  const h=useHarness(t,{ready:true,storage:{'account-scan-settings':{timeout_seconds:7.5}}});
  const start=await h.dispatch({type:'observe-start',tabId:42,token:'fixture-pairing-only'});
  assert.equal(start.ok,true,start.error);
  assert.equal(h.business().find(item=>item.message.type==='observe-start-scan').message.account_timeout_seconds,7.5);
  h.state.storage['observation-status:42']={phase:'scanning',time:new Date(Date.now()-100000).toISOString(),progress:{account_timeout_seconds:120}};
  const duplicate=await h.dispatch({type:'observe-start',tabId:42,token:'fixture-pairing-only'});
  assert.equal(duplicate.ok,false);assert.match(duplicate.error,/仍在进行/);
  assert.equal(h.state.storage['observation:42'].enabled,true);
});

test('completed scan counts survive subsequent current-account observations',async t=>{
  const summary={completed:3,total:3,succeeded:2,failed:1,unread:0,done:true,restored:true,account_timeout_seconds:3};
  const h=useHarness(t,{ready:true,storage:{'observation:42':{enabled:true,token:'fixture-pairing-only'},'observation-status:42':{phase:'complete',progress:summary}}});
  const result=await h.dispatch({type:'observe',accounts:[],error:''},{id:EXTENSION_ID,tab:{id:42},url:VEST_URL});
  assert.equal(result.ok,true,result.error);
  assert.deepEqual(h.state.storage['observation-status:42'].progress,summary);
  assert.equal(h.state.storage['observation-status:42'].phase,'observing');
});

test('stopping a scan keeps its controls busy until the page reports restoration complete',async t=>{
  const h=useHarness(t,{ready:true,storage:{'observation:42':{enabled:true,token:'fixture-pairing-only'},
    'observation-status:42':{phase:'scanning',progress:{completed:1,total:3,succeeded:1,failed:0,unread:2}}},
    commandHandler:async m=>m.type==='observation-stop'?{ok:true,scan_running:true}:{ok:true}});
  const result=await h.dispatch({type:'observe-stop',tabId:42});assert.equal(result.ok,true,result.error);
  assert.equal(h.state.storage['observation-status:42'].phase,'stopping');
  assert.equal(h.state.storage['observation-status:42'].progress.done,false);
  const finished=await h.dispatch({type:'observe',accounts:[],progress:{completed:1,total:3,succeeded:1,failed:0,unread:2,done:true,restored:true}},
    {id:EXTENSION_ID,tab:{id:42},url:VEST_URL});
  assert.equal(finished.ok,true,finished.error);
  assert.equal(h.state.storage['observation-status:42'].phase,'complete');
});

test('stopping after receiver loss disables observation without re-injecting scripts', async (t) => {
  const h = useHarness(t, { storage: { 'observation:42': { enabled: true, token: 'fixture-pairing-only' } } });
  const response = await h.dispatch({ type: 'observe-stop', tabId: 42 });
  assert.equal(response.ok, true);
  assert.equal(h.state.storage['observation:42'].enabled, false);
  assert.equal(h.state.injections.length, 0);
});

test('a listener which never answers receives a bounded connection error', async (t) => {
  const h = useHarness(t, { fastTimers: true, pingHandler: () => new Promise(() => {}) });
  await assert.rejects(h.sandbox.sendPageCommand(42, { type: 'capture' }), /超时|无响应|连接|timeout/i);
  assert.equal(h.business().length, 0);
});

test('a failed handshake after injection cannot enter an endless injection loop', async (t) => {
  const h = useHarness(t, { injectHandler: async () => {} });
  await assert.rejects(h.sandbox.sendPageCommand(42, { type: 'capture' }));
  assert.equal(h.state.injections.length, 1);
  assert.equal(h.business().length, 0);
});
