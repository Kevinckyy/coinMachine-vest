// Real MV3 / Chrome messaging regression, in an isolated temporary Edge profile.
// Vest documents are fulfilled from a local fixture through CDP. No request is
// sent to Vest and the extension's copied endpoint targets only a fake API.
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { createServer } from 'node:http';
import { cp, mkdir, readFile, writeFile } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { randomUUID } from 'node:crypto';
import path from 'node:path';

const root = fileURLToPath(new URL('../', import.meta.url));
const edge = process.env.EDGE_PATH || [
  'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe',
].find(existsSync);
if (!edge) throw new Error('Set EDGE_PATH to an installed Chromium / Edge executable.');
const run = path.join(root, '.runtime', 'extension-browser-smoke-' + randomUUID());
const extensionDir = path.join(run, 'extension');
const profileDir = path.join(run, 'edge');
await mkdir(run, { recursive: true });
const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
async function until(action, label, timeout = 15000) {
  const end = Date.now() + timeout;
  let lastError;
  while (Date.now() < end) {
    try { const result = await action(); if (result) return result; } catch (error) { lastError = error; }
    await pause(150);
  }
  throw new Error('Timeout: ' + label + (lastError ? ' (' + lastError.message + ')' : ''));
}

const uploads = [];
const sessionRequests = [];
const pollRequests = [];
const jobResults = [];
const executionSessions = new Map();
let queuedOpen = null;
let monitorAccount = false;
let observationGate = null;
let cancelledJobDeliveries = 0;
let preparationPlan = null;
const preparationObservations = [];
const sizingRequests = [];
const inspections = [];
const unexpectedRequests = [];
const fakeToken = 'isolated-extension-smoke-not-a-real-token';
const automaticAccounts = [
  { id: 'auto-alpha', name: 'QA Alpha', category: 'eval', identity: 'eval:QA Alpha', mode: 'browser', auto_discovered: true },
  { id: 'auto-beta', name: 'QA Beta', category: 'funded', identity: 'funded:QA Beta', mode: 'browser', auto_discovered: true },
  { id: 'auto-main', name: '主账户', category: 'default', identity: 'default:主账户', mode: 'browser', auto_discovered: true, execution_excluded:true },
];
const api = createServer(async (request, response) => {
  const send = (status, body) => {
    response.writeHead(status, { 'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*', 'Access-Control-Allow-Headers': 'Content-Type, X-Bridge-Token', 'Access-Control-Allow-Methods': 'GET, POST, OPTIONS' });
    response.end(JSON.stringify(body));
  };
  if (request.method === 'OPTIONS') return send(204, {});
  if (request.headers['x-bridge-token'] !== fakeToken) return send(401, { error: 'Fixture token mismatch' });
  if (request.url === '/api/bridge/accounts') return send(200, { accounts: [] });
  if (request.url === '/api/bridge/session' && request.method === 'POST') {
    let raw = '';
    for await (const chunk of request) raw += chunk;
    const body = JSON.parse(raw);
    sessionRequests.push(body);
    for (const row of body.accounts || []) {
      const account = automaticAccounts.find(account => account.name === row.name);
      if (account) { account.category = row.category; account.identity = row.category + ':' + row.name; }
    }
    assert.equal(typeof body.session_id, 'string', 'execution authorization must be scoped to a browser session');
    const previous = executionSessions.get(body.session_id) || { mode: 'readonly', mode_epoch: 0 };
    let current = previous;
    if (body.mode_epoch >= previous.mode_epoch) {
      if (body.mode === 'readonly') current = { mode: 'readonly', mode_epoch: body.mode_epoch };
      else if (body.mode === 'execute' && (body.authorize === true || previous.mode === 'execute')) current = { mode: 'execute', mode_epoch: body.mode_epoch };
    }
    executionSessions.set(body.session_id, current);
    return send(200, { ok: true, session_id: body.session_id, ...current, accounts: automaticAccounts,
      next_account: current.mode === 'execute' && (monitorAccount || queuedOpen) && !body.heartbeat_only ? {...automaticAccounts[0],operation:queuedOpen?.type || 'monitor'} : null,
      ...(preparationPlan ? {preparation:{...preparationPlan,active:current.mode==='execute' && preparationPlan.active}} : {}) });
  }
  if (request.url === '/api/bridge/preparation' && request.method === 'POST') {
    let raw=''; for await(const chunk of request) raw+=chunk;
    const body=JSON.parse(raw);
    if(body.observation) preparationObservations.push(body);
    return send(200, preparationPlan || {active:false,accounts:[]});
  }
  if (request.url === '/api/bridge/observe' && request.method === 'POST') {
    let raw = '';
    for await (const chunk of request) raw += chunk;
    const body = JSON.parse(raw);
    uploads.push(body);
    if (observationGate) { observationGate.entered=true; await observationGate.promise; }
    return send(200, { ok: true, accounts_received: body.accounts?.length || 0, accounts: automaticAccounts });
  }
  if (request.url === '/api/bridge/poll' && request.method === 'POST') {
    let raw = '';
    for await (const chunk of request) raw += chunk;
    const body = JSON.parse(raw);
    pollRequests.push(body);
    let job = null;
    if (queuedOpen) {
      job = { ...queuedOpen, account_id: body.account_id, session_id: body.session_id, mode_epoch: body.mode_epoch, identity: body.identity };
      queuedOpen = null;
      cancelledJobDeliveries++;
    }
    return send(200, { job, mode: 'browser', server_time: Date.now() / 1000 });
  }
  if (request.url === '/api/bridge/size' && request.method === 'POST') {
    let raw=''; for await(const chunk of request) raw+=chunk;
    sizingRequests.push(JSON.parse(raw));
    return send(409,{error:'Fixture declines sizing acknowledgment to forbid all order clicks'});
  }
  if (request.url === '/api/bridge/result' && request.method === 'POST') {
    let raw = '';
    for await (const chunk of request) raw += chunk;
    jobResults.push(JSON.parse(raw));
    automaticAccounts[0].status='locked';
    return send(200, { ok: true, status: 'unknown' });
  }
  if (request.url === '/api/bridge/inspect' && request.method === 'POST') {
    let raw='';for await(const chunk of request)raw+=chunk;
    const body=JSON.parse(raw);inspections.push(body);
    return send(200,{ok:true,read_only:true,account_id:body.account_id,position_open:body.snapshot.position_open});
  }
  unexpectedRequests.push({ path: request.url, method: request.method });
  send(404, { error: 'Fixture forbids this endpoint' });
});
await new Promise((resolve) => api.listen(0, '127.0.0.1', resolve));
const endpoint = 'http://127.0.0.1:' + api.address().port;

class CDP {
  constructor(socket) {
    this.socket = socket;
    this.sequence = 0;
    this.pending = new Map();
    this.listeners = new Map();
    socket.addEventListener('message', (event) => {
      const data = JSON.parse(event.data);
      if (data.id && this.pending.has(data.id)) {
        const pending = this.pending.get(data.id); this.pending.delete(data.id); clearTimeout(pending.timer);
        data.error ? pending.reject(new Error(JSON.stringify(data.error))) : pending.resolve(data.result);
      } else if (data.method) {
        for (const handler of this.listeners.get(data.method) || []) Promise.resolve(handler(data.params)).catch(() => {});
      }
    });
    socket.addEventListener('close', () => {
      for (const pending of this.pending.values()) { clearTimeout(pending.timer); pending.reject(new Error('CDP connection closed')); }
      this.pending.clear();
    });
  }
  static async open(url) {
    const socket = new WebSocket(url);
    await new Promise((resolve, reject) => { socket.addEventListener('open', resolve, { once: true }); socket.addEventListener('error', reject, { once: true }); });
    return new CDP(socket);
  }
  on(method, handler) {
    if (!this.listeners.has(method)) this.listeners.set(method, []);
    this.listeners.get(method).push(handler);
  }
  command(method, params = {}, timeout = 10000, sessionId) {
    return new Promise((resolve, reject) => {
      const id = ++this.sequence;
      const timer = setTimeout(() => { this.pending.delete(id); reject(new Error('CDP timeout: ' + method)); }, timeout);
      this.pending.set(id, { resolve, reject, timer });
      this.socket.send(JSON.stringify({ id, method, params, ...(sessionId ? {sessionId} : {}) }));
    });
  }
  async evaluate(expression, timeout = 15000) {
    const result = await this.command('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true }, timeout);
    if (result.exceptionDetails) throw new Error(result.exceptionDetails.exception?.description || result.exceptionDetails.text);
    return result.result.value;
  }
  close() { this.socket.close(); }
}

const fixtureHtml = `<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Isolated Vest connection fixture</title>
<style>body{font:14px sans-serif;padding:20px}button{padding:8px} [role=menu]{border:1px solid #333;padding:8px} [role=menuitem]{padding:8px}</style></head><body>
<h1>Simulated accounts, never a trading site</h1>
<button data-testid="account-selector-trigger" aria-expanded="false"><span class="truncate">QA Alpha</span><span>Evaluation</span></button>
<p data-testid="account-value">$987.50</p><p data-testid="account-upnl">-$12.50</p>
<p data-testid="book-mark">$20,100.00</p>
<div data-testid="order-form">
<button data-testid="ordertype-tab-market" role="tab" aria-selected="true" data-state="active">Market</button>
<input data-testid="size-input" value="2">
<button data-testid="size-unit-select" data-unit="token">NQ</button>
<button data-testid="size-pct-25">25%</button><button data-testid="size-pct-50">50%</button><button data-testid="size-pct-75">75%</button><button data-testid="size-pct-100">100%</button><span role="slider" aria-valuenow="0"></span>
<button data-testid="tpsl-toggle" role="checkbox" aria-checked="true" data-state="checked">TP/SL</button>
<button data-testid="tpsl-unit-usd" role="tab" aria-selected="false" data-state="inactive">In dollars</button>
<input data-testid="tpsl-input-tp" value="100"><input data-testid="tpsl-input-sl" value="50">
<button data-testid="tpsl-limit-switch" role="switch" aria-checked="false" data-state="unchecked">Limit off</button>
<button data-testid="submit-long">Forbidden buy</button><button data-testid="submit-short">Forbidden sell</button>
</div>
<button data-testid="account-tab-active-positions" role="tab" aria-selected="true" data-state="active" aria-controls="fixture-positions">Active Positions</button>
<div id="fixture-positions" role="tabpanel"><p>No positions yet</p></div>
<script>(()=>{ const state=window.__fixture={selected:'alpha',selections:[],tradeClicks:0};
const accounts=state.accounts=[{id:'alpha',name:'QA Alpha',category:'eval',badge:'Evaluation',equity:'$987.50',pnl:'-$12.50'},{id:'beta',name:'QA Beta',category:'funded',badge:'Funded',equity:'$1,275.50',pnl:'+$25.50'},{id:'main',name:'主账户',category:'default',badge:'Default',equity:'$0.00',pnl:'$0.00'}];
const trigger=document.querySelector('[data-testid="account-selector-trigger"]'); let menu=null;
document.querySelector('[data-testid="tpsl-unit-usd"]').addEventListener('mousedown',()=>setTimeout(()=>{const tab=document.querySelector('[data-testid="tpsl-unit-usd"]');tab.setAttribute('aria-selected','true');tab.dataset.state='active';},350));
document.addEventListener('keydown',event=>{const unit=event.target.closest('[data-testid="size-unit-select"]');if(!unit || event.key!=='ArrowDown')return;const menu=document.createElement('div');menu.setAttribute('role','listbox');for(const value of ['token','micro']){const option=document.createElement('div');option.tabIndex=0;option.setAttribute('role','option');option.dataset.testid='size-unit-option-'+value;option.textContent=value;const choose=()=>{unit.dataset.unit=value;unit.textContent=value==='micro'?'Micro':'NQ';menu.remove();};option.onkeydown=e=>{if(e.key==='Enter')choose();};option.onclick=choose;menu.append(option);}document.body.append(menu);});
function close(){menu?.remove();menu=null;trigger.setAttribute('aria-expanded','false');}
function open(){if(menu)return;menu=document.createElement('div');menu.setAttribute('role','menu');menu.dataset.slot='dropdown-menu-content';menu.onkeydown=e=>{if(e.key==='Escape')close();};for(const account of accounts){const row=document.createElement('div');row.setAttribute('role','menuitem');row.dataset.testid='account-item-'+account.category;const name=document.createElement('span');name.className='truncate';name.textContent=account.name;const badge=document.createElement('span');badge.textContent=account.badge;row.append(name,badge);row.onclick=()=>{state.selected=account.id;state.selections.push(account.id);trigger.firstElementChild.textContent=account.name;trigger.lastElementChild.textContent=account.badge;close();setTimeout(()=>{document.querySelector('[data-testid="account-value"]').textContent=account.equity;document.querySelector('[data-testid="account-upnl"]').textContent=account.pnl;},200);};menu.append(row);}document.body.append(menu);trigger.setAttribute('aria-expanded','true');}
trigger.onkeydown=e=>{if(e.key==='ArrowDown')open();if(e.key==='Escape')close();};trigger.onpointerdown=()=>menu?close():open();trigger.onclick=()=>menu?close():open();for(const button of document.querySelectorAll('[data-testid="submit-long"],[data-testid="submit-short"]'))button.onclick=()=>{state.tradeClicks++;};state.pctClicks=[];for(const percent of [25,50,75,100])document.querySelector('[data-testid="size-pct-'+percent+'"]').onclick=()=>{state.pctClicks.push(percent);setTimeout(()=>{const unit=document.querySelector('[data-testid="size-unit-select"]').dataset.unit;document.querySelector('[data-testid="size-input"]').value=String(percent/100*(unit==='micro'?4:8));document.querySelector('[data-testid="order-form"] [role="slider"]').setAttribute('aria-valuenow',String(percent));},100);};})();<\/script>
</body></html>`;

let browser, browserCDP;
const connections = [];
try {
  await cp(path.join(root, 'extension'), extensionDir, { recursive: true });
  const backgroundPath = path.join(extensionDir, 'background.js');
  const originalBackground = await readFile(backgroundPath, 'utf8');
  assert.ok(originalBackground.includes("const ENDPOINT = 'http://127.0.0.1:8765';"), 'test must redirect the copied endpoint away from the user service');
  await writeFile(backgroundPath, originalBackground.replace("const ENDPOINT = 'http://127.0.0.1:8765';", `const ENDPOINT = '${endpoint}';`));
  const manifestPath = path.join(extensionDir, 'manifest.json');
  const manifest = JSON.parse(await readFile(manifestPath, 'utf8'));
  manifest.host_permissions.push(endpoint + '/*');
  await writeFile(manifestPath, JSON.stringify(manifest, null, 2));
  browser = spawn(edge, ['--headless=new', '--disable-gpu', '--no-first-run', '--disable-background-networking', '--disable-component-update', '--disable-default-apps', '--disable-sync', '--remote-debugging-port=0', '--user-data-dir=' + profileDir, '--disable-extensions-except=' + extensionDir, '--load-extension=' + extensionDir, 'about:blank'], { windowsHide: true, stdio: 'ignore' });
  const debugging = await until(async () => (await readFile(path.join(profileDir, 'DevToolsActivePort'), 'utf8')).trim().split('\n'), 'isolated Edge debugging endpoint');
  const debugBase = 'http://127.0.0.1:' + Number(debugging[0]);
  browserCDP = await CDP.open('ws://127.0.0.1:' + Number(debugging[0]) + debugging[1]);
  const targets = async () => (await browserCDP.command('Target.getTargets')).targetInfos;
  const worker = await until(async () => (await targets()).find((target) => target.type === 'service_worker' && /^chrome-extension:\/\/[a-p]+\/background\.js$/.test(target.url)), 'real unpacked MV3 service worker (this browser may disallow --load-extension)', 12000);
  const extensionId = new URL(worker.url).hostname;
  const connectTarget = async (targetId) => {
    const target = await until(async () => (await (await fetch(debugBase + '/json/list')).json()).find((item) => item.id === targetId), 'target WebSocket');
    const client = await CDP.open(target.webSocketDebuggerUrl); connections.push(client); return client;
  };
  const vestTarget = await browserCDP.command('Target.createTarget', { url: 'about:blank' });
  const vestPage = await connectTarget(vestTarget.targetId);
  let vestNetworkRequests = 0;
  vestPage.on('Fetch.requestPaused', async ({ requestId, request, resourceType }) => {
    if (request.url.startsWith('https://next.vestmarkets.com/')) {
      vestNetworkRequests++;
      await vestPage.command('Fetch.fulfillRequest', { requestId, responseCode: 200, responseHeaders: [{ name: 'Content-Type', value: resourceType === 'Document' ? 'text/html; charset=utf-8' : 'text/plain' }], body: Buffer.from(resourceType === 'Document' ? fixtureHtml : '').toString('base64') });
    } else await vestPage.command('Fetch.failRequest', { requestId, errorReason: 'BlockedByClient' });
  });
  await vestPage.command('Fetch.enable', { patterns: [{ urlPattern: '*', requestStage: 'Request' }] });
  await vestPage.command('Page.navigate', { url: 'https://next.vestmarkets.com/trade/NQ-PERP' });
  await until(() => vestPage.evaluate(`document.readyState === 'complete' && document.querySelector('[data-testid="account-value"]')?.textContent === '$987.50'`), 'simulated Vest document');

  await browserCDP.command('Target.activateTarget', { targetId: vestTarget.targetId });
  const popupTarget = await browserCDP.command('Target.createTarget', { url: 'chrome-extension://' + extensionId + '/popup.html', background: true });
  const popup = await connectTarget(popupTarget.targetId);
  await until(() => popup.evaluate(`typeof chrome !== 'undefined' && typeof chrome.runtime?.sendMessage === 'function'`), 'actual extension page');
  const tabId = await popup.evaluate(`(async()=>{const tabs=await chrome.tabs.query({url:'https://next.vestmarkets.com/trade/*'});return tabs[0]?.id;})()`);
  assert.equal(typeof tabId, 'number', 'actual browser tab ID');
  const call = (message) => popup.evaluate(`chrome.runtime.sendMessage(${JSON.stringify(message)})`);
  const ping = await call({ type: 'page-command', tabId, message: { type: 'bridge-ping' } });
  assert.equal(ping.ok, true, 'normal actual runtime->background->tab connection');

  const disposed = await popup.evaluate(`(async()=>{const result=await chrome.scripting.executeScript({target:{tabId:${tabId},frameIds:[0]},world:'ISOLATED',func:()=>{const c=globalThis.__vestControlBridgeController;if(!c)throw new Error('Controller not found');c.dispose();return true;}});return result[0].result;})()`);
  assert.equal(disposed, true);
  const absent = await popup.evaluate(`chrome.tabs.sendMessage(${tabId},{type:'bridge-ping'},{frameId:0}).then(()=>({failed:false}),error=>({failed:true,message:error.message}))`);
  assert.equal(absent.failed, true, 'the native browser must reproduce the missing receiving end before testing recovery');
  assert.match(absent.message, /receiving end|establish connection/i);

  const repaired = await call({ type: 'page-command', tabId, message: { type: 'bridge-ping' } });
  assert.equal(repaired.ok, true, 'missing content script is automatically injected and reconnected');
  const identityMarker = `chrome.scripting.executeScript({target:{tabId:${tabId},frameIds:[0]},world:'ISOLATED',func:()=>{globalThis.__qaController=globalThis.__vestControlBridgeController;return !!globalThis.__qaController;}}).then(r=>r[0].result)`;
  assert.equal(await popup.evaluate(identityMarker), true);
  await call({ type: 'page-command', tabId, message: { type: 'bridge-ping' } });
  assert.equal(await popup.evaluate(`chrome.scripting.executeScript({target:{tabId:${tabId},frameIds:[0]},world:'ISOLATED',func:()=>globalThis.__qaController===globalThis.__vestControlBridgeController}).then(r=>r[0].result)`), true, 'a healthy connection must reuse its content controller');

  const startedAt = Date.now();
  const started = await call({ type: 'observe-start', tabId, token: fakeToken });
  assert.equal(started.ok, true, 'scan start handshake');
  assert.equal(started.data?.started, true, 'scan acknowledges asynchronously instead of waiting in the popup');
  assert.ok(Date.now() - startedAt < 5000, 'the start action must respond while the scan runs');
  const completed = await until(() => uploads.find((item) => item.progress?.done), 'actual background receives completed scan', 30000);
  assert.equal(completed.progress.restored, true, 'scan restores the original account');
  assert.equal(completed.accounts.length, 3);
  assert.equal(completed.accounts.find((item) => item.name === 'QA Alpha').balance, 1000);
  assert.equal(completed.accounts.find((item) => item.name === 'QA Beta').balance, 1250);
  // Reproduce the reported switch failure: view the excluded main account and
  // remove every opening control. Enabling an idle permission must not prepare
  // this account, change the selection, poll a job or touch an order input.
  await vestPage.evaluate(`(() => {document.querySelector('[data-testid="account-selector-trigger"]').click();document.querySelector('[data-testid="account-item-default"]').click();window.__fixture.savedForm=document.querySelector('[data-testid="order-form"]');window.__fixture.savedForm.remove();})()`);
  await until(()=>vestPage.evaluate(`window.__fixture.selected==='main' && document.querySelector('[data-testid="account-value"]').textContent==='$0.00'`),'main account is selected with no opening form');
  await until(()=>uploads.some(item=>item.accounts.some(account=>account.name==='主账户' && account.current)),'main observation reaches the background');
  await until(()=>popup.evaluate(`!document.getElementById('modeSwitch').disabled`),'switch is ready after scanning');
  const idlePolls=pollRequests.length;
  const idleSelections=await vestPage.evaluate('window.__fixture.selections.length');
  const idleRequests=sessionRequests.length;
  let releaseObservation;
  observationGate={entered:false,promise:new Promise(resolve=>{releaseObservation=resolve;})};
  await until(()=>observationGate.entered,'pause one current-account observation during the switch');
  await popup.evaluate(`document.getElementById('token').value=${JSON.stringify(fakeToken)};document.getElementById('modeSwitch').click()`);
  await pause(250);
  assert.equal(await popup.evaluate(`document.getElementById('executionError').hidden`),true,'a short observation sync must not fail the execute switch');
  releaseObservation();observationGate=null;
  await until(async()=>{const state=await call({type:'control-status',tabId});return state.data?.mode==='execute' && sessionRequests.length>idleRequests+3;},'idle execute mode remains enabled while viewing the main account');
  assert.equal(pollRequests.length,idlePolls,'an idle permission cannot poll a trading job');
  assert.equal(await vestPage.evaluate('window.__fixture.selections.length'),idleSelections,'idle mode cannot switch accounts');
  assert.equal(await vestPage.evaluate(`!!document.querySelector('[data-testid="order-form"]')`),false);
  assert.equal(await vestPage.evaluate('window.__fixture.tradeClicks'),0);
  await until(()=>popup.evaluate(`document.getElementById('modeSwitch').checked && !document.getElementById('modeSwitch').disabled`),'idle main-account switch finishes');
  await popup.evaluate(`document.getElementById('modeSwitch').click()`);
  await until(async()=>{const state=await call({type:'control-status',tabId});return state.data?.mode==='readonly';},'idle execute permission is revoked');
  await vestPage.evaluate(`(() => {document.body.append(window.__fixture.savedForm);delete window.__fixture.savedForm;document.querySelector('[data-testid="account-selector-trigger"]').click();document.querySelector('[data-testid="account-item-eval"]').click();})()`);
  await until(()=>vestPage.evaluate(`window.__fixture.selected==='alpha' && document.querySelector('[data-testid="account-value"]').textContent==='$987.50'`),'restore evaluation account after main-account switch regression');
  monitorAccount=true;
  // Real browser mode switching must not require a manually created trading
  // account or a separate identity / acknowledgement form.
  assert.equal(await popup.evaluate(`!!document.getElementById('modeSwitch')`), true, 'the extension exposes the single read-only / execute switch');
  assert.equal(await popup.evaluate(`!!document.getElementById('account') || !!document.getElementById('ack') || !!document.getElementById('capture')`), false, 'the removed binding and acknowledgement flow does not remain in the popup');
  await vestPage.evaluate(`(() => { for (const account of window.__fixture.accounts) { account.equity=account.id==='alpha'?'$1,000.00':'$1,250.00'; account.pnl='$0.00'; } document.querySelector('[data-testid="account-value"]').textContent='$1,000.00'; document.querySelector('[data-testid="account-upnl"]').textContent='$0.00'; })()`);
  await until(() => popup.evaluate(`typeof tab !== 'undefined' && tab?.id === ${tabId} && !document.getElementById('modeSwitch').disabled`), 'popup targets the Vest page and finishes scan feedback');
  await popup.evaluate(`(() => { document.getElementById('token').value=${JSON.stringify(fakeToken)}; const control=document.getElementById('modeSwitch'); if(control.checked || control.disabled) throw new Error('Expected an enabled read-only switch'); control.click(); })()`);
  const enabled = await until(async () => {
    const status = await call({ type: 'control-status', tabId });
    if (status.ok && status.data.mode === 'execute') return status;
    const error = await popup.evaluate(`document.getElementById('status').classList.contains('error') ? document.getElementById('status').textContent : ''`);
    if (error) throw new Error(error);
    return false;
  }, 'native popup switch enables execution', 15000);
  assert.equal(enabled.data.mode, 'execute');
  assert.equal(await popup.evaluate(`chrome.storage.local.get('observation:'+${tabId}).then(result=>result['observation:'+${tabId}].registrations.length)`), 3, 'scanning automatically registers accounts without a manual binding');
  assert.ok(sessionRequests.some(item => item.mode === 'execute' && item.authorize === true), 'only the explicit toggle authorizes execution');
  await until(() => pollRequests.some(item => item.account_id === 'auto-alpha' && item.ready === true), 'automatic account selection, preparation and fresh dispatch poll', 15000);
  preparationPlan={active:true,run_id:'legacy-run',plan_id:'legacy-plan',accounts:[automaticAccounts[1]]};
  const beforeRetired=sessionRequests.length;
  await until(()=>sessionRequests.length>beforeRetired+2,'normal heartbeats ignore retired preparation plans');
  assert.equal((await targets()).some(target=>target.url?.includes('#vest-preparation=')),false,'no preparation window is opened');
  await until(()=>popup.evaluate(`document.getElementById('modeSwitch').checked && !document.getElementById('modeSwitch').disabled`),'execute switch completes its update');
  await popup.evaluate(`document.getElementById('modeSwitch').click()`);
  await until(async()=>{const status=await call({type:'control-status',tabId});return status.data?.mode==='readonly';},'popup revokes execution');
  preparationPlan=null;
  assert.ok([...executionSessions.values()].every(item => item.mode === 'readonly'), 'fake backend sees execution revoked');
  const grants = await popup.evaluate(`chrome.storage.session.get(null)`);
  assert.ok(!Object.values(grants).some(value => value && value.mode === 'execute'), 'no execution grant survives switching to read-only');

  // Revoke through the actual popup while an already delivered order is in the
  // quantity input's asynchronous settling period. The page never makes a real
  // order: all DOM and API traffic in this scenario is local test data.
  await vestPage.command('Runtime.enable');
  await vestPage.command('Runtime.addBinding', { name: 'fixtureQuantityInput' });
  let awaitingInputRevoke = true;
  let signalInputRevoke;
  const inputRevoked = new Promise(resolve => { signalInputRevoke = resolve; });
  vestPage.on('Runtime.bindingCalled', async ({ name }) => {
    if (name !== 'fixtureQuantityInput' || !awaitingInputRevoke) return;
    awaitingInputRevoke = false;
    try {
      await popup.evaluate(`(() => { const control=document.getElementById('modeSwitch'); if(!control.checked || control.disabled) throw new Error('Cannot revoke executing popup switch'); control.click(); })()`);
      signalInputRevoke({ ok: true });
    } catch (error) { signalInputRevoke({ ok: false, error: error.message }); }
  });
  await vestPage.evaluate(`(() => { window.__fixture.inputs=[]; for(const name of ['size-input','tpsl-input-tp','tpsl-input-sl']) { const input=document.querySelector('[data-testid="'+name+'"]'); input.addEventListener('input',()=>{window.__fixture.inputs.push(name);if(name==='size-input')window.fixtureQuantityInput('quantity');}); } })()`);
  queuedOpen = { id: 'fixture-revoked-open', type: 'open', symbol: 'NQ-PERP', side: 'buy', quantity: 2, take_profit: 100, stop_loss: 50 };
  await until(() => popup.evaluate(`!document.getElementById('modeSwitch').checked && !document.getElementById('modeSwitch').disabled`), 'mode switch returns to read-only before in-flight cancellation test');
  await popup.evaluate(`document.getElementById('modeSwitch').click()`);
  const inputRevokeResult = await Promise.race([inputRevoked, pause(15000).then(() => ({ ok: false, error: 'The simulated order did not reach quantity input' }))]);
  assert.equal(inputRevokeResult.ok, true, inputRevokeResult.error+' '+JSON.stringify({results:jobResults,inputs:await vestPage.evaluate('window.__fixture.inputs'),popup:await popup.evaluate(`document.getElementById('status').textContent`)}));
  const cancelled = await until(() => jobResults.find(result => result.job_id === 'fixture-revoked-open'), 'delivered job is reported after switching to read-only', 15000);
  assert.equal(cancelled.success, false);
  assert.equal(cancelled.confirmed, false);
  assert.equal(cancelledJobDeliveries, 1, 'a revoked delivered job must never be repeated');
  const inputEvents = await vestPage.evaluate('window.__fixture.inputs');
  assert.deepEqual(inputEvents, ['size-input'], 'mode revocation stops subsequent TP/SL edits during the pending input wait');
  await until(async () => { const status=await call({ type: 'control-status', tabId }); return status.ok && status.data.mode==='readonly'; }, 'read-only remains effective after the delayed job result');
  assert.ok([...executionSessions.values()].every(item => item.mode === 'readonly'), 'late delivered result must not restore execution');
  queuedOpen={id:'fixture-percentage-open',type:'open',symbol:'NQ-PERP',side:'buy',size_percent:75,take_profit:null,stop_loss:130};
  await until(()=>popup.evaluate(`!document.getElementById('modeSwitch').checked && !document.getElementById('modeSwitch').disabled`),'read-only settles before percentage test');
  await popup.evaluate(`document.getElementById('modeSwitch').click()`);
  const declinedSize=await until(()=>jobResults.find(result=>result.job_id==='fixture-percentage-open'),'percentage sizing refusal is reported before buying',15000);
  assert.equal(declinedSize.success,false);
  assert.equal(sizingRequests.length,1);
  assert.equal(sizingRequests[0].size_percent,75);
  assert.equal(sizingRequests[0].quantity,6,'actual actor reads the page-calculated size instead of treating 75 as quantity');
  assert.equal(await vestPage.evaluate(`document.querySelector('[data-testid="size-unit-select"]').dataset.unit`),'micro','real content actor automatically selects Micro');
  assert.equal(await vestPage.evaluate(`document.querySelector('[data-testid="size-input"]').value`),'3','3 Micro is pinned as 6 canonical Vest units');
  assert.equal(await vestPage.evaluate(`document.querySelector('[data-testid="tpsl-unit-usd"]').getAttribute('aria-selected')`),'true','mousedown-driven USD selection is verified before polling');
  assert.equal(await vestPage.evaluate(`document.querySelector('[data-testid="tpsl-input-tp"]').value`),'','actual extension clears an old TP for a stop-only order');
  assert.equal(await vestPage.evaluate(`document.querySelector('[data-testid="tpsl-input-sl"]').value`),'130');
  assert.deepEqual(await vestPage.evaluate('window.__fixture.pctClicks'),[75]);
  assert.equal(await vestPage.evaluate('window.__fixture.tradeClicks'),0);
  await until(async()=>{const status=await call({type:'control-status',tabId});return status.ok && status.data.mode==='readonly';},'sizing refusal leaves execution disabled');
  await until(()=>inspections.some(item=>item.account_id==='auto-alpha' && item.snapshot.position_open===false),'read-only observation supplies a flat snapshot for explicit manual verification');
  await until(()=>popup.evaluate(`!document.getElementById('executionError').hidden && document.getElementById('executionError').textContent.includes('Fixture declines sizing')`),'execution errors remain visible separately from observation updates');
  await popup.command('Page.reload');
  await until(()=>popup.evaluate(`typeof tab!=='undefined' && tab?.id===${tabId} && !document.getElementById('executionError').hidden && document.getElementById('executionError').textContent.includes('Fixture declines sizing')`),'reopening the popup restores the last execution error');
  const beforePass = uploads.length;
  await vestPage.evaluate(`(() => { const account=window.__fixture.accounts.find(account=>account.id==='alpha'); account.category='passed'; account.badge='Passed'; document.querySelector('[data-testid="account-selector-trigger"]').lastElementChild.textContent='Passed'; })()`);
  await until(() => uploads.slice(beforePass).some(item => item.accounts.some(account => account.name==='QA Alpha' && account.category==='passed' && account.identity==='passed:QA Alpha')), 'native evaluation success is published for automatic strategy locking');
  // A flat passed account remains observable in execute mode even when Vest
  // removes the opening form. This must not stop other accounts in the session.
  await vestPage.evaluate(`document.querySelector('[data-testid="order-form"]').remove()`);
  const passedPollStart = pollRequests.length;
  await until(() => popup.evaluate(`!document.getElementById('modeSwitch').checked && !document.getElementById('modeSwitch').disabled`), 'read-only switch settles after the cancelled job');
  await popup.evaluate(`document.getElementById('modeSwitch').click()`);
  await until(() => pollRequests.slice(passedPollStart).some(item => item.account_id==='auto-alpha' && item.identity==='passed:QA Alpha' && item.ready===false), 'execute mode reconciles a flat passed account without preparing an opening form');
  assert.equal((await call({ type:'control-status',tabId })).data.mode,'execute', 'observing a passed account must not revoke the entire session');
  await until(() => popup.evaluate(`document.getElementById('modeSwitch').checked && !document.getElementById('modeSwitch').disabled`), 'passed-account observation is active');
  await popup.evaluate(`document.getElementById('modeSwitch').click()`);
  const viewing = await vestPage.evaluate('window.__fixture');
  assert.equal(viewing.selected, 'alpha');
  assert.equal(viewing.tradeClicks, 0, 'the connection recovery and read-only scan must never click an order');
  assert.deepEqual(unexpectedRequests, [], 'only fake read-only API routes were requested');
  assert.ok(vestNetworkRequests >= 1, 'the simulated Vest document was locally fulfilled');
  console.log(JSON.stringify({ ok: true, realExtensionVersion: manifest.version, checks: ['actual MV3 service worker', 'actual popup / content messaging', 'native missing receiving-end reproduced', 'automatic script injection recovery without page reload', 'healthy controller reused', 'asynchronous scan acknowledgment', 'real read-only scan upload and account restoration', 'automatic accounts without manual binding', 'single execute / read-only mode switch', 'fresh account dispatch after automatic preparation', 'real popup revocation interrupts an already delivered job', 'revocation stops subsequent input edits and records one uncertain result', 'evaluation success is observed without a manual account rescan', 'no trading clicks', 'no real Vest or user API requests'] }, null, 2));
} catch (error) {
  if (browserCDP) {
    try { console.error('Isolated browser targets:', JSON.stringify((await browserCDP.command('Target.getTargets')).targetInfos.map(({type,url})=>({type,url})))); } catch {}
  }
  throw error;
} finally {
  if (browserCDP) { try { await browserCDP.command('Browser.close', {}, 3000); } catch {} }
  for (const connection of connections) connection.close();
  browserCDP?.close();
  browser?.kill();
  await new Promise((resolve) => api.close(resolve));
}
