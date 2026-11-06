// A real headless Edge smoke test with the Chrome DevTools Protocol.
// Only an isolated local paper database is used; this never logs in or trades at Vest.
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { mkdir, writeFile, readFile } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import { randomUUID } from 'node:crypto';

const root = fileURLToPath(new URL('../', import.meta.url));
const port = 18765;
const base = `http://127.0.0.1:${port}`;
const edge = process.env.EDGE_PATH || ['C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe', 'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe'].find(existsSync);
if (!edge) throw new Error('Set EDGE_PATH to an installed Chromium / Edge executable.');
const run = path.join(root, '.runtime', 'browser-smoke-' + randomUUID());
await mkdir(run, { recursive: true });
const sleep = (ms) => new Promise(resolve => setTimeout(resolve, ms));
async function until(fn, label, timeout = 20000) {
  const end = Date.now() + timeout;
  while (Date.now() < end) { try { const value = await fn(); if (value) return value; } catch {} await sleep(200); }
  throw new Error('Timeout: ' + label);
}
const serverArgs=process.env.VEST_SMOKE_BACKTEST_ONLY==='1'
  ? [path.join(root,'tests','backtest-browser-server.py'),'--port',String(port),'--db',path.join(run,'smoke.sqlite')]
  : ['-m','server.app','--port',String(port),'--db',path.join(run,'smoke.sqlite')];
let server = spawn('python',serverArgs,{cwd:root,windowsHide:true,stdio:'pipe'});
let browser, socket, closeBrowser;
try {
  await until(async () => (await fetch(base + '/api/health')).ok, 'local API');
  browser = spawn(edge, ['--headless=new', '--disable-gpu', '--no-first-run', '--disable-background-networking', '--disable-component-update', '--disable-default-apps', '--disable-sync', '--hide-scrollbars', '--remote-debugging-port=0', '--user-data-dir=' + path.join(run, 'edge'), 'about:blank'], { windowsHide: true, stdio: 'ignore' });
  const debugPort = await until(async () => Number((await readFile(path.join(run, 'edge', 'DevToolsActivePort'), 'utf8')).split('\n')[0]), 'isolated browser debugging port');
  const page = await until(async () => (await (await fetch(`http://127.0.0.1:${debugPort}/json/list`)).json()).find(item => item.type === 'page'), 'headless Edge');
  socket = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => { socket.onopen = resolve; socket.onerror = reject; });
  let seq = 0;
  const pending = new Map();
  const errors = [];
  socket.onmessage = (event) => {
    const data = JSON.parse(event.data);
    if (data.id && pending.has(data.id)) {
      const { resolve, reject, timer } = pending.get(data.id); clearTimeout(timer); pending.delete(data.id);
      data.error ? reject(new Error(JSON.stringify(data.error))) : resolve(data.result);
    }
    if (data.method === 'Runtime.exceptionThrown') errors.push(data.params.exceptionDetails.exception?.description || data.params.exceptionDetails.text);
  };
  const command = (method, params = {}, timeout = 15000) => new Promise((resolve, reject) => {
    const id = ++seq;
    const timer = setTimeout(() => { pending.delete(id); reject(new Error('CDP timeout: ' + method)); }, timeout);
    pending.set(id, { resolve, reject, timer }); socket.send(JSON.stringify({ id, method, params }));
  });
  closeBrowser = () => command('Browser.close', {}, 3000);
  const evaluate = async (expression, timeout = 15000) => {
    const result = await command('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true }, timeout);
    if (result.exceptionDetails) throw new Error(result.exceptionDetails.exception?.description || 'Browser evaluation failed');
    return result.result.value;
  };
  const clickText = async (text, selector = 'button') => {
    const result = await evaluate(`(() => { const e = [...document.querySelectorAll(${JSON.stringify(selector)})].find(e => e.textContent.trim().includes(${JSON.stringify(text)}) && e.getClientRects().length); if(!e || e.disabled) return false; e.click(); return true; })()`);
    assert.ok(result, 'Clickable control: ' + text);
    await sleep(250);
  };
  await command('Runtime.enable');
  await command('Page.enable');
  await command('Emulation.setDeviceMetricsOverride', { width: 1600, height: 1080, deviceScaleFactor: 1, mobile: false });
  const runPhaseFixture=async()=>{
  await command('Page.navigate', { url: base + '/api/health' });
  await until(() => evaluate(`document.readyState === 'complete' && location.pathname === '/api/health'`), 'phase transition fixture page');
  const phaseFixture = await readFile(path.join(root,'tests','phase-transition-fixture.html'),'utf8');
  await evaluate(`document.open(); document.write(${JSON.stringify(phaseFixture.replace(/<script>[\s\S]*?<\/script>/,''))}); document.close();`);
  await evaluate(phaseFixture.match(/<script>([\s\S]*?)<\/script>/)[1]);
  await evaluate(await readFile(path.join(root,'extension','core.js'),'utf8'));
  await evaluate(await readFile(path.join(root,'extension','content.js'),'utf8'));
  const outcome=await evaluate('runPhaseTransitionFixtureTests()',20000);
  assert.equal(outcome.ok,true,JSON.stringify(outcome));
  return outcome;
  };
  const runPhaseExecutionFixture=async()=>{
    await command('Page.navigate', {url:base+'/api/health'});
    await until(()=>evaluate(`document.readyState==='complete' && location.pathname==='/api/health'`),'phase execution fixture page');
    const fixture=await readFile(path.join(root,'tests','phase-execution-fixture.html'),'utf8');
    await evaluate(`document.open();document.write(${JSON.stringify(fixture.replace(/<script>[\s\S]*?<\/script>/,''))});document.close();`);
    await evaluate(fixture.match(/<script>([\s\S]*?)<\/script>/)[1]);
    await evaluate(await readFile(path.join(root,'extension','core.js'),'utf8'));
    await evaluate(await readFile(path.join(root,'extension','content.js'),'utf8'));
    const outcome=await evaluate('runPhaseExecutionFixtureTests()',60000);
    assert.equal(outcome.ok,true,JSON.stringify(outcome));
    return outcome;
  };
  const runPhaseDialogFixture=async()=>{
    await command('Page.navigate',{url:base+'/api/health'});
    await until(()=>evaluate(`document.readyState==='complete' && location.pathname==='/api/health'`),'phase dialog fixture page');
    const fixture=await readFile(path.join(root,'tests','phase-dialog-fixture.html'),'utf8');
    await evaluate(`document.open();document.write(${JSON.stringify(fixture.replace(/<script>[\s\S]*?<\/script>/,''))});document.close();`);
    await evaluate(fixture.match(/<script>([\s\S]*?)<\/script>/)[1]);
    await evaluate(await readFile(path.join(root,'extension','core.js'),'utf8'));
    await evaluate(await readFile(path.join(root,'extension','content.js'),'utf8'));
    const outcome=await evaluate('runPhaseDialogFixtureTests()',60000);
    assert.equal(outcome.ok,true,JSON.stringify(outcome));
    return outcome;
  };
  if(process.env.VEST_SMOKE_BACKTEST_ONLY==='1'){
    const {runBacktestChecks}=await import('../tests/backtest-browser-checks.mjs');
    console.log(JSON.stringify(await runBacktestChecks({base,root,run,command,evaluate,clickText,until,sleep,errors}),null,2));
  } else if(process.env.VEST_SMOKE_STRATEGY_ONLY==='1'){
    const {runStrategyChecks}=await import('../tests/strategy-browser-checks.mjs');
    const restartServer=async()=>{
      server.kill();
      await until(async()=>{try{await fetch(base+'/api/health');return false;}catch{return true;}},'test server stopped');
      server=spawn('python',['-m','server.app','--port',String(port),'--db',path.join(run,'smoke.sqlite')],{cwd:root,windowsHide:true,stdio:'pipe'});
      await until(async()=> (await fetch(base+'/api/health')).ok,'test server restarted');
    };
    console.log(JSON.stringify(await runStrategyChecks({base,root,run,command,evaluate,clickText,until,sleep,restartServer}),null,2));
  } else if(process.env.VEST_SMOKE_PHASE_DIALOG_ONLY==='1'){
    console.log(JSON.stringify(await runPhaseDialogFixture()));
  } else if(process.env.VEST_SMOKE_PHASE_ONLY==='1'){
    console.log(JSON.stringify({phaseTransition:await runPhaseFixture(),phaseExecution:await runPhaseExecutionFixture(),phaseDialog:await runPhaseDialogFixture()}));
  } else {
  await command('Page.navigate', { url: base });
  await until(() => evaluate(`document.querySelector('h1')?.textContent.includes('交易工作台') && document.querySelectorAll('tbody tr').length >= 8`), 'dashboard + accounts');
  await sleep(1000);
  assert.ok(await evaluate(`document.querySelectorAll('canvas').length > 0`), 'K-line chart renders');
  const chartHeight = await evaluate(`document.querySelector('.market-panel').getBoundingClientRect().height`);
  assert.ok(chartHeight > 300 && chartHeight <= 600, 'chart height remains bounded instead of growing on resize');
  const dashboard = await command('Page.captureScreenshot', { format: 'png', captureBeyondViewport: false });
  await writeFile(path.join(run, 'dashboard.png'), Buffer.from(dashboard.data, 'base64'));
  // Exercise all major pages and modal interactions, with React's real event path.
  for (const title of ['账户管理', '盈亏分析', '自动策略', '浏览器连接']) {
    await clickText(title, 'aside button');
    await until(() => evaluate(`document.querySelector('h1')?.textContent.includes(${JSON.stringify(title)})`), title);
  }
  await clickText('账户管理', 'aside button');
  await clickText('添加账户');
  assert.ok(await evaluate(`!!document.querySelector('[role="dialog"]')`), 'add account modal');
  await evaluate(`(() => { const e=document.querySelector('[role="dialog"] input'); Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(e,'QA 模拟账户'); e.dispatchEvent(new Event('input',{bubbles:true})); })()`);
  await clickText('创建账户');
  await until(async () => (await (await fetch(base + '/api/state')).json()).accounts.some(a => a.name === 'QA 模拟账户'), 'create account via UI');
  await clickText('交易工作台', 'aside button');
  // Submit the visible bulk paper order form, then verify persisted positions.
  await clickText('模拟买入 / 做多');
  await until(async () => (await (await fetch(base + '/api/state')).json()).positions.length > 0, 'bulk order via UI');
  const state = await (await fetch(base + '/api/state')).json();
  assert.equal(state.positions.length, 8, 'all initially selected accounts received one paper position');
  await fetch(base + '/api/positions/close-all', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
  const after = await (await fetch(base + '/api/state')).json();
  assert.equal(after.positions.length, 0, 'flatten paper positions');
  assert.ok(after.history.length >= 120, 'paper fills persist to history');
  // Real-account records are discovered automatically; the plugin mode is the only execution permission.
  await fetch(base + '/api/settings', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ mode: 'browser' }) });
  const pairing = await (await fetch(base + '/api/bridge/config')).json();
  let observationAge = 0;
  const sessionBody = (mode, modeEpoch, authorize = false) => ({
    session_id: 'qa-auto-session', mode, mode_epoch: modeEpoch, authorize,
    tab_url: 'https://next.vestmarkets.com/trade/NQ-PERP', accounts: [
      { capital_metrics: Object.fromEntries([['daily_loss',12.5,130],['drawdown',20,500],['goal',60,300]].map(([key,current,limit]) => [key,{text:`$${current} / $${limit}`,current,limit,percent:null,observed_at:Date.now()/1000-observationAge}])), name: 'QA Observed Account', category: 'eval', identity: 'eval:QA Observed Account', equity: 987.5, balance: 1000, unrealized_pnl: -12.5, current: true, observed_at: Date.now() / 1000 - observationAge },
      { name: 'QA Unknown Account', category: 'funded', identity: 'funded:QA Unknown Account', equity: null, balance: null, unrealized_pnl: null, current: false, observed_at: null },
    ],
  });
  const updatePluginMode = async (mode, modeEpoch, authorize = false) => {
    const response = await fetch(base + '/api/bridge/session', {
      method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Bridge-Token': pairing.token },
      body: JSON.stringify(sessionBody(mode, modeEpoch, authorize)),
    });
    const result = await response.json();
    assert.equal(response.status, 200, 'plugin session update: ' + JSON.stringify(result));
    return result;
  };
  observationAge = 31;
  const readonlySession = await updatePluginMode('readonly', 0);
  assert.equal(readonlySession.mode, 'readonly');
  assert.equal(readonlySession.accounts.length, 2, 'both page accounts appear without manually creating any browser account');
  const anotherBrowser={...sessionBody('readonly',0),session_id:'qa-other-browser-session'};
  const duplicateRead=await fetch(base+'/api/bridge/session',{method:'POST',headers:{'Content-Type':'application/json','X-Bridge-Token':pairing.token},body:JSON.stringify(anotherBrowser)});
  assert.equal(duplicateRead.status,200);
  const duplicateSession=await duplicateRead.json();
  assert.deepEqual(duplicateSession.accounts.map(account=>account.id).sort(),readonlySession.accounts.map(account=>account.id).sort(),'same names from a different browser reuse the same account IDs');

  assert.ok(readonlySession.accounts.every(account => account.auto_discovered && !account.executable), 'read-only discovery never grants execution');
  await until(() => evaluate(`document.querySelector('#vest-page-accounts')?.textContent.includes('QA Observed Account') && document.querySelector('#vest-page-accounts')?.textContent.includes('1,000.00')`), 'auto-discovered account table shows observed balance');
  assert.equal(await evaluate(`document.querySelectorAll('#vest-page-accounts table').length`), 1, 'one unified table rather than separate observation and trade binding tables');
  assert.equal(await evaluate(`document.querySelectorAll('#vest-page-accounts tbody tr').length`),2,'re-reading from another browser displays each account name once');
  assert.equal(await evaluate(`document.querySelector('[aria-label="选择 QA Observed Account"]').disabled`), true, 'read-only discovered account cannot be selected for execution');
  assert.equal(await evaluate(`document.querySelector('.order-panel button[type="submit"]').disabled`), true, 'read-only mode disables the real order form');
  assert.equal(await evaluate(`document.body.textContent.includes('交易绑定') || [...document.querySelectorAll('.heading-actions button')].some(button => button.textContent.includes('添加账户'))`), false, 'browser flow contains no trade binding or manual add-account step');
  assert.equal(await evaluate(`(() => { const row=[...document.querySelectorAll('#vest-page-accounts tbody tr')].find(row=>row.textContent.includes('QA Unknown Account')); return row && [...row.querySelectorAll('td.numeric:not(.daily-realized-pnl):not(.capital-metric-cell):not(.phase-realized-pnl)')].length === 3 && [...row.querySelectorAll('td.numeric:not(.daily-realized-pnl):not(.capital-metric-cell):not(.phase-realized-pnl)')].every(cell=>cell.textContent.trim()==='—'); })()`), true, 'unknown account financial fields stay unknown');
  await until(()=>evaluate(`[...document.querySelectorAll('#vest-page-accounts tbody tr')].some(row=>row.textContent.includes('QA Observed Account') && row.textContent.includes('1,000.00') && row.textContent.includes('待更新'))`),'expired account keeps its last amounts with a pending-update marker');
  assert.equal(await evaluate(`document.querySelectorAll('#vest-page-accounts th.daily-pnl-heading').length`),1,'account table includes daily realized PnL with the trading-day cutoff');
  await until(()=>evaluate(`(() => { const row=document.querySelector('[aria-label="选择 QA Observed Account"]')?.closest('tr');return row && row.querySelectorAll('.capital-metric-cell').length===3 && row.textContent.includes('12.50 / 130.00') && row.textContent.includes('20.00 / 500.00') && row.textContent.includes('60.00 / 300.00') && [...row.querySelectorAll('.capital-metric-cell')].every(cell=>cell.textContent.includes('待更新'));})()`),'three capital metrics retain stale values with pending labels');
  observationAge = 0;
  const executingSession = await updatePluginMode('execute', 1, true);
  assert.equal(executingSession.mode, 'execute');
  await until(()=>evaluate(`[...document.querySelectorAll('#vest-page-accounts th')].filter(th=>['最大日亏损','最大回撤','阶段目标'].some(label=>th.textContent.includes(label))).length===3 && [...document.querySelector('[aria-label="选择 QA Observed Account"]').closest('tr').querySelectorAll('.capital-metric-cell')].every(cell=>!cell.textContent.includes('待更新'))`),'new capital readings clear individual pending markers');
  assert.equal(executingSession.accounts.find(account => account.name === 'QA Observed Account').executable, true);
  assert.equal(executingSession.accounts.find(account => account.name === 'QA Unknown Account').executable, false, 'an unread account remains unavailable in execution mode');
  await until(() => evaluate(`document.querySelector('[aria-label="选择 QA Observed Account"]')?.disabled === false`), 'plugin execute mode enables the automatically discovered account');
  await clickText('账户管理', 'aside button');
  await until(() => evaluate(`document.querySelector('h1')?.textContent.includes('账户管理') && document.querySelector('[aria-label="选择 QA Observed Account"]')?.disabled === false`), 'account management uses the same executable automatic accounts');
  assert.equal(await evaluate(`document.querySelectorAll('#vest-page-accounts table').length`), 1, 'account management also contains only one account table');
  assert.equal(await evaluate(`!!document.querySelector('.heading-actions button')?.textContent.includes('添加')`), false, 'real account management has no manual create-account action');
  const browserAccountsScreenshot = await command('Page.captureScreenshot', { format: 'png', captureBeyondViewport: false });
  await writeFile(path.join(run, 'browser-accounts.png'), Buffer.from(browserAccountsScreenshot.data, 'base64'));
  await clickText('交易工作台', 'aside button');
  await until(() => evaluate(`document.querySelector('.order-panel button[type="submit"]') && document.querySelector('[aria-label="选择 QA Observed Account"]')?.disabled === false`), 'return to executable browser order form');
  await evaluate(`(() => { const checkbox=document.querySelector('[aria-label="选择 QA Observed Account"]'); if(!checkbox.checked) checkbox.click(); })()`);
  await until(() => evaluate(`document.querySelector('.order-panel button[type="submit"]')?.disabled === false`), 'selected automatic account enables order submission without another setup step');
  assert.equal(await evaluate(`document.querySelectorAll('[role="dialog"]').length`), 0, 'permission switch and selection require no second authorization dialog');
  assert.equal(await evaluate(`document.querySelector('[aria-label="每账户止损金额"]').value`),'130','manual order default stop loss is 130');
  assert.equal(await evaluate(`document.querySelector('[aria-label="每账户止盈金额"]')`),null,'fixed take profit input is removed');
  await evaluate(`document.querySelector('[aria-label="锁定策略 QA Observed Account"]').click()`);
  await until(async () => (await (await fetch(base+'/api/state')).json()).accounts.some(account => account.name==='QA Observed Account' && account.strategy_locked), 'strategy lock is persisted from the account table');
  await until(() => evaluate(`document.querySelector('[aria-label="解除策略锁定 QA Observed Account"]') && document.querySelector('.order-panel button[type="submit"]')?.disabled === false`), 'a strategy-only lock does not disable manual trading after the update completes');
  assert.equal(await evaluate(`[...document.querySelectorAll('#vest-page-accounts tbody tr')].some(row=>row.textContent.includes('QA Observed Account') && row.textContent.includes('自动解锁'))`),true,'account table shows the next daily unlock time');
  await clickText('自动策略', 'aside button');
  await until(() => evaluate(`[...document.querySelectorAll('.account-pool label')].some(label => label.textContent.includes('QA Observed Account') && label.querySelector('input').disabled && label.textContent.includes('手动排除自动策略'))`), 'locked account is excluded from the strategy pool');
  assert.equal(await evaluate(`document.querySelector('#strategy-form button[type="submit"]').disabled`), true, 'a locked selection cannot start the strategy');
  const lockedPool = await command('Page.captureScreenshot', { format: 'png', captureBeyondViewport: false });
  await writeFile(path.join(run, 'strategy-locked.png'), Buffer.from(lockedPool.data, 'base64'));
  await evaluate(`document.querySelector('[aria-label="解除策略锁定 QA Observed Account"]').click()`);
  await until(async () => (await (await fetch(base+'/api/state')).json()).accounts.some(account => account.name==='QA Observed Account' && !account.strategy_locked), 'explicit unlock permits selecting the account again');
  await until(() => evaluate(`[...document.querySelectorAll('.account-pool label')].some(label => label.textContent.includes('QA Observed Account') && !label.querySelector('input').disabled)`), 'auto-discovered execution account is available in strategy pool');
  await evaluate(`(() => { const input=document.querySelector('[aria-label="每批账户数量"]'); Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(input,'1'); input.dispatchEvent(new Event('input',{bubbles:true})); })()`);
  await until(() => evaluate(`document.querySelector('#strategy-form button[type="submit"]')?.disabled === false`), 'execution mode permits starting a configured strategy without a binding wizard');
  assert.equal(await evaluate(`document.querySelectorAll('.position-size-options button').length`),4,'strategy offers only the four Vest position-size presets');
  await evaluate(`document.querySelector('[aria-label="仓位大小 75%"]').click()`);
  assert.equal(await evaluate(`document.querySelector('[aria-label="仓位大小 75%"]').getAttribute('aria-pressed')`),'true','position size selection updates');
  assert.equal(await evaluate(`document.querySelector('#strategy-form')?.textContent.includes('每账户合约数量')`),false,'strategy quantity has been renamed to position size');
  // Do not click a real-order or strategy-submit button. This isolated test verifies permission and UI only.
  await updatePluginMode('readonly', 2);
  await until(() => evaluate(`document.querySelector('#strategy-form button[type="submit"]')?.disabled === true && [...document.querySelectorAll('.account-pool label')].some(label => label.textContent.includes('QA Observed Account') && label.querySelector('input').disabled)`), 'switching plugin back to read-only revokes strategy and pool execution');
  await clickText('交易工作台', 'aside button');
  await until(() => evaluate(`document.querySelector('.order-panel button[type="submit"]')?.disabled === true && document.querySelector('[aria-label="选择 QA Observed Account"]')?.disabled === true`), 'read-only mode revokes manual order execution too');
  const modeState = await (await fetch(base + '/api/state')).json();
  assert.equal(modeState.accounts.filter(account => account.mode === 'browser' && account.auto_discovered).length, 2);
  assert.equal(modeState.positions.filter(position => position.mode === 'browser').length, 0, 'mode changes and selection never create real positions');
  assert.equal(modeState.bridge.pending_jobs, 0, 'mode changes never submit a trading task');
  const retiredPreparation=await fetch(base+'/api/bridge/preparation',{method:'POST',headers:{'Content-Type':'application/json','X-Bridge-Token':pairing.token},body:JSON.stringify({})});
  assert.equal(retiredPreparation.status,200);
  assert.equal((await retiredPreparation.json()).active,false,'retired preparation API never activates a window');
  await clickText('自动策略','aside button');
  assert.equal(await evaluate(`document.querySelector('.preparation-panel')`),null,'preparation panel is removed');
  await updatePluginMode('readonly',4);
  await clickText('交易工作台','aside button');
  const mainBody=sessionBody('execute',5,true);
  mainBody.accounts.push({name:'主账户',category:'default',current:false,balance:4321,equity:4321,unrealized_pnl:0,observed_at:Date.now()/1000});
  const mainResponse=await fetch(base+'/api/bridge/session',{method:'POST',headers:{'Content-Type':'application/json','X-Bridge-Token':pairing.token},body:JSON.stringify(mainBody)});
  assert.equal(mainResponse.status,200);
  const mainSession=await mainResponse.json();
  assert.equal(mainSession.accounts.find(account=>account.name==='主账户').executable,false);
  assert.notEqual(mainSession.next_account?.name,'主账户');
  await until(()=>evaluate(`(() => {const row=document.querySelector('[aria-label="选择 主账户"]')?.closest('tr');return row && row.querySelector('input').disabled && row.textContent.includes('4,321.00') && row.textContent.includes('永久排除') && row.textContent.includes('不自动解锁') && !row.querySelector('[aria-label="解除策略锁定 主账户"]');})()`),'main account keeps amounts visible but has no selection or unlock action');
  await clickText('自动策略','aside button');
  await until(()=>evaluate(`[...document.querySelectorAll('.account-pool label')].some(label=>label.textContent.includes('主账户') && label.querySelector('input').disabled && label.textContent.includes('永久排除'))`),'main account cannot be selected in the strategy pool');
  await updatePluginMode('readonly',6);
  await clickText('交易工作台','aside button');
  // Administrative failure confirmation uses the local frontend origin only;
  // re-reading stale eval metadata must never re-enable the retired account.
  const failedBody=sessionBody('readonly',7);
  failedBody.accounts[0].category='failed';
  const failedRegistration=await fetch(base+'/api/bridge/session',{method:'POST',headers:{'Content-Type':'application/json','X-Bridge-Token':pairing.token},body:JSON.stringify(failedBody)});
  assert.equal(failedRegistration.status,200);
  const failedAccount=(await failedRegistration.json()).accounts.find(account=>account.name==='QA Observed Account');
  const disposition={account_id:failedAccount.id,acknowledge:true,terminal_status:'failed'};
  const deniedDisposition=await fetch(base+'/api/bridge/reconcile',{method:'POST',headers:{'Content-Type':'application/json','X-Bridge-Token':pairing.token,'Origin':'chrome-extension://'+'a'.repeat(32)},body:JSON.stringify(disposition)});
  assert.equal(deniedDisposition.status,403,'an extension cannot confirm a terminal failure');
  const confirmedFailure=await fetch(base+'/api/bridge/reconcile',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(disposition)});
  assert.equal(confirmedFailure.status,200);
  await updatePluginMode('execute',8);
  await until(()=>evaluate(`!document.querySelector('[aria-label="选择 QA Observed Account"]')`),'automatically archived account is hidden by default');
  await evaluate(`document.querySelector('[aria-label="显示已归档账户"]').click()`);
  await until(()=>evaluate(`(() => { const row=document.querySelector('[aria-label="选择 QA Observed Account"]')?.closest('tr'); return row && row.querySelector('input').disabled && row.textContent.includes('评估失败停用') && row.textContent.includes('已归档 · 永久停用') && !row.textContent.includes('人工核对') && !row.textContent.includes('自动解锁'); })()`),'confirmed failed account stays disabled without reconcile or daily-unlock controls');
  await clickText('自动策略','aside button');
  await until(()=>evaluate(`[...document.querySelectorAll('.account-pool label')].every(label=>!label.textContent.includes('QA Observed Account'))`),'confirmed failed account cannot join another strategy');
  await clickText('账户管理','aside button');
  await until(()=>evaluate(`!!document.querySelector('[aria-label="自动归档失败账户"]')`),'archive preferences visible in account management');
  await evaluate(`document.querySelector('[aria-label="自动归档失败账户"]').click()`);
  await until(async()=> (await (await fetch(base+'/api/state')).json()).preferences.auto_archive_failed===false,'auto archive can be disabled via UI');
  const manualBody=sessionBody('execute',9);
  manualBody.accounts[1].category='failed';manualBody.accounts[1].status_observed_at=Date.now()/1000;
  assert.equal((await fetch(base+'/api/bridge/session',{method:'POST',headers:{'Content-Type':'application/json','X-Bridge-Token':pairing.token},body:JSON.stringify(manualBody)})).status,200);
  await until(()=>evaluate(`!!document.querySelector('[aria-label="归档 QA Unknown Account"]')`),'failed accounts offer manual archive when auto archive is off');
  await evaluate(`document.querySelector('[aria-label="归档 QA Unknown Account"]').click()`);
  await clickText('确认执行');
  await until(async()=> (await (await fetch(base+'/api/state')).json()).accounts.some(a=>a.name==='QA Unknown Account'&&a.archived&&a.archive_source==='user'),'manual archive persists without deleting account records');
  await evaluate(`document.querySelector('[aria-label="自动归档失败账户"]').click()`);
  await until(async()=> (await (await fetch(base+'/api/state')).json()).preferences.auto_archive_failed===true,'auto archive setting persists');
  await updatePluginMode('readonly',10);
  await clickText('交易工作台','aside button');
  const closedBody=sessionBody('readonly',11);
  closedBody.accounts.push({name:'QA Closed Account',category:'closed',current:false,balance:4698.49,equity:4698.49,unrealized_pnl:0,observed_at:Date.now()/1000,status_observed_at:Date.now()/1000});
  const closedRegistration=await fetch(base+'/api/bridge/session',{method:'POST',headers:{'Content-Type':'application/json','X-Bridge-Token':pairing.token},body:JSON.stringify(closedBody)});
  assert.equal(closedRegistration.status,200);
  const closedAccount=(await closedRegistration.json()).accounts.find(a=>a.name==='QA Closed Account');
  assert.equal(closedAccount.category,'closed');assert.equal(closedAccount.archived,true);
  await until(()=>evaluate(`(() => {const row=document.querySelector('[aria-label="选择 QA Closed Account"]')?.closest('tr');return row && row.querySelector('input').disabled && row.textContent.includes('已关闭停用') && !row.textContent.includes('人工核对');})()`),'native closed accounts are archived without a manual verification control');
  // These jobs exist only in the isolated local test DB. No Vest page receives
  // them. Unknown receipts block the UI; closure must clear that block.
  const recoveryBody=(mode,epoch)=>({...sessionBody(mode,epoch,mode==='execute'),accounts:[
    {name:'QA Archived Run',category:'eval',current:true,balance:5000,equity:5000,unrealized_pnl:0,observed_at:Date.now()/1000},
    {name:'QA Continue',category:'eval',current:false,balance:5000,equity:5000,unrealized_pnl:0,observed_at:Date.now()/1000},
    {name:'QA Manual Archive',category:'eval',current:false,balance:5000,equity:5000,unrealized_pnl:0,observed_at:Date.now()/1000}]});
  const recoveryHeaders={'Content-Type':'application/json','X-Bridge-Token':pairing.token};
  const recoveryRegistration=await fetch(base+'/api/bridge/session',{method:'POST',headers:recoveryHeaders,body:JSON.stringify(recoveryBody('execute',12))});
  assert.equal(recoveryRegistration.status,200);
  const recoveryAccount=(await recoveryRegistration.json()).accounts.find(a=>a.name==='QA Archived Run');
  const fakeStrategy=await fetch(base+'/api/strategy/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({account_ids:[recoveryAccount.id],batch_size:1,quantity:4,side:'buy',stop_loss:130,order_delay_min:0,order_delay_max:0,batch_delay_min:3,batch_delay_max:3})});
  assert.equal(fakeStrategy.status,200);
  await until(async()=> (await (await fetch(base+'/api/state')).json()).bridge.jobs.some(j=>j.account_id===recoveryAccount.id&&j.status==='queued'),'isolated strategy job queued');
  await until(()=>evaluate(`(() => {const button=document.querySelector('[aria-label="归档 QA Manual Archive"]');return button && !button.disabled && !!button.closest('.observed-account-name');})()`),'healthy accounts expose manual archive beside the name during a running strategy');
  await evaluate(`document.querySelector('[aria-label="归档 QA Manual Archive"]').click()`);
  await clickText('确认执行');
  await until(async()=> (await (await fetch(base+'/api/state')).json()).accounts.some(a=>a.name==='QA Manual Archive'&&a.archived),'manual archive is saved from the account-name action');
  const manualArchiveState=await (await fetch(base+'/api/state')).json();
  const manuallyArchived=manualArchiveState.accounts.find(a=>a.name==='QA Manual Archive');
  assert.equal(manuallyArchived.category,'eval');assert.equal(!!manuallyArchived.retired_failed,false);
  assert.equal(manuallyArchived.archive_kind,'manual');assert.equal(manuallyArchived.executable,false);
  assert.equal(manualArchiveState.strategy.status,'running','manual archive must not pause an unrelated running strategy');
  const fakeSnapshot=()=>({balance:5000,equity:5000,unrealized_pnl:0,price:100,symbol:'NQ-PERP',position_open:false,observed_at:Date.now()/1000});
  const fakePoll=await fetch(base+'/api/bridge/poll',{method:'POST',headers:recoveryHeaders,body:JSON.stringify({account_id:recoveryAccount.id,session_id:'qa-auto-session',mode_epoch:12,identity:recoveryAccount.identity,ready:true,tab_url:'https://next.vestmarkets.com/trade/NQ-PERP',snapshot:fakeSnapshot()})});
  assert.equal(fakePoll.status,200);
  const fakeJob=(await fakePoll.json()).job;assert.ok(fakeJob);
  assert.equal((await fetch(base+'/api/bridge/result',{method:'POST',headers:recoveryHeaders,body:JSON.stringify({job_id:fakeJob.id,account_id:recoveryAccount.id,session_id:'qa-auto-session',success:false,confirmed:false,error:'Fixture uncertain result',snapshot:fakeSnapshot()})})).status,200);
  await clickText('自动策略','aside button');
  await until(()=>evaluate(`document.querySelector('#strategy-form button[type="submit"]')?.disabled && document.querySelector('#strategy-form button[type="submit"]')?.textContent.includes('待人工核验')`),'an actual unknown receipt still requires verification');
  const recoveredBody=recoveryBody('execute',13);recoveredBody.accounts[0].category='closed';
  assert.equal((await fetch(base+'/api/bridge/session',{method:'POST',headers:recoveryHeaders,body:JSON.stringify(recoveredBody)})).status,200);
  await until(()=>evaluate(`[...document.querySelectorAll('.account-pool label')].some(label=>label.textContent.includes('QA Continue')&&!label.querySelector('input').disabled)`),'remaining account becomes selectable after closure');
  await evaluate(`(() => {const label=[...document.querySelectorAll('.account-pool label')].find(label=>label.textContent.includes('QA Continue'));if(!label.querySelector('input').checked)label.querySelector('input').click();})()`);
  await until(()=>evaluate(`!document.querySelector('#strategy-form button[type="submit"]').disabled && !document.querySelector('#strategy-form button[type="submit"]').textContent.includes('人工核验')`),'archived account with no unresolved jobs cannot leave a false manual verification block');
  assert.equal((await (await fetch(base+'/api/state')).json()).strategy.status,'paused','test does not resume or submit another strategy');
  await fetch(base+'/api/strategy/stop',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
  await fetch(base+'/api/bridge/session',{method:'POST',headers:recoveryHeaders,body:JSON.stringify(recoveryBody('readonly',14))});
  await clickText('交易工作台','aside button');
  // Phase data comes only from isolated fake accounts; no trading job is sent.
  const phaseSession=async phase=>{
    const stamp=Date.now()/1000;
    const body={session_id:'qa-phase-session',mode:'readonly',mode_epoch:0,tab_url:'https://next.vestmarkets.com/trade/NQ-PERP',accounts:[
      {name:'QA Phase Account',category:phase==='live'?'funded':'eval',phase,phase_observed_at:stamp,current:true,
       balance:1000,equity:1000,unrealized_pnl:0,observed_at:stamp,
       capital_metrics:{goal:{text:'$10 / $500',current:10,limit:500,percent:null,observed_at:stamp,phase}}}]};
    const result=await fetch(base+'/api/bridge/session',{method:'POST',headers:recoveryHeaders,body:JSON.stringify(body)});
    assert.equal(result.status,200);return result.json();
  };
  await phaseSession('1/2');
  await until(()=>evaluate(`document.querySelector('[aria-label="查看 QA Phase Account 阶段明细"]')?.textContent==='1/2'`),'explicit phase column');
  await evaluate(`document.querySelector('[aria-label="查看 QA Phase Account 阶段明细"]').click()`);
  await until(()=>evaluate(`document.querySelector('[aria-labelledby="phase-title"] tbody tr')?.textContent.includes('当前阶段')`),'phase ledger modal');
  assert.equal(await evaluate(`document.querySelectorAll('[aria-labelledby="phase-title"] tbody tr').length`),3,'all three phases have their own ledger row');
  await phaseSession('2/2');
  await until(()=>evaluate(`document.querySelectorAll('[aria-labelledby="phase-title"] tbody tr')[1]?.textContent.includes('当前阶段')`),'live updates show current phase');
  assert.ok(await evaluate(`document.querySelectorAll('[aria-labelledby="phase-title"] tbody tr')[0]?.textContent.includes('10.00 / 500.00')`),'previous phase platform metric is retained separately');
  await evaluate(`document.querySelector('[aria-label="关闭阶段明细"]').click()`);
  await phaseSession('live');
  await until(()=>evaluate(`document.querySelector('[aria-label="查看 QA Phase Account 阶段明细"]')?.textContent==='live'`),'funded phase column');
  assert.equal((await (await fetch(base+'/api/state')).json()).strategy.status,'stopped','phase changes do not restart a stopped strategy');
  // Responsive layout must fit the viewport and keep navigation usable.
  await command('Emulation.setDeviceMetricsOverride', { width: 390, height: 844, deviceScaleFactor: 1, mobile: false });
  await sleep(500);
  const metrics = await evaluate(`({body:document.body.scrollWidth, viewport:innerWidth})`);
  assert.ok(metrics.body <= metrics.viewport + 2, `mobile layout overflow (${metrics.body}/${metrics.viewport})`);
  const mobile = await command('Page.captureScreenshot', { format: 'png', captureBeyondViewport: false });
  await writeFile(path.join(run, 'mobile.png'), Buffer.from(mobile.data, 'base64'));
  assert.deepEqual(errors, [], 'no uncaught browser runtime errors');
  // Run the real extension content script against public-markup fixtures.
  // This resets the page context and substitutes a mocked Chrome API, never a real account.
  await command('Page.navigate', { url: base + '/api/health' });
  await until(() => evaluate(`document.readyState === 'complete' && location.pathname === '/api/health'`), 'fixture page');
  const fixture = await readFile(path.join(root, 'tests', 'bridge-fixture.html'), 'utf8');
  const fixtureScript = fixture.match(/<script>([\s\S]*?)<\/script>/)[1];
  const fixtureHtml = fixture.replace(/<script>[\s\S]*?<\/script>/, '');
  await evaluate(`document.open(); document.write(${JSON.stringify(fixtureHtml)}); document.close();`);
  await evaluate(fixtureScript);
  await evaluate(await readFile(path.join(root, 'extension', 'core.js'), 'utf8'));
  await evaluate(await readFile(path.join(root, 'extension', 'content.js'), 'utf8'));
  const bridge = await evaluate('runBridgeFixtureTests()',60000);
  assert.equal(bridge.failed, 0, 'all bridge DOM scenarios: ' + JSON.stringify(bridge));
  assert.equal(bridge.total, 22);
  const stopRule=await evaluate('runStopRuleFixtureTests()',60000);
  assert.equal(stopRule.failed,0,'conditional stop rule: '+JSON.stringify(stopRule));
  await command('Page.navigate', { url: base + '/api/health' });
  await until(() => evaluate(`document.readyState === 'complete' && location.pathname === '/api/health'`), 'account observation fixture page');
  const observerFixture = await readFile(path.join(root, 'tests', 'account-observation-fixture.html'), 'utf8');
  const observerScript = observerFixture.match(/<script>([\s\S]*?)<\/script>/)[1];
  await evaluate(`document.open(); document.write(${JSON.stringify(observerFixture.replace(/<script>[\s\S]*?<\/script>/, ''))}); document.close();`);
  await evaluate(observerScript);
  await evaluate(await readFile(path.join(root, 'extension', 'core.js'), 'utf8'));
  await evaluate(await readFile(path.join(root, 'extension', 'content.js'), 'utf8'));
  const accountObservation = await evaluate('runAccountObservationFixtureTests()', 60000);
  assert.equal(accountObservation.failed, 0, 'all independent account observation scenarios: ' + JSON.stringify(accountObservation));
  assert.equal(accountObservation.total, 14);
  const phaseTransition=await runPhaseFixture();
  const phaseExecution=await runPhaseExecutionFixture();
  const phaseDialog=await runPhaseDialogFixture();
  const failureContinuation=[];
  for(const options of [{label:'Closed'},{label:'已关闭',missingCache:true},{label:'Closed',missingCache:true,idle:true},{live:true},{live:true,pnlLabel:'Unrealized PnL',bannerOnly:true},{pendingOpening:true}]) {
    await command('Page.navigate', { url: base + '/api/health' });
  await until(() => evaluate(`document.readyState === 'complete' && location.pathname === '/api/health'`), 'failure continuation fixture page');
  const failureFixture=await readFile(path.join(root,'tests','failed-account-fixture.html'),'utf8');
  const failureScript=failureFixture.match(/<script>([\s\S]*?)<\/script>/)[1];
  await evaluate(`document.open(); document.write(${JSON.stringify(failureFixture.replace(/<script>[\s\S]*?<\/script>/,''))}); document.close();`);
  await evaluate(`globalThis.failureFixtureOptions=${JSON.stringify(options)}`);
  await evaluate(failureScript);
  await evaluate(await readFile(path.join(root,'extension','core.js'),'utf8'));
  await evaluate(await readFile(path.join(root,'extension','content.js'),'utf8'));
  const outcome=await evaluate('runFailedAccountFixtureTests()',15000);
  assert.equal(outcome.ok,true,JSON.stringify({options,...outcome}));
  failureContinuation.push({options,...outcome});
  }
  console.log(JSON.stringify({ ok: true, checks: ['dashboard', 'candlestick chart', 'bounded chart layout', '5 pages', 'account creation', 'bulk paper order', 'persisted history', 'automatically discovered accounts without trade binding', 'read-only mode prevents execution', 'plugin execute mode enables orders and strategy pool', 'switching back to read-only revokes execution', 'unknown financial values remain unknown', 'responsive viewport', 'no runtime errors'], bridge, stopRule, accountObservation, phaseTransition, phaseExecution, phaseDialog, failureContinuation, screenshots: [path.join(run, 'dashboard.png'), path.join(run, 'browser-accounts.png'), path.join(run, 'mobile.png')] }, null, 2));
  }
} finally {
  try { await closeBrowser?.(); } catch {}
  socket?.close();
  browser?.kill();
  server.kill();
}
