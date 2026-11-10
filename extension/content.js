/* Operates only the public webpage DOM; never reads the website's credentials. */
(() => {
  'use strict';
  const VERSION = '0.3.27';
  const CONTROLLER = '__vestControlBridgeController';
  // A previous injection may belong to a reloaded extension context. Retire it
  // instead of trusting a boolean marker that can outlive its message listener.
  try { globalThis[CONTROLLER]?.dispose?.(); } catch {}
  let disposed = false;
  let timerId = null;
  const runtime = chrome.runtime;
  const core = globalThis.VestBridgeCore;
  let busy = false;
  let config = null;
  let tabKey = null;
  let pickCleanup = null;
  let lastDiagnostic = '';
  let observationEnabled = false;
  let observedAccounts = [];
  let scanRunning = false;
  let lastObservationPoll = 0;
  let executionMode = 'readonly';
  let executionEpoch = 0;
  let executionSession = null;
  let connectionBlocked = false;
  let connectionRevision = 0;
  let sessionAccounts = [];
  let preparingEpoch = null;
  let heartbeatBusy = false;
  let lastModeHeartbeat = 0;
  let bootReady = false;
  let phaseAdvance = null;
  // A dismissed React dialog can retain its old component tree. Keep only weak
  // DOM keys while still suppressing duplicate clicks on a persistent notice.
  let terminalDismissals = new WeakMap();
  let lastCostObservation = 0;
  let profitClaimFlow = null;
  let preparationOnly = location.hash.startsWith('#vest-preparation=');
  let preparationGuardInstalled = false;
  let finishBoot;
  const booted = new Promise(resolve => { finishBoot = resolve; });
  function assertLive() {
    if (disposed) throw new Error('页面连接已更新，旧任务已停止');
    try {
      if (!runtime.id) { dispose(); throw new Error('扩展已重载，请重新连接页面'); }
    } catch (error) { dispose(); throw error; }
  }
  const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const visible = (el) => !!el && el.getClientRects().length > 0 && getComputedStyle(el).visibility !== 'hidden';
  function blockPreparationTrade(event) {
    if (preparationOnly && event.target instanceof Element && event.target.closest('[data-testid="order-form"],[data-testid^="position-close-"],[data-testid="close-dialog"],[data-testid="confirm-order-submit"]')) {
      event.preventDefault(); event.stopImmediatePropagation();
    }
  }
  function protectPreparationWindow() {
    if (preparationGuardInstalled) return;
    preparationGuardInstalled = true;
    for (const event of ['pointerdown','mousedown','click','submit','keydown']) document.addEventListener(event,blockPreparationTrade,true);
  }
  if (preparationOnly) protectPreparationWindow();
  const elementText = (el) => el instanceof HTMLInputElement ? el.value : core.normalized(el?.textContent);
  const unknownSnapshot = () => ({ equity: null, balance: null, unrealized_pnl: null, price: null, entry_price: null, position_open: null, position_id: null, symbol: null, side: null, quantity: null, observed_at: Date.now() / 1000 });
  function assertArmed(job) {
    assertConnection();
    assertExecutableAccount();
    assertLive();
    if (preparationOnly) throw new Error('预备窗口为只读，禁止任何交易操作');
    if (config?.autoDiscovered && (executionMode !== 'execute' || config.mode_epoch !== executionEpoch)) throw new Error('插件已切回只读，停止后续操作');
    if (!config?.armed || !config.enabled || (job && config.accountId !== job.account_id)) throw new Error('执行授权或绑定已变化，停止后续动作');
    if(config.autoDiscovered&&config.phase&&observerIdentity().phase!==config.phase)throw new Error('账户阶段发生变化，禁止执行旧阶段任务');
    if (config.autoDiscovered && job?.type === 'open' && observerIdentity().category === 'passed') throw new Error('账户已通过评估，禁止再次开仓');
  }
  function queryOne(selector, scope = document, required = false) {
    const matches = [...scope.querySelectorAll(selector)].filter(visible);
    if (matches.length > 1) throw new Error('页面控件不唯一: ' + selector);
    if (required && matches.length !== 1) throw new Error('页面控件不可见: ' + selector);
    return matches[0] || null;
  }
  function mainForm(required = false) {
    const forms = [...document.querySelectorAll('[data-testid="order-form"]')]
      .filter((el) => visible(el) && !el.closest('[role="dialog"],[role="alertdialog"]'));
    if (forms.length > 1) throw new Error('存在多个可见主下单表单');
    if (required && forms.length !== 1) throw new Error('主下单表单不可见');
    return forms[0] || null;
  }
  function positionPanel(required = false) {
    const tab = queryOne('[data-testid="account-tab-active-positions"]');
    const panelId = tab?.getAttribute('aria-controls');
    const panel = panelId ? document.getElementById(panelId) : null;
    if (tab && isSelected(tab) && panel && visible(panel)) return panel;
    if (required) throw new Error('请保持活动持仓标签选中并等待数据加载');
    return null;
  }
  async function ensurePositionPanelLayout(guard = () => {}) {
    const pane=document.getElementById('trade-account-tabs');
    const tab=queryOne('[data-testid="account-tab-active-positions"]');
    if (!pane || !tab || !pane.contains(tab) || pane.getBoundingClientRect().height>=200) return;
    const identity=observerIdentity().signature;
    const separator=pane.previousElementSibling;
    const chart=separator?.previousElementSibling;
    if (!separator?.matches('[data-slot="resizable-handle"][role="separator"][aria-orientation="horizontal"]') || chart?.id!=='trade-chart-activity') {
      const error=new Error('持仓面板高度不足，无法定位其展开控件，请展开持仓区域');error.code='OBSERVATION_PENDING';throw error;
    }
    // The native virtual table omits its rows when the pane is nearly collapsed.
    // Resize only the verified chart/account separator, before any order lease.
    for(let step=0;step<8 && pane.getBoundingClientRect().height<200;step++) {
      guard();assertNoDialogs();
      if(observerIdentity().signature!==identity)throw new Error('展开持仓区域时账号或阶段发生变化');
      separator.focus({preventScroll:true});
      separator.dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowUp',code:'ArrowUp',bubbles:true,cancelable:true}));
      separator.dispatchEvent(new KeyboardEvent('keyup',{key:'ArrowUp',code:'ArrowUp',bubbles:true,cancelable:true}));
      await delay(100);
    }
    guard();
    if(pane.getBoundingClientRect().height<200) {
      const error=new Error('持仓表仍未展开，等待可读取持仓行后再继续');error.code='OBSERVATION_PENDING';throw error;
    }
  }
  function assertNoDialogs(allowed = null) {
    const dialogs = [...document.querySelectorAll('[role="dialog"],[role="alertdialog"]')].filter(visible);
    if (dialogs.some((el) => el !== allowed) || (allowed && (!visible(allowed) || dialogs.length !== 1))) {
      throw new Error('页面存在额外确认或风险弹窗；已暂停，请人工处理');
    }
    if (field('submit')) throw new Error('页面存在成本或风险警告；禁止自动代为确认');
  }

  function findAll(selector, key) {
    if (!selector) return [];
    const formFields = ['quantity', 'unit', 'takeProfit', 'stopLoss', 'buy', 'sell'];
    const positionFields = ['positionOpen', 'positionEmpty', 'entryPrice', 'positionSymbol', 'positionSide', 'positionValue', 'close'];
    const scope = formFields.includes(key) ? mainForm() : positionFields.includes(key) ? positionPanel() : document;
    if (!scope) return [];
    if (selector.startsWith('text:')) {
      const values = selector.slice(5).split('||').map(core.normalized);
      return [...scope.querySelectorAll('h1,h2,h3,h4,p,span,div')].filter((el) =>
        values.includes(core.normalized(el.textContent)) &&
        ![...el.children].some((child) => values.includes(core.normalized(child.textContent))) && visible(el));
    }
    return [...scope.querySelectorAll(selector)].filter(visible);
  }
  function field(key, required = false) {
    const matches = findAll(config?.selectors?.[key] || core.defaults[key], key);
    if (matches.length > 1) throw new Error(`控件不唯一: ${key} (${matches.length})`);
    if (required && matches.length !== 1) throw new Error(`控件不可见: ${key}`);
    return matches[0] || null;
  }
  function numeric(key) {
    if (key==='unrealized') {
      const banner=bannerUnrealized();
      if (banner.found) return banner.value;
    }
    const el = field(key); return el ? core.parseMoney(elementText(el)) : null;
  }
  function positionDetails() {
    const symbolEl = field('positionSymbol', true);
    const id = symbolEl.getAttribute('data-testid')?.match(/^position-symbol-(.+)$/)?.[1];
    const row = symbolEl.closest('tr');
    if (!id || !row) throw new Error('需要可识别持仓 ID 的桌面持仓表，不能按品种猜测控件');
    const requireSame = (key, prefix) => {
      const el = field(key, true);
      if (el.getAttribute('data-testid') !== prefix + id || el.closest('tr') !== row) throw new Error('持仓数据不属于同一持仓 ID: ' + key);
      return el;
    };
    const link = symbolEl.closest('a[href]');
    const symbolText = core.normalized(elementText(symbolEl));
    core.canonicalNqPosition(symbolText,link?.getAttribute('href'),location.href);
    const side = core.parseSide(elementText(requireSame('positionSide', 'position-side-')));
    const entry = core.parseMoney(elementText(requireSame('entryPrice', 'position-entry-')));
    const valueEl = requireSame('positionValue', 'position-value-');
    // Public Vest desktop markup: value is in the first p, quantity + market unit in the next p.
    const sizeEl = valueEl.closest('p')?.nextElementSibling;
    if (!sizeEl || sizeEl.tagName !== 'P' || !visible(sizeEl) || sizeEl.closest('tr') !== row) throw new Error('无法定位同持仓的真实数量');
    const sizeText = core.normalized(elementText(sizeEl));
    const quantity = core.parseMoney(sizeText);
    if (!side || !(entry > 0) || !(quantity > 0) || !sizeText.endsWith(' ' + symbolText)) throw new Error('持仓方向、数量或数量单位无法确认');
    const protection = queryOne('[data-testid="td-tpsl"]', row);
    const tp = protection ? queryOne('span.text-long', protection) : null;
    const sl = protection ? queryOne('span.text-short', protection) : null;
    const tpPrice = tp ? core.parseMoney(elementText(tp)) : null;
    const slPrice = sl ? core.parseMoney(elementText(sl)) : null;
    return {
      position_id: id, symbol: 'NQ-PERP', side, quantity, entry_price: entry,
      native_tp_price: tpPrice, native_sl_price: slPrice,
      native_sl: slPrice > 0 && (side === 'buy' ? slPrice < entry : slPrice > entry),
      native_tp: tpPrice > 0 && (side === 'buy' ? tpPrice > entry : tpPrice < entry),
      native_sl_absent: !!protection && (!sl || /^(?:-|—|–)$/.test(core.normalized(elementText(sl)))),
      native_tpsl: tpPrice > 0 && slPrice > 0 && (side === 'buy' ? tpPrice > entry && slPrice < entry : tpPrice < entry && slPrice > entry),
    };
  }
  function snapshot() {
    positionPanel(true);
    const equity = numeric('balance');
    const pnl = numeric('unrealized');
    const isOpen = core.positionState(!!field('positionOpen'), !!field('positionEmpty'));
    const details = isOpen ? positionDetails() : { position_id: null, symbol: 'NQ-PERP', side: null, quantity: null, entry_price: null };
    let phase=null;try{phase=observerIdentity().phase;}catch{}
    return {
      ...(phase ? {phase} : {}),
      equity,
      balance: equity !== null && pnl !== null ? Math.round((equity - pnl) * 100) / 100 : null,
      unrealized_pnl: pnl,
      price: numeric('price'),
      ...details,
      position_open: isOpen,
      observed_at: Date.now() / 1000,
    };
  }
  function assertAccount(opening = true) {
    assertLive();
    assertExecutableAccount();
    if (config?.autoDiscovered && config.phase && observerIdentity().phase!==config.phase) throw new Error('账户阶段发生变化，停止旧阶段操作');
    if (!core.isNqTradePath(location.pathname)) throw new Error('当前标签页不是已校准的 NQ-PERP');
    // Auto-discovery records the phase in its signature. Use the same reader
    // here so an unchanged account with a known phase compares consistently.
    const currentSignature = config?.autoDiscovered ? observerIdentity().signature : core.normalized(elementText(field('identity', true)));
    if (config?.autoDiscovered && currentSignature !== core.normalized(config.identityText)) {
      const identity = observerIdentity();
      if (identity.name === config.accountName && identity.category === 'passed') {
        config.identityText = identity.signature;
        config.accountCategory = 'passed';
        for (const account of observedAccounts.filter(item => item.name === identity.name)) {
          account.category = 'passed'; account.identity = 'passed:' + identity.name;
        }
      }
    }
    if (!config?.identityText || currentSignature !== core.normalized(config.identityText)) {
      throw new Error('账户身份发生变化，已停止本次操作');
    }
    if (opening) {
      const unit = field('unit', true);
      if (unit.getAttribute('data-unit') !== (config.inputUnit || 'token')) throw new Error('数量单位与配置不一致，必须确认 '+(config.inputUnit || 'token'));
      if (config.unitText && core.normalized(elementText(unit)) !== core.normalized(config.unitText)) throw new Error('数量单位标识变化');
    }
    const current = snapshot();
    if ([current.equity, current.balance, current.unrealized_pnl, current.price].some((value) => value === null || !Number.isFinite(value))) throw new Error('净值、浮动盈亏或价格无法读取');
    if (current.position_open === null) throw new Error('持仓状态不明确；请保持活动持仓面板可见');
    if (current.position_open && !(current.entry_price > 0)) throw new Error('无法读取真实成交均价');
    return current;
  }
  function readyCheck() {
    assertNoDialogs();
    const current = assertAccount();
    const form = mainForm(true);
    for (const key of ['quantity', 'takeProfit', 'stopLoss', 'buy', 'sell']) field(key, true);
    if (field('buy') === field('sell')) throw new Error('做多与做空控件不能相同');
    const usd = queryOne('[data-testid="tpsl-unit-usd"]', form, true);
    if (!isSelected(usd)) throw new Error('请将页面止盈止损单位切到 USD，并确认选中状态');
    const limit = queryOne('[data-testid="tpsl-limit-switch"]', form, true);
    if (limit.getAttribute('aria-checked') !== 'false' && limit.dataset.state !== 'unchecked') throw new Error('止盈止损必须确认使用市场触发，请关闭 Limit');
    const market = queryOne('[data-testid="ordertype-tab-market"]', form, true);
    if (!isSelected(market)) throw new Error('必须先切换并校准市价订单');
    return current;
  }
  function isSelected(el) {
    return el.getAttribute('aria-selected') === 'true' || el.getAttribute('aria-pressed') === 'true' ||
      el.getAttribute('data-state') === 'active' || el.getAttribute('data-state') === 'on' || el.getAttribute('data-active') === 'true' ||
      el.getAttribute('data-selected') === 'true';
  }
  function click(el) {
    assertLive();
    if (!visible(el) || el.disabled || el.getAttribute('aria-disabled') === 'true') throw new Error('按钮不可操作，已取消后续动作');
    el.click();
  }
  async function selectTab(selector, scope, guard, dialog = null) {
    guard(); assertNoDialogs(dialog);
    let tab=queryOne(selector,scope(),true);
    if (isSelected(tab)) return;
    // Radix Tabs changes selection on mousedown/keyboard, not click alone.
    tab.focus();
    tab.dispatchEvent(new MouseEvent('mousedown',{bubbles:true,cancelable:true,button:0,buttons:1,ctrlKey:false}));
    tab.dispatchEvent(new MouseEvent('mouseup',{bubbles:true,cancelable:true,button:0,buttons:0,ctrlKey:false}));
    tab.click();
    const deadline=Date.now()+4000;
    while (Date.now()<deadline) {
      await delay(100); guard(); assertNoDialogs(dialog);
      tab=queryOne(selector,scope(),true);
      if (isSelected(tab)) return;
    }
    throw new Error('无法确认页面标签已选中：'+selector+'；未提交订单');
  }
  async function ensureQuantityUnit(wanted, scope, guard, dialog = null) {
    guard(); assertNoDialogs(dialog);
    const selector='[data-testid="size-unit-select"]';
    let unit=queryOne(selector,scope(),true);
    if (unit.dataset.unit===wanted) return;
    unit.focus();
    unit.dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowDown',code:'ArrowDown',bubbles:true,cancelable:true}));
    const optionSelector=`[data-testid="size-unit-option-${wanted}"]`;
    const deadline=Date.now()+4000;
    let chosen=false;
    while (Date.now()<deadline) {
      await delay(100); guard(); assertNoDialogs(dialog);
      unit=queryOne(selector,scope(),true);
      const option=queryOne(optionSelector);
      if (unit.dataset.unit===wanted && !option) return;
      if (option && !chosen) {
        option.focus();
        option.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',code:'Enter',bubbles:true,cancelable:true}));
        if (option.isConnected) option.click();
        chosen=true;
      }
    }
    throw new Error('无法确认数量单位已切换为 '+wanted+'；未提交订单');
  }
  async function setInput(key, value) {
    if (preparationOnly) throw new Error('预备窗口禁止填写交易表单');
    if (config?.autoDiscovered) assertArmed();
    assertNoDialogs();
    const el = field(key, true);
    if (!(el instanceof HTMLInputElement) || el.disabled || el.readOnly) throw new Error('输入控件不可编辑: ' + key);
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
    setter.call(el, String(value));
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
    await delay(150);
    if (value === '') {
      if (field(key,true).value.trim() !== '') throw new Error('无法清空'+(key === 'stopLoss' ? '止损' : '止盈')+'，禁止带入旧金额');
      return;
    }
    const actual = core.parseMoney(field(key, true).value);
    if (actual === null || Math.abs(actual - value) > 0.000001) throw new Error('页面输入值未被接受: ' + key);
  }
  async function prepare(guard = () => {}) {
    if (preparationOnly) throw new Error('预备窗口禁止准备交易表单');
    assertExecutableAccount();
    guard();
    const prepareClick = el => { guard(); assertExecutableAccount(); click(el); };
    assertNoDialogs();
    const form = mainForm(true);
    const protectedGuard=()=>{guard();assertExecutableAccount();};
    await selectTab('[data-testid="ordertype-tab-market"]',()=>mainForm(true),protectedGuard);
    await selectTab('[data-testid="account-tab-active-positions"]',()=>document,protectedGuard);
    assertNoDialogs();
    await ensureQuantityUnit(config.inputUnit || 'token',()=>mainForm(true),protectedGuard);
    if (!field('takeProfit') || !field('stopLoss')) {
      const toggle = queryOne('[data-testid="tpsl-toggle"]', form, true);
      if (toggle.getAttribute('aria-checked') === 'true' || toggle.dataset.state === 'checked') throw new Error('TP/SL 已开启但输入不可见，请人工核对');
      assertNoDialogs(); prepareClick(toggle); await delay(200);
    }
    await selectTab('[data-testid="tpsl-unit-usd"]',()=>mainForm(true),protectedGuard);
    const limit = queryOne('[data-testid="tpsl-limit-switch"]', form, true);
    if (limit.getAttribute('aria-checked') === 'true' || limit.dataset.state === 'checked') { assertNoDialogs(); prepareClick(limit); }
    await delay(300);
    assertNoDialogs();
    guard();
    return snapshot();
  }
  async function prepareAccount(guard = () => {}, opening = true) {
    if (preparationOnly) throw new Error('预备窗口不能启用执行模式');
    assertExecutableAccount();
    guard();
    await ensurePositionPanelLayout(guard);
    if (config?.autoDiscovered && (!opening || config.accountCategory === 'passed')) {
      // Vest may remove the opening form after evaluation success. A flat
      // completed account only needs observation; do not revoke other accounts.
      observerAssertPage();
      const active = queryOne('[data-testid="account-tab-active-positions"]', document, true);
      if (!isSelected(active)) await selectTab('[data-testid="account-tab-active-positions"]',()=>document,guard);
      guard();
      const identity = observerIdentity();
      if (identity.name !== config.accountName || identity.signature !== config.identityText) throw new Error('账户身份发生变化，停止执行准备');
      const current = snapshot();
      if (typeof current.position_open === 'boolean' && [current.equity,current.balance,current.unrealized_pnl,current.price].every(value => value !== null && Number.isFinite(value)) && current.price > 0) return current;
    }
    // Existing positions continue through the normal protected closing path.
    return await prepare(guard);
  }
  async function awaitPosition(job, oldIds, expectedPosition) {
    const until = Date.now() + 12000;
    let filled=null;
    while (Date.now() < until) {
      await delay(350);
      await ensurePositionPanelLayout(() => assertArmed(job));
      const closeDialog = job.type === 'close' ? queryOne('[data-testid="close-dialog"]') : null;
      assertNoDialogs(closeDialog);
      const current = assertAccount(job.type === 'open');
      if (job.type === 'open' && current.position_open) {
        if (oldIds.has(current.position_id) || !core.matchesPosition(current, job)) throw new Error('页面持仓 ID、方向或数量不符合本次订单，禁止认作成交');
        filled=current;
        // The entry request and native protection can be accepted separately. Wait for both visible legs.
        const stopMatches = job.stop_loss == null ? current.native_sl_absent : current.native_sl_price > 0;
        if (stopMatches && (job.take_profit == null ? current.native_tp_price == null : current.native_tp)) return current;
      } else if (job.type === 'close') {
        if (current.position_open && !core.matchesPosition(current, expectedPosition)) throw new Error('等待平仓时持仓 ID、方向或数量变化，停止执行');
        if (!current.position_open && !closeDialog) return current;
      }
    }
    if(job.type==='open'&&filled)return {...filled,stop_check:{status:'failed',error:'成交已确认，但保护状态未及时显示'}};
    throw new Error('12 秒内未观察到明确成交及保护状态；不得自动重试，请手工核对');
  }
  async function calibrateStop(job,filled) {
    if(filled.stop_check?.status==='failed')return filled;
    if(!globalThis.VestStopProtection)return {...filled,stop_check:{status:'failed',error:'止损校验模块未加载，请重载插件'}};
    const corrected=await globalThis.VestStopProtection.correct({
      guard:assertArmed,noDialogs:assertNoDialogs,read:()=>assertAccount(false),
      one:queryOne,click,delay,visible,
      intent:(task,current,price)=>call('stop-protection',{job_id:task.id,account_id:config.accountId,
        session_id:executionSession,mode_epoch:executionEpoch,identity:config.identity,phase:config.phase,
        snapshot:current,target_price:price}),
    },job,filled);
    if(job.take_profit!=null&&!corrected.native_tp)return {...corrected,stop_check:{status:'failed',error:'开仓已成交，但止盈保护未确认'}};
    return corrected;
  }
  async function selectPositionSize(job) {
    assertArmed(job); assertNoDialogs();
    if (assertAccount().position_open !== false) throw new Error('选择仓位大小前账户必须明确空仓');
    const form = mainForm(true);
    const button = queryOne(`[data-testid="size-pct-${job.size_percent}"]`,form,true);
    if (core.normalized(elementText(button)) !== job.size_percent+'%') throw new Error('页面仓位比例按钮文案不匹配');
    click(button);
    const deadline = Date.now()+4000;
    let stable = 0, previous = null;
    while (Date.now()<deadline) {
      await delay(150);
      assertArmed(job); assertNoDialogs();
      if (assertAccount().position_open !== false) throw new Error('选择仓位大小期间持仓变化');
      const slider = queryOne('[role="slider"]',mainForm(true),true);
      const quantity = numeric('quantity');
      if (Number(slider.getAttribute('aria-valuenow')) === job.size_percent && quantity>0) {
        stable = core.sameQuantity(quantity,previous) ? stable+1 : 1;
        previous = quantity;
        if (stable>=3) return core.nqInputToVest(quantity,config.inputUnit || 'token');
      } else { stable=0; previous=null; }
    }
    throw new Error('无法确认页面仓位比例或计算数量，未提交订单');
  }
  async function execute(job) {
    core.validateJob(job, config);
    assertArmed(job);
    const historyKey = 'executed:' + config.accountId;
    const stored = await chrome.storage.local.get(historyKey);
    const history = stored[historyKey] || [];
    if (history.includes(job.id)) throw new Error('任务已领取，禁止重复执行');
    // Persist before any possible click. Unknown outcomes are never retried.
    await chrome.storage.local.set({ [historyKey]: [...history.slice(-499), job.id] });
    assertNoDialogs();
    const before = job.type === 'open' ? readyCheck() : assertAccount(false);
    if (job.type === 'open') {
      if (before.position_open !== false) throw new Error('账户已有持仓，不能重复开仓');
      const oldIds = new Set([...document.querySelectorAll('[data-testid^="position-symbol-"]')]
        .map((el) => el.getAttribute('data-testid').slice('position-symbol-'.length)));
      if (job.size_percent === undefined) await setInput('quantity',core.nqVestToInput(job.quantity,config.inputUnit || 'token'));
      await setInput('takeProfit', job.take_profit == null ? '' : job.take_profit);
      let stopDecision = core.strategyStopLoss(job,job.skip_stop_loss_on_low_drawdown ? observerCapitalMetrics().drawdown : null,config.phase);
      await setInput('stopLoss', stopDecision.stop_loss == null ? '' : stopDecision.stop_loss);
      if (job.size_percent !== undefined) {
        const quantity = await selectPositionSize(job);
        if (job.skip_stop_loss_on_low_drawdown) {
          stopDecision = core.strategyStopLoss(job,observerCapitalMetrics().drawdown,config.phase);
          await setInput('stopLoss',stopDecision.stop_loss == null ? '' : stopDecision.stop_loss);
        }
        const sized = await call('size',{ job_id:job.id,account_id:config.accountId,session_id:executionSession,
          mode_epoch:config.mode_epoch,identity:config.identity,size_percent:job.size_percent,quantity,snapshot:readyCheck(),
          ...(job.skip_stop_loss_on_low_drawdown ? {stop_loss:stopDecision.stop_loss,drawdown:stopDecision.drawdown} : {}) });
        assertArmed(job);
        if (sized.job_id!==job.id || sized.size_percent!==job.size_percent || !core.sameQuantity(sized.quantity,quantity)) throw new Error('本地数量核验不一致，未提交订单');
        job = {...job,quantity};
        if (job.skip_stop_loss_on_low_drawdown) {
          if (!Object.hasOwn(sized,'stop_loss') || sized.stop_loss !== stopDecision.stop_loss) throw new Error('本地止损规则核验不一致，未提交订单');
          job = {...job,stop_loss:sized.stop_loss,stop_loss_omitted:sized.stop_loss_omitted};
        }
      }
      if (readyCheck().position_open !== false) throw new Error('下单前持仓状态变化');
      if (!core.sameQuantity(core.nqInputToVest(numeric('quantity'),config.inputUnit || 'token'),job.quantity)) throw new Error('提交前数量发生变化，未下单');
      if (job.size_percent !== undefined && Number(queryOne('[role="slider"]',mainForm(true),true).getAttribute('aria-valuenow'))!==job.size_percent) throw new Error('提交前仓位比例变化，未下单');
      if ((job.take_profit == null ? field('takeProfit',true).value.trim() !== '' : !core.sameQuantity(numeric('takeProfit'),job.take_profit)) ||
          (job.stop_loss == null ? field('stopLoss',true).value.trim() !== '' : !core.sameQuantity(numeric('stopLoss'),job.stop_loss))) throw new Error('提交前止盈止损金额变化，未下单');
      if (job.skip_stop_loss_on_low_drawdown && core.strategyStopLoss(job,observerCapitalMetrics().drawdown,config.phase).stop_loss !== job.stop_loss) throw new Error('提交前剩余最大回撤条件发生变化，未下单');
      assertNoDialogs();
      assertArmed(job);
      click(field(job.side === 'buy' ? 'buy' : 'sell', true));
      await delay(500);
      // This selector is a cost / risk warning, NOT a routine order confirmation.
      assertNoDialogs();
      return await calibrateStop(job,await awaitPosition(job, oldIds));
    }
    if (before.position_open !== true) throw new Error('持仓状态不匹配，请手工核对');
    const expectedPosition = { position_id: job.browser_position_id || job.position_id, symbol: job.symbol, side: job.side, quantity: job.quantity };
    if (!expectedPosition.position_id || !core.matchesPosition(before, expectedPosition)) throw new Error('平仓任务与当前持仓 ID、方向或数量不匹配');
    const closeButton = field('close', true);
    if (closeButton.getAttribute('data-testid') !== 'position-close-' + expectedPosition.position_id) throw new Error('平仓按钮不属于待关闭的持仓 ID');
    assertNoDialogs();
    assertArmed(job);
    click(closeButton);
    await delay(450);
    const dialog = queryOne('[data-testid="close-dialog"]');
    assertNoDialogs(dialog);
    if (dialog) {
      await ensureQuantityUnit(config.inputUnit || 'token',()=>dialog,()=>assertArmed(job),dialog);
      const sizePct = queryOne('[data-testid="size-pct-100"]', dialog, true);
      click(sizePct);
      await delay(200);
      assertNoDialogs(dialog);
      await selectTab('[data-testid="ordertype-tab-market"]',()=>dialog,()=>assertArmed(job),dialog);
      const slider = queryOne('[role="slider"]', dialog, true);
      if (slider.getAttribute('aria-valuenow') !== '100') throw new Error('无法确认 100% 全量平仓比例');
      if (!core.matchesPosition(assertAccount(false), expectedPosition)) throw new Error('提交平仓前持仓已变化');
      const confirm = queryOne(config.selectors.closeConfirm, dialog, true);
      const size = core.parseMoney(elementText(confirm));
      if (!core.sameQuantity(size, expectedPosition.quantity)) throw new Error('平仓确认按钮的最终数量与预期全量持仓不符');
      const dialogUnit = queryOne('[data-testid="size-unit-select"]', dialog, true);
      if (dialogUnit.dataset.unit !== (config.inputUnit || 'token')) throw new Error('平仓弹窗数量单位不匹配');
      assertNoDialogs(dialog);
      assertArmed(job);
      click(confirm);
    }
    return await awaitPosition(job, null, expectedPosition);
  }

  async function call(type, data = {}) {
    assertLive();
    let result;
    try { result = await runtime.sendMessage({ type, ...data }); }
    catch (error) {
      if (/context invalidated|Extension context|Receiving end/i.test(error.message)) dispose();
      throw error;
    }
    assertLive(); // A retired actor must not refill caches after an awaited IPC.
    if (!result?.ok) throw Object.assign(new Error(result?.error || '本地桥接无响应'), {code:result?.code});
    return result.data;
  }
  async function tick() {
    if (disposed || busy || !config?.enabled) return;
    busy = true;
    try {
      let current;
      let ready = false;
      try { current = readyCheck(); ready = !!config.armed; lastDiagnostic = ready ? '已就绪' : '观测模式'; }
      catch (error) {
        lastDiagnostic = error.message;
        current = unknownSnapshot();
      }
      const response = await call('poll', { snapshot: current, ready, diagnostic: lastDiagnostic });
      if (response.job) {
        let result;
        try {
          const observed = await execute(response.job);
          result = { job_id: response.job.id, success: true, confirmed: true, snapshot: observed };
        } catch (error) {
          lastDiagnostic = error.message;
          config.armed = false;
          await chrome.storage.local.set({ [tabKey]: config });
          let observed;
          try { observed = assertAccount(); } catch { observed = unknownSnapshot(); }
          result = { job_id: response.job.id, success: false, confirmed: false, error: error.message, snapshot: observed };
        }
        await call('result', { result });
      }
    } catch (error) { lastDiagnostic = error.message; }
    finally { busy = false; }
  }

  // Account observation is independent from the trading adapter. It never prepares
  // order inputs, reads credentials or creates a trade binding.
  function observerAssertPage(allowTerminalNotice = false) {
    assertLive();
    if (!core.isNqTradePath(location.pathname)) throw new Error('请打开已登录的 Vest NQ-PERP 交易页面');
    if ([...document.querySelectorAll('[role="dialog"],[role="alertdialog"]')].some(visible) &&
        !(allowTerminalNotice && (terminalAccountDialog() || liveActivationDialog()))) throw new Error('页面存在弹窗，请先关闭后再读取账户');
  }
  function observerTrigger() {
    return queryOne('[data-testid="account-selector-trigger"]', document, true);
  }
  function observerIdentity() {
    const trigger = observerTrigger();
    const names = [...trigger.querySelectorAll('span.truncate')].filter(visible);
    if (names.length !== 1) throw new Error('无法唯一读取当前账户名称，请使用桌面布局');
    const name = core.normalized(names[0].textContent);
    if (!name) throw new Error('账户名称为空，请确认已登录');
    const labels = [...trigger.querySelectorAll('span')].filter(el => visible(el) && el !== names[0] && !el.contains(names[0])).map(el => core.normalized(el.textContent));
    // Vest replaces the 2/2 badge with "Evaluation Passed" while its native
    // result modal is open. The exact stage Profit Target label in the same
    // account banner remains explicit phase evidence; the modal text alone is
    // insufficient. Conflicting badge/goal phases still resolve to unknown.
    const phaseLabels=[...document.querySelectorAll('div.sticky.top-14 [data-slot="badge"],div.sticky.top-14 p')]
      .filter(el=>visible(el)&&!el.closest('[role="dialog"],[role="menu"]')).map(el=>core.normalized(el.textContent));
    const phase=core.accountPhase([...labels,...phaseLabels]);
    return { name, signature: core.normalized(trigger.textContent)+(phase?'|'+phase:''), phase,
      category: core.accountCategory(labels) || (phase==='live'?'funded':phase?'eval':null) };
  }
  function assertExecutableAccount() {
    core.assertExecutionAllowed({ name: config?.accountName || config?.identityText,
      category: config?.accountCategory, execution_excluded: config?.execution_excluded });
    // Check the actual page too: a stale config must never authorize the main account.
    const trigger = observerTrigger();
    const names = [...trigger.querySelectorAll('span.truncate')].filter(visible);
    const name = names.length === 1 ? core.normalized(names[0].textContent) : core.normalized(trigger.textContent);
    const labels = [...trigger.querySelectorAll('span')].filter(el => visible(el) && (names.length !== 1 || !el.contains(names[0]))).map(el => core.normalized(el.textContent));
    core.assertExecutionAllowed({ name, category: core.accountCategory(labels) });
  }
  function observerCapitalMetrics() {
    let phase=null;try{phase=observerIdentity().phase;}catch{}
    const eligible = el => visible(el) && !el.closest('[role="dialog"],[role="alertdialog"],[role="menu"]');
    const banners = [...document.querySelectorAll('div.sticky.top-14')].filter(eligible);
    const observed_at = Date.now() / 1000;
    return Object.fromEntries(core.CAPITAL_INDICATORS.map(({key,testId,labels}) => {
      // Desktop's top-left banner omits the goal/drawdown test IDs. Read only
      // exact labels inside that banner; never search unrelated account cards.
      let sources = banners.flatMap(banner => {
        const identified = [...banner.querySelectorAll(`[data-testid="${testId}"]`)].filter(eligible);
        if (identified.length) return identified.map(el => ({el,text:elementText(el)}));
        return [...banner.querySelectorAll('p')].filter(eligible).flatMap(el => {
          const text = core.normalized(elementText(el));
          const label = labels.find(label => text.toLowerCase().startsWith(label.toLowerCase()+':') || text.startsWith(label+'：'));
          return label ? [{el,text:text.slice(label.length+1).trim()}] : [];
        });
      });
      if (!sources.length) sources = [...document.querySelectorAll(`[data-testid="${testId}"]`)]
        .filter(eligible).map(el => ({el,text:elementText(el)}));
      const readings = sources.map(({el,text}) => el.closest('[aria-busy="true"],[data-loading="true"]') ? null : core.parseCapitalMetric(text));
      const first = readings[0];
      const consistent = first && readings.every(item => item && ['current','limit','percent'].every(field => item[field] === first[field]));
      return [key,consistent ? {...first,observed_at,...(phase?{phase}:{})} : null];
    }));
  }
  function bannerUnrealized() {
    // The desktop second-row banner has no account-upnl test ID. Read its
    // exact visible label without opening the account selector or any menu.
    const eligible=el=>visible(el) && !el.closest('[role="dialog"],[role="alertdialog"],[role="menu"],[data-slot="dropdown-menu-content"]');
    const labels=/^(?:未实现盈亏|未實現盈虧|Unrealized (?:PnL|P\/L|P&L))\s*[:：]\s*(.*)$/i;
    const candidates=[...document.querySelectorAll('div.sticky.top-14')].filter(eligible)
      .flatMap(banner=>[...banner.querySelectorAll('p')].filter(eligible))
      .map(el=>({el,match:core.normalized(elementText(el)).match(labels)})).filter(item=>item.match);
    if (!candidates.length) return {found:false,value:null};
    const values=candidates.map(({el,match})=>el.closest('[aria-busy="true"],[data-loading="true"]') ? null : core.parseMoney(match[1]));
    const first=values[0];
    return {found:true,value:first!==null && values.every(value=>value===first) ? first : null};
  }
  function observerValues(expectedName) {
    observerAssertPage();
    const identity = observerIdentity();
    if (expectedName && identity.name !== expectedName) throw new Error('账户切换尚未确认，未读取资金数据');
    const equity = core.parseMoney(elementText(queryOne('[data-testid="account-value"]', document, true)));
    const banner=bannerUnrealized();
    const unrealized = banner.found ? banner.value : core.parseMoney(elementText(queryOne('[data-testid="account-upnl"]', document, true)));
    if (equity === null || unrealized === null) throw new Error('净值或浮动盈亏不可读（可能开启隐私模式）');
    let phase_snapshot;
    if(identity.phase)try{
      const reading=snapshot(),after=observerIdentity();
      if(after.name===identity.name&&after.phase===identity.phase)phase_snapshot=reading;
    }catch{}
    const observed_at=Date.now()/1000;
    return { name: identity.name, equity, unrealized_pnl: unrealized, capital_metrics:observerCapitalMetrics(),
      ...(identity.phase ? {phase:identity.phase,phase_observed_at:observed_at,phase_snapshot} : {}),
      ...(identity.category ? { category: identity.category, identity: identity.category + ':' + identity.name, status_observed_at:Date.now()/1000 } : {}),
      balance: Math.round((equity - unrealized) * 100) / 100, observed_at, read_error: '' };
  }
  function observerItems() {
    return [...document.querySelectorAll('[data-testid^="account-item-"]')].filter(visible).map((element) => {
      const name = core.normalized(element.querySelector('span.truncate')?.textContent);
      const category = element.getAttribute('data-testid').slice('account-item-'.length);
      const labels=[...element.querySelectorAll('span')].filter(el=>visible(el)&&!el.classList.contains('truncate')&&!el.querySelector('span.truncate')).map(el=>core.normalized(el.textContent));
      return { element, name, category, phase:core.accountPhase(labels) };
    });
  }
  async function observerOpenSelector(guard = () => {}, outerDeadline = Infinity) {
    guard();
    const pause=ms=>delay(Math.min(ms,Math.max(0,outerDeadline-Date.now())));
    observerAssertPage();
    if (observerItems().length) return observerItems();
    const trigger = observerTrigger();
    trigger.focus();
    // Radix opens on keyboard or pointerdown rather than a synthetic click.
    trigger.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowDown', code: 'ArrowDown', bubbles: true }));
    await pause(180);
    guard();
    if (!observerItems().length) {
      trigger.dispatchEvent(new PointerEvent('pointerdown', { pointerType: 'mouse', isPrimary: true, button: 0, bubbles: true }));
      trigger.dispatchEvent(new PointerEvent('pointerup', { pointerType: 'mouse', isPrimary: true, button: 0, bubbles: true }));
      await pause(180);
      guard();
    }
    if (!observerItems().length && trigger.getAttribute('aria-expanded') !== 'true') trigger.click();
    const deadline = Number.isFinite(outerDeadline)?outerDeadline:Date.now()+4500;
    while (Date.now() < deadline) {
      guard();
      observerAssertPage();
      const items = observerItems();
      if (items.length) return items;
      await pause(150);
    }
    guard();
    throw new Error('未找到可见账户下拉列表，请确认页面已登录且为桌面布局');
  }
  async function observerCloseSelector(guard = () => {}, outerDeadline = Infinity) {
    guard();
    const pause=ms=>delay(Math.min(ms,Math.max(0,outerDeadline-Date.now())));
    if (!observerItems().length) return;
    const menu = queryOne('[data-slot="dropdown-menu-content"]') || queryOne('[role="menu"]');
    (menu || observerTrigger()).dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', code: 'Escape', bubbles: true }));
    await pause(120);
    guard();
    if (observerItems().length) {
      const trigger = observerTrigger();
      trigger.dispatchEvent(new PointerEvent('pointerdown', { pointerType: 'mouse', isPrimary: true, button: 0, bubbles: true }));
      trigger.dispatchEvent(new PointerEvent('pointerup', { pointerType: 'mouse', isPrimary: true, button: 0, bubbles: true }));
      await pause(120);
      guard();
      if (observerItems().length && trigger.getAttribute('aria-expanded') === 'true') trigger.click();
    }
  }
  async function observerWaitStable(name, minimumDelay = 1000, { monitoring = false, samples = 3, deadline: outerDeadline = Infinity, sampleInterval = 250, guard = () => {} } = {}) {
    guard();
    await delay(Math.min(minimumDelay,Math.max(0,outerDeadline-Date.now())));
    guard();
    let previous = null;
    let matches = 0;
    let error = '账户资金读数未稳定';
    const deadline = Number.isFinite(outerDeadline)?outerDeadline:Date.now()+4000;
    while (Date.now() < deadline) {
      guard();
      try {
        const value = observerValues(name);
        const loadingFields=monitoring ? '[data-testid="account-value"],[data-testid="account-upnl"]' : '[data-testid="account-value"],[data-testid="account-upnl"],[data-testid^="capital-indicator-"],div.sticky.top-14[aria-busy="true"],div.sticky.top-14 [aria-busy="true"],div.sticky.top-14[data-loading="true"],div.sticky.top-14 [data-loading="true"]';
        const pending = [...document.querySelectorAll(loadingFields)]
          .some((el) => visible(el) && el.closest('[aria-busy="true"],[data-loading="true"]'));
        if (pending) throw new Error('账户数据仍在加载');
        const identity = observerIdentity().signature;
        // During monitoring, balances, fees and risk metrics can all change.
        // Wait for identity/loading, then validate the exact position in poll.
        // A scan or opening still requires stable cash and account metrics.
        const metrics = JSON.stringify(Object.fromEntries(Object.entries(value.capital_metrics).map(([key,item]) => [key,item && [item.current,item.limit,item.percent]])));
        matches = previous && previous.identity === identity && (monitoring ||
          (previous.metrics === metrics && Math.abs(previous.balance - value.balance) <= 0.02)) ? matches + 1 : 1;
        previous = { identity, balance: value.balance, metrics };
        if (matches >= samples) return value;
      } catch (caught) { error = caught.message; matches = 0; previous = null; }
      await delay(Math.min(sampleInterval,Math.max(0,deadline-Date.now())));
    }
    guard();
    const pending=new Error(error);
    pending.code='OBSERVATION_PENDING';
    throw pending;
  }
  async function observerSelect(account, guard = () => {}, outerDeadline = Infinity) {
    guard();
    if (!observationEnabled) throw new Error('账户读取已停止');
    observerAssertPage();
    const items = await observerOpenSelector(guard,outerDeadline);
    guard();
    for (const item of items) {
      if (items.filter(other => other.name === item.name).length === 1) {
        for (const cached of observedAccounts.filter(other => other.name === item.name)) {
          cached.category = item.category; cached.identity = item.category + ':' + item.name; cached.status_observed_at=Date.now()/1000;
          applyObservedPhase(cached,item.phase);
        }
      }
    }
    const matchingName = items.filter((item) => item.name === account.name);
    if (matchingName.length !== 1) throw new Error('账户同名或列表发生变化，不自动选择');
    const selected = matchingName;
    account.category = selected[0].category;
    account.identity = selected[0].category + ':' + account.name;
    if (observerIdentity().name === account.name) { await observerCloseSelector(guard,outerDeadline); return; }
    observerAssertPage();
    guard();
    selected[0].element.click();
    const deadline = Number.isFinite(outerDeadline)?outerDeadline:Date.now()+3000;
    while (Date.now() < deadline) {
      guard();
      observerAssertPage();
      if (observerIdentity().name === account.name && !observerItems().length) return;
      await delay(Math.min(150,Math.max(0,deadline-Date.now())));
    }
    guard();
    throw new Error('无法确认已选择正确账户；已停止扫描');
  }
  async function observerPublish(error = '', progress = null) {
    if (Date.now()-lastCostObservation>15000) {
      const quote=orderExecutionCosts();
      if (quote) {
        lastCostObservation=Date.now();
        // A read-only quote report must never change trade readiness or replay
        // a command when an older local service does not have this endpoint.
        void call('cost-observation',{quote}).catch(()=>{});
      }
    }
    return await call('observe', { accounts: observedAccounts, error, progress });
  }
  function orderExecutionCosts() {
    try {
      const form=mainForm();
      if (!form || !core.isNqTradePath(location.pathname)) return null;
      const read=labels=>{
        const rows=[...form.querySelectorAll('p,dt,label')].filter(el=>visible(el)&&labels.test(elementText(el)));
        if (rows.length!==1) return null;
        const sibling=rows[0].nextElementSibling;
        if (!sibling || !visible(sibling)) return null;
        const text=elementText(sibling),percent=[...text.matchAll(/(\d+(?:\.\d+)?)\s*%/g)];
        if (percent.length!==1 || /[<>≤≥*]/.test(text)) return null;
        return {bps:Number(percent[0][1])*100,text};
      };
      const fee=read(/^(?:Fee|Fees|手续费|手續費|费用|費用)$/i);
      if (!fee || fee.bps>100) return null;
      const quantity=numeric('quantity'),price=numeric('price'),unit=field('unit')?.dataset.unit;
      const notional=quantity>0 && price>0 ? unit==='usd'?quantity:unit==='micro'?quantity*2*price:unit==='token'?quantity*price:null:null;
      const slip=notional>0 ? read(/^(?:Slippage|Estimated Slippage|滑点|滑點|预估滑点|預估滑點)$/i) : null;
      return {symbol:'NQ-PERP',fee_bps:fee.bps,observed_at:Date.now()/1000,
        ...(slip && slip.bps<=100 ? {slippage_bps:slip.bps} : {}),...(notional>0?{notional}:{}),
        source_text:'Fee: '+fee.text+(slip?' | Slippage: '+slip.text:'')};
    } catch { return null; }
  }
  function applyObservedPhase(account, phase) {
    if(!phase)return;
    if(account.phase!==phase)Object.assign(account,{balance:null,equity:null,unrealized_pnl:null,observed_at:null,capital_metrics:{},phase_snapshot:null});
    Object.assign(account,{phase,phase_observed_at:Date.now()/1000});
  }
  async function discoverAccounts({account_timeout_seconds=3,scan_id=null} = {}) {
    assertLive();
    if (busy) throw new Error('正在处理交易或读取任务，请等待完成');
    const timeout=core.accountScanTimeoutSeconds(account_timeout_seconds);
    scanRunning=true;
    busy = true;
    observationEnabled = true;
    if (config) config.armed = false;
    let original = null;
    let originalItem = null;
    let overallError = '';
    let restored = false;
    const progress=(name='',extra={})=>{
      const succeeded=observedAccounts.filter(a=>a.scan_status==='success').length;
      const failed=observedAccounts.filter(a=>a.scan_status==='failed').length;
      return {scan_id,account_timeout_seconds:timeout,completed:succeeded+failed,total:observedAccounts.length,
        succeeded,failed,unread:observedAccounts.length-succeeded-failed,name,...extra};
    };
    const readGuard=deadline=>()=>{
      assertLive();
      if (!observationEnabled) {const error=new Error('账户读取已停止');error.code='SCAN_STOPPED';throw error;}
      if (Date.now()>=deadline) {const error=new Error(`单账号读取超时（${timeout} 秒），已跳过`);error.code='ACCOUNT_SCAN_TIMEOUT';throw error;}
    };
    try {
      observerAssertPage();
      original = observerIdentity();
      const items = await observerOpenSelector();
      if (items.length > 100) throw new Error('账户数超过 100，已停止读取');
      if (items.some((item) => !item.name || !item.category)) throw new Error('账户列表名称或类别无法明确读取');
      const counts = new Map();
      for (const item of items) counts.set(item.name, (counts.get(item.name) || 0) + 1);
      const candidates = items.filter((item) => item.name === original.name);
      if (candidates.length === 1) originalItem = { name: candidates[0].name, category: candidates[0].category };
      observedAccounts = items.map(({ name, category, phase }) => ({ name, category,...(phase?{phase,phase_observed_at:Date.now()/1000}:{}),
        identity: category + ':' + name, platform_id: null,
        equity: null, balance: null, unrealized_pnl: null, current: false, observed_at: null,
        scan_status:counts.get(name)>1?'failed':'pending',
        read_error: counts.get(name) > 1 ? '账户名称重复，无法可靠区分；未切换读取' : '等待读取' }));
      await observerCloseSelector();
      await observerPublish('', progress());
      if (!originalItem) throw new Error('当前账户名称不唯一，保留账户列表但不切换读取余额');
      for (let index = 0; index < observedAccounts.length; index++) {
        const account = observedAccounts[index];
        if (!observationEnabled) { overallError = '账户读取已停止'; break; }
        if (counts.get(account.name) > 1) continue;
        account.scan_status='reading';
        await observerPublish('', progress(account.name,{index:index+1}));
        const deadline=Date.now()+timeout*1000,guard=readGuard(deadline);
        try {
          await observerSelect(account,guard,deadline);
          const value = await observerWaitStable(account.name,200,{deadline,sampleInterval:100,guard});
          guard();
          Object.assign(account, value);
          account.scan_status='success';
        } catch (caught) {
          if (caught.code==='SCAN_STOPPED') {account.scan_status='pending';overallError=caught.message;break;}
          account.scan_status='failed';
          account.read_error = caught.message;
          let currentName=null;
          try {currentName=observerIdentity().name;} catch {}
          if (currentName===account.name) {try {account.capital_metrics=observerCapitalMetrics();} catch {}}
          const modal=[...document.querySelectorAll('[role="dialog"],[role="alertdialog"]')].some(visible);
          if (modal || (caught.code!=='ACCOUNT_SCAN_TIMEOUT' && currentName!==account.name)) {
            overallError = caught.message;
            break;
          }
          // A timed-out account leaves no detached read/select promise behind.
          // Close only its account menu, then verify the next target afresh.
          if (caught.code==='ACCOUNT_SCAN_TIMEOUT') await observerCloseSelector(()=>assertLive());
        }
        await observerPublish('',progress());
      }
    } catch (caught) { overallError = caught.message; }
    finally {
      if (!disposed && original && originalItem) {
        try {
          try {await observerPublish(overallError,progress('',{restoring:true}));} catch { /* Restore the viewing account even if the local service is offline. */ }
          // Restore the original viewing account even if the user stopped a long scan.
          const enabled = observationEnabled;
          observationEnabled = true;
          const deadline=Date.now()+timeout*1000,guard=readGuard(deadline);
          try { await observerSelect(originalItem,guard,deadline); await observerWaitStable(originalItem.name,200,{deadline,sampleInterval:100,guard}); }
          finally { observationEnabled = enabled; }
          restored = observerIdentity().signature === original.signature;
          if (!restored) throw new Error('账户名称已恢复，但状态标识变化，请人工核对');
        } catch (caught) { overallError = [overallError, '恢复原账户未确认：' + caught.message].filter(Boolean).join('；'); }
      } else if (!disposed) {
        try { await observerCloseSelector(); } catch {}
      }
      for (const account of observedAccounts) {
        account.current = restored && account.name === original?.name && account.category === originalItem?.category;
        if (account.read_error === '等待读取') account.read_error = overallError || '未读取';
      }
      if (!restored) observationEnabled = false;
      busy = false;
      try { if (!disposed) await observerPublish(overallError,progress('',{done:true,restored})); }
      catch (caught) { overallError = [overallError, '同步失败：' + caught.message].filter(Boolean).join('；'); }
      scanRunning=false;
    }
    return { accounts: observedAccounts, error: overallError, restored, ...progress() };
  }
  async function observationTick() {
    if (disposed || busy || !observationEnabled || Date.now() - lastObservationPoll < 2000) return;
    busy = true;
    lastObservationPoll = Date.now();
    try { await updateCurrentObservation(); }
    finally { busy = false; }
  }
  async function updateCurrentObservation() {
    let error = '';
    try {
      let value = observerValues();
      const matches = observedAccounts.filter((account) => account.name === value.name);
      if(matches.length===1&&matches[0].phase&&value.phase&&matches[0].phase!==value.phase)
        value=await observerWaitStable(value.name);
      for (const account of observedAccounts) account.current = false;
      if (matches.length > 1) throw new Error('当前账户名称重复，无法可靠关联余额，请先设置唯一名称');
      if (matches.length === 1) Object.assign(matches[0], value, { current: true });
      else observedAccounts.push({ ...value, category: value.category || 'unknown', identity: value.identity || 'unknown:' + value.name, platform_id: null, current: true });
    } catch (caught) {
      error = caught.message;
      try {
        const identity = observerIdentity();
        const matches = observedAccounts.filter(account => account.name === identity.name);
        if (!matches.length && ['failed','closed'].includes(identity.category)) {
          for (const account of observedAccounts) account.current=false;
          const current={name:identity.name,category:identity.category,identity:identity.category+':'+identity.name,
            status_observed_at:Date.now()/1000,current:true,balance:null,equity:null,unrealized_pnl:null,observed_at:null,platform_id:null};
          observedAccounts.push(current);matches.push(current);
        }
        if (matches.length === 1) {
          applyObservedPhase(matches[0],identity.phase);
          matches[0].capital_metrics = matches[0].observed_at ? observerCapitalMetrics() : {};
          if (identity.category) Object.assign(matches[0],{category:identity.category,identity:identity.category+':'+identity.name,status_observed_at:Date.now()/1000});
        }
      } catch {}
    }
    try {
      const published=await observerPublish(error);
      const identity=observerIdentity();
      const account=published?.accounts?.find(account=>account.name===identity.name);
      if (account?.status==='locked') {
        const current=snapshot();
        if (observerIdentity().signature!==identity.signature) throw new Error('只读核对时账户发生变化');
        await call('inspect-current',{name:identity.name,snapshot:current});
      }
    } catch (caught) { lastDiagnostic = caught.message; }
  }
  function autoConfig(account, armed = false) {
    core.assertExecutionAllowed(account);
    const identity = observerIdentity();
    if (identity.name !== account.name || observedAccounts.filter(item => item.name === account.name).length !== 1) throw new Error('账户名称无法唯一对应，停止执行');
    return { accountId: account.id, accountName: account.name, identity: account.identity,
      phase:account.phase,
      accountCategory: account.category, execution_excluded: core.executionExcluded(account), inputUnit:'micro',
      identityText: identity.signature, unitText: null, selectors: { ...core.defaults },
      enabled: true, armed, autoDiscovered: true, mode_epoch: executionEpoch };
  }
  function phaseAdvanceDialog() {
    const dialogs = [...document.querySelectorAll('[role="dialog"],[role="alertdialog"]')].filter(visible);
    if (dialogs.length !== 1 || dialogs[0].getAttribute('role') !== 'dialog') return null;
    const dialog = dialogs[0];
    const title = queryOne('[data-slot="dialog-title"]', dialog);
    const description = queryOne('[data-slot="dialog-description"]', dialog);
    const status = queryOne('[data-testid="step-status"]', dialog);
    if (!title || !description || !status || field('submit')) return null;
    if (!/^(?:Step 1 Completed!|第\s*1\s*阶段已完成[！!])$/i.test(elementText(title))) return null;
    if (!/(?:Step 2 of 2|第\s*2\s*\/\s*2\s*阶段)/i.test(elementText(description))) return null;
    const buttons = [...dialog.querySelectorAll('button')].filter(el => visible(el) && /^(?:Start Next Step|开始下一阶段)$/i.test(elementText(el)));
    if (buttons.length !== 1) return null;
    const text = elementText(status);
    const ready = /^(?:Step 2 is ready\.|第\s*2\s*阶段已准备就绪。)$/.test(text);
    if (!ready && !/^(?:Preparing Step 2\.\.\.|正在准备第\s*2\s*阶段\.\.\.)$/.test(text)) return null;
    return {dialog, button:buttons[0], ready};
  }
  async function advanceCompletedPhase(epoch, receivedJob = null) {
    if (await advanceCompletedLive(epoch)) return true;
    if (disposed || preparationOnly || executionMode !== 'execute' || executionEpoch !== epoch) return false;
    assertExecution(epoch);
    if (phaseAdvance?.epoch !== epoch) phaseAdvance = null;
    const modal = phaseAdvanceDialog();
    if (!modal && !phaseAdvance) return false;
    const identity = observerIdentity();
    if (!phaseAdvance) {
      const matches = sessionAccounts.filter(item => item.name === identity.name);
      if (identity.phase !== '1/2' || matches.length !== 1 || matches[0].phase !== '1/2' ||
          observedAccounts.filter(item => item.name === identity.name).length !== 1) throw new Error('阶段通过弹窗与当前已登记账户不符，已停止自动切换');
      core.assertExecutionAllowed(matches[0]);
      phaseAdvance = {epoch, name:identity.name, accountId:matches[0].id, clickedAt:null};
    }
    const pending = phaseAdvance;
    const account = sessionAccounts.find(item => item.id === pending.accountId);
    if (!account || identity.name !== pending.name) throw new Error('进入下一阶段期间账户发生变化，已停止操作');
    core.assertExecutionAllowed(account);
    if (pending.clickedAt === null) {
      if (!modal) { phaseAdvance = null; return false; }
      if (identity.phase !== '1/2') throw new Error('阶段通过弹窗与页面阶段不符，未点击下一阶段');
      // Waiting for the platform is read-only. Never click Got It, another
      // confirmation dialog, or a disabled/loading Start Next Step button.
      lastDiagnostic = '第一阶段已通过，等待第二阶段准备就绪';
      if (!receivedJob) {
        const readFlat = () => {
          try {
            const loading = '[data-testid="account-value"],[data-testid="account-upnl"],[data-testid="book-mark"],div.sticky.top-14,div.sticky.top-14 [aria-busy="true"],div.sticky.top-14 [data-loading="true"]';
            const panel = positionPanel(true);
            if ([...document.querySelectorAll(loading),panel].some(el => visible(el) && el.closest('[aria-busy="true"],[data-loading="true"]')) ||
                [...panel.querySelectorAll('[aria-busy="true"],[data-loading="true"]')].some(visible)) return null;
            const current = snapshot();
            return observerIdentity().name === pending.name && current.phase === '1/2' && current.position_open === false && current.unrealized_pnl === 0 &&
              [current.balance,current.equity,current.price].every(value => value !== null && Number.isFinite(value)) && current.price > 0 ? current : null;
          } catch { return null; }
        };
        const before = readFlat();
        if (before) {
          await delay(250); assertExecution(epoch);
        }
        const current = before && readFlat();
        if (current && Math.abs(current.balance - before.balance) <= 0.02) {
          // Preserve an observed native close in the old ledger before selecting
          // the successor account. ready:false cannot dispatch a trading job.
          const reply = await call('poll', {account_id:account.id,session_id:executionSession,mode_epoch:epoch,
            identity:account.identity,phase:'1/2',snapshot:current,ready:false});
          acceptSession(reply); assertExecution(epoch);
          if (reply.job) throw new Error('阶段切换只读核对收到交易任务，已停止操作');
        }
      }
      assertExecution(epoch);
      if (observerIdentity().name !== pending.name || observerIdentity().phase !== '1/2') throw new Error('点击下一阶段前账户或阶段发生变化');
      const fresh = phaseAdvanceDialog();
      if (!fresh || !fresh.ready || fresh.button.disabled || fresh.button.getAttribute('aria-disabled') === 'true' || fresh.button.getAttribute('aria-busy') !== 'false') return true;
      pending.clickedAt = Date.now(); // Reserve before the click; never replay it.
      click(fresh.button);
      lastDiagnostic = '已点击开始下一阶段，等待同名账户的 2/2 读数';
      return true;
    }
    if (Date.now() - pending.clickedAt > 20000) throw new Error('进入第二阶段后 20 秒内未能核验同名账户的空仓读数；未重复点击，请核对页面');
    if (modal || [...document.querySelectorAll('[role="dialog"],[role="alertdialog"]')].some(visible)) {
      if (!modal) throw new Error('阶段切换期间出现其他弹窗，请人工核对');
      return true;
    }
    if (identity.phase !== '2/2') return true;
    const values = await observerWaitStable(pending.name, 300);
    assertExecution(epoch);
    const current = values.phase_snapshot;
    if (values.phase !== '2/2' || current?.phase !== '2/2' || current.position_open !== false ||
        current.unrealized_pnl !== 0 || !(current.price > 0)) throw new Error('第二阶段账户尚未提供明确空仓证明，停止后续交易');
    const cached = observedAccounts.filter(item => item.name === pending.name);
    if (cached.length !== 1) throw new Error('第二阶段同名账户无法唯一对应');
    applyObservedPhase(cached[0], '2/2');
    for (const item of observedAccounts) item.current = item.name === pending.name;
    Object.assign(cached[0], values, {current:true});
    const reply = await observerPublish();
    acceptSession(reply); assertExecution(epoch);
    const saved = reply?.accounts?.find(item => item.id === pending.accountId && item.name === pending.name);
    if (saved?.phase !== '2/2' || saved.phase_pending) return true;
    if (config?.accountId === pending.accountId) config.armed = false;
    phaseAdvance = null;
    lastDiagnostic = '同名账户已进入 2/2，阶段账本已同步';
    return true;
  }
  let liveAdvance=null;
  function liveActivationDialog() {
    const dialogs=[...document.querySelectorAll('[role="dialog"],[role="alertdialog"]')].filter(visible);
    if (dialogs.length!==1 || dialogs[0].getAttribute('role')!=='dialog') return null;
    const dialog=dialogs[0],title=queryOne('[data-slot="dialog-title"]',dialog);
    if (!title || queryOne('[data-testid="confirm-order-submit"],[data-testid="close-submit"]',dialog)) return null;
    let description=queryOne('[data-slot="dialog-description"]',dialog);
    if (!description) {
      const header=title.closest('[data-slot="dialog-header"]');
      const siblings=header?[...header.children].filter(el=>visible(el)&&el!==title&&!el.contains(title)):[];
      if (siblings.length===1) description=siblings[0];
    }
    if (!core.liveActivationNotice(elementText(title),elementText(description))) return null;
    const buttons=[...dialog.querySelectorAll('button')].filter(visible);
    const go=buttons.filter(el=>/^(?:前往 Funded Account|Go to Funded Account)$/i.test(elementText(el)));
    if (go.length!==1 || buttons.some(el=>el!==go[0] && !/^(?:我理解|I Understand)$/i.test(elementText(el)))) return null;
    if (buttons.length>1 && !go[0].disabled) return null;
    return {dialog,button:go[0]};
  }
  async function advanceCompletedLive(epoch) {
    if (disposed || preparationOnly || executionMode!=='execute' || executionEpoch!==epoch) return false;
    if (liveAdvance && liveAdvance.epoch!==epoch) liveAdvance=null;
    const modal=liveActivationDialog();
    if (!modal && !liveAdvance) return false;
    assertExecution(epoch);
    if (!liveAdvance) {
      const identity=observerIdentity(),matches=sessionAccounts.filter(a=>a.name===identity.name);
      if (identity.phase!=='2/2' || identity.category!=='passed' || matches.length!==1 || matches[0].phase!=='2/2' ||
          observedAccounts.filter(a=>a.name===identity.name).length!==1) throw new Error('评估通过弹窗与已登记二阶段账号不符');
      core.assertExecutionAllowed(matches[0]);
      if (modal.button.disabled || modal.button.getAttribute('aria-disabled')==='true' || modal.button.getAttribute('aria-busy')==='true') {
        lastDiagnostic='评估已通过，等待平台允许激活 Live；未点击禁用或加载中的按钮';return true;
      }
      assertNoDialogs(modal.dialog);
      const reserved=await call('live-activation-reserve',{account_id:matches[0].id,name:identity.name,mode_epoch:epoch});
      assertExecution(epoch);
      if (reserved.duplicate || reserved.status!=='pending') throw new Error('该账号已有 Live 激活尝试，请核对结果；未重复点击');
      const fresh=liveActivationDialog(),current=observerIdentity();
      if (!fresh || current.name!==identity.name || current.phase!=='2/2' || current.category!=='passed' ||
          fresh.button.disabled || fresh.button.getAttribute('aria-disabled')==='true' || fresh.button.getAttribute('aria-busy')==='true') throw new Error('激活前账号或按钮状态变化，未点击');
      assertNoDialogs(fresh.dialog);assertExecution(epoch);
      liveAdvance={epoch,name:identity.name,accountId:matches[0].id,clickedAt:reserved.started_at,returnClicked:false};
      click(fresh.button);
      lastDiagnostic='已点击前往 Funded Account，等待同名 Live 账号；不会重复激活';return true;
    }
    const pending=liveAdvance;
    if (pending.epoch!==epoch) throw new Error('Live 激活授权已变化，请核对结果');
    if (Date.now()-pending.clickedAt>20000) throw new Error('Live 激活后 20 秒内未完成核验；未重复点击，请核对页面');
    if (modal) return true;
    assertNoDialogs();
    let identity;
    try {identity=observerIdentity();} catch {return true;}
    if (identity.name!==pending.name) throw new Error('Live 激活后账号名称变化，请核对关联账号');
    if (location.pathname==='/portfolio') {
      if (identity.phase!=='live') return true;
      if (!pending.returnClicked) {
        const links=[...document.querySelectorAll('a[href]')].filter(el=>visible(el)&&/^(?:交易|Trade)$/i.test(elementText(el))&&
          el.origin===location.origin && ['/trade','/trade/NQ-PERP','/trade/NDX-USD-PERP'].includes(el.pathname));
        const link=core.sameDestinationNavigation(links);
        if (!link) {lastDiagnostic='Live 已激活，等待交易导航提供唯一目标地址';return true;}
        assertExecution(epoch);pending.returnClicked=true;click(link);
      }
      return true;
    }
    if (!core.isNqTradePath(location.pathname)) {
      if (location.pathname==='/trade') return true;
      throw new Error('Live 激活后未返回 NQ 交易页，请核对页面');
    }
    if (identity.phase!=='live') return true;
    const values=await observerWaitStable(pending.name,300),proof=values.phase_snapshot;
    assertExecution(epoch);
    if (values.phase!=='live' || proof?.phase!=='live' || proof.position_open!==false || proof.unrealized_pnl!==0 || !(proof.price>0)) throw new Error('Live 账号缺少新鲜明确空仓读数');
    const cached=observedAccounts.filter(a=>a.name===pending.name);
    if (cached.length!==1) throw new Error('Live 同名账号无法唯一对应');
    applyObservedPhase(cached[0],'live');
    for (const item of observedAccounts) item.current=item.name===pending.name;
    Object.assign(cached[0],values,{current:true});
    const reply=await observerPublish();acceptSession(reply);assertExecution(epoch);
    const saved=reply?.accounts?.find(a=>a.id===pending.accountId && a.name===pending.name);
    if (saved?.phase!=='live' || saved.phase_pending) return true;
    await call('live-activation-complete',{account_id:pending.accountId,name:pending.name,mode_epoch:epoch});
    assertExecution(epoch);
    if (config?.accountId===pending.accountId) config.armed=false;
    liveAdvance=null;
    lastDiagnostic=saved.strategy_locked?'已进入 Live 并默认锁定，继续其他账号':'Live 阶段已核验，按独立参数继续策略';
    return true;
  }
  function terminalAccountDialog() {
    const dialogs=[...document.querySelectorAll('[role="dialog"],[role="alertdialog"]')].filter(visible);
    if (dialogs.length!==1 || dialogs[0].getAttribute('role')!=='dialog') return null;
    const dialog=dialogs[0];
    if (dialog.matches('[data-testid="close-dialog"]') || queryOne('[data-testid="confirm-order-submit"]',dialog)) return null;
    const title=queryOne('[data-slot="dialog-title"]',dialog);
    let description=queryOne('[data-slot="dialog-description"]',dialog);
    if (!description && title) {
      // Vest's capital-result header uses a plain sibling div, not the shared
      // DialogDescription component (verified against the current public build).
      const header=title.closest('[data-slot="dialog-header"]');
      const siblings=header ? [...header.children].filter(el=>visible(el) && el!==title && !el.contains(title)) : [];
      if (siblings.length===1) description=siblings[0];
    }
    const reason=core.terminalNoticeReason(elementText(title),elementText(description));
    if (!reason) return null;
    const identity=observerIdentity();
    if (!['failed','closed'].includes(identity.category) || core.executionExcluded(identity)) return null;
    const close=[...dialog.querySelectorAll('button')].filter(button=>visible(button) &&
      /^(?:Close|关闭|關閉)$/i.test(core.normalized(button.getAttribute('aria-label') || button.textContent)) &&
      !button.matches('[data-testid="close-submit"],[data-testid="confirm-order-submit"],[data-testid^="position-close-"]'));
    if (close.length!==1) return null;
    return {dialog,button:close[0],identity,reason};
  }
  async function handleCurrentTerminal(epoch) {
    const identity=observerIdentity();
    if (!['failed','closed'].includes(identity.category) || core.executionExcluded(identity)) return false;
    const modal=terminalAccountDialog();
    const saved=sessionAccounts.filter(item=>item.name===identity.name);
    const acknowledged=saved.length===1 && saved[0].retired_failed;
    if (!acknowledged) {
      assertExecution(epoch);
      if (!await reportCurrentTerminal(identity.name)) return true;
      assertExecution(epoch);
    }
    if (!modal) return !acknowledged;
    assertExecution(epoch);
    const fresh=terminalAccountDialog();
    if (!fresh || fresh.dialog!==modal.dialog || fresh.identity.signature!==identity.signature) return true;
    const dismissed = terminalDismissals.get(fresh.dialog);
    if (dismissed?.epoch===epoch && dismissed.name===identity.name) {
      lastDiagnostic=identity.name+' 关闭提示尚未消失，等待读取；不重复点击';
      return true;
    }
    assertNoDialogs(fresh.dialog);
    if (fresh.button.disabled || fresh.button.getAttribute('aria-disabled')==='true' || fresh.button.getAttribute('aria-busy')==='true') return true;
    // Archive acknowledgement precedes this informational Close. Paid retry,
    // account purchases, order confirmations and historical links are untouched.
    terminalDismissals.set(fresh.dialog,{epoch,name:identity.name});
    click(fresh.button);
    lastDiagnostic=identity.name+' 已确认关闭并归档，已关闭亏损上限提示，等待切换下一账户';
    return true;
  }
  async function reportCurrentTerminal(expectedName) {
    // Read only the named current-account badge. A missing account, hidden
    // money or an unrelated card/dialog is never proof of evaluation failure.
    const identity=observerIdentity();
    if (!['failed','closed'].includes(identity.category) || (expectedName && identity.name!==expectedName)) return false;
    let matches=observedAccounts.filter(item=>item.name===identity.name);
    if (matches.length>1) throw new Error('终止账户名称不唯一，不能自动归档');
    await delay(250);
    const stable=observerIdentity();
    if (stable.name!==identity.name || stable.signature!==identity.signature || stable.category!==identity.category) return false;
    if (!matches.length) {
      // Closed accounts disappear from the selector, but the current header is
      // still observable. Do not invent money or require a new selector entry.
      const current={name:identity.name,category:identity.category,current:true,
        balance:null,equity:null,unrealized_pnl:null,observed_at:null,platform_id:null};
      observedAccounts.push(current);matches=[current];
    }
    try { Object.assign(matches[0],observerValues(identity.name)); } catch { /* Status can be read even when money is private. */ }
    Object.assign(matches[0],{category:identity.category,identity:identity.category+':'+identity.name,status_observed_at:Date.now()/1000,current:true});
    const reply=await observerPublish();
    acceptSession(reply);
    const retired=reply?.accounts?.find(item=>item.name===identity.name);
    if (!retired?.retired_failed) throw new Error('失败状态尚未被服务确认，请保持当前页面核对');
    if (config?.accountName===identity.name) config.armed=false;
    lastDiagnostic=identity.name+' 已终止，已结束该号任务并跳过';
    return true;
  }
  async function reportCurrentPhaseChange(account) {
    if(!account?.phase)return false;
    const identity=observerIdentity(),rank=phase=>['1/2','2/2','live'].indexOf(phase);
    if(identity.name!==account.name||!identity.phase||rank(identity.phase)<=rank(account.phase))return false;
    await delay(250);
    if(observerIdentity().phase!==identity.phase||observerIdentity().name!==identity.name)return false;
    let cached=observedAccounts.find(item=>item.name===identity.name);
    if(!cached)return false;
    applyObservedPhase(cached,identity.phase);
    try{Object.assign(cached,observerValues(identity.name),{current:true});}catch{cached.current=true;}
    const reply=await observerPublish();acceptSession(reply);
    const saved=reply?.accounts?.find(item=>item.id===account.id);
    if(saved?.phase===identity.phase||saved?.phase_pending?.phase===identity.phase){
      if(config?.accountId===account.id)config.armed=false;
      lastDiagnostic='阶段已切换，结束旧阶段任务并同步新阶段';return true;
    }
    return false;
  }
  function assertExecution(epoch) {
    assertLive();
    if (executionMode !== 'execute' || executionEpoch !== epoch) throw new Error('插件已切回只读或权限已更新');
    assertConnection();
  }
  function assertConnection() {
    if (connectionBlocked) throw Object.assign(new Error('本地连接暂时中断，暂停操作等待原授权核验'), {code:'EXECUTION_CONNECTION_PENDING'});
  }
  function acceptConnection(reply) {
    if (reply?.mode_epoch !== executionEpoch || executionMode !== 'execute' ||
        !Number.isInteger(reply.connection_revision) || reply.connection_revision < connectionRevision) return;
    connectionRevision = reply.connection_revision;
    connectionBlocked = !!reply.connection_pending;
    if (connectionBlocked) lastDiagnostic = '本地连接暂时中断，暂停操作等待恢复：' + (reply.connection_error || '');
  }
  function acceptSession(reply) {
    if (disposed) return;
    if (reply?.stale_response) return;
    if (reply?.mode_epoch !== undefined && reply.mode_epoch < executionEpoch) return;
    acceptConnection(reply);
    if (Array.isArray(reply?.accounts) && reply.accounts.length) sessionAccounts = reply.accounts;
    if (reply?.mode === 'readonly') {
      profitClaimFlow?.dispose();
      executionMode = 'readonly'; preparingEpoch = null;
      executionEpoch = Math.max(executionEpoch, reply.mode_epoch || 0);
      if (config) config.armed = false;
    }
  }
  function accountTerminated(account) {
    const saved=sessionAccounts.find(item=>item.id===account?.id);
    return !!saved && (saved.retired_failed || saved.archived);
  }
  async function executionHeartbeat() {
    if (disposed || heartbeatBusy || executionMode !== 'execute' || Date.now() - lastModeHeartbeat < 2000) return;
    heartbeatBusy = true; lastModeHeartbeat = Date.now();
    const epoch = executionEpoch;
    try { const reply=await call('mode-heartbeat',{profit_claim:profitClaimFlow?.heartbeat()}); if (epoch===executionEpoch) acceptSession(reply); }
    catch (error) { if (epoch===executionEpoch) { profitClaimFlow?.dispose();executionMode = 'readonly'; if (config) config.armed = false; lastDiagnostic = error.message; } }
    finally { heartbeatBusy = false; }
  }
  async function executionTick() {
    if (disposed || busy || preparationOnly || executionMode !== 'execute') return;
    busy = true;
    const epoch = executionEpoch;
    let receivedJob = null;
    let account = null;
    try {
      assertExecution(epoch);
      if (await profitClaimFlow?.run(epoch)) return;
      if (await handleCurrentTerminal(epoch)) return;
      if (await advanceCompletedPhase(epoch)) return;
      const routed = await call('execution-next');
      acceptSession(routed);
      assertExecution(epoch);
      account = routed.next_account;
      if (routed.profit_claim) {
        if(!profitClaimFlow)throw new Error('利润领取模块未加载，请重新加载新版插件');
        await profitClaimFlow.begin(routed.profit_claim,epoch);return;
      }
      if (!account) {
        if (await reportCurrentTerminal()) return;
        // An enabled permission is idle until actual work exists. Keep the
        // currently viewed account up to date without preparing any form.
        if (observationEnabled && Date.now()-lastObservationPoll>=2000) {
          lastObservationPoll = Date.now();
          await updateCurrentObservation();
        }
        return;
      }
      if (await reportCurrentTerminal(account.name)) return;
      core.assertExecutionAllowed(account);
      const cached = observedAccounts.filter(item => item.name === account.name);
      if (cached.length !== 1) throw new Error('目标账户已变化，请切回只读重新读取账户');
      observationEnabled = true;
      const alreadySelected=observerIdentity().name===account.name;
      if (!alreadySelected) await observerSelect(cached[0], () => assertExecution(epoch));
      assertExecution(epoch);
      const opening = !routed.next_account.operation || routed.next_account.operation === 'open';
      const actualPhase=observerIdentity().phase;
      const phaseChanged=!!actualPhase&&!!cached[0].phase&&actualPhase!==cached[0].phase;
      // A selected account is monitored directly. Only a real account switch
      // needs the selector and its delayed loading checks; no repeated menus.
      const directMonitor=alreadySelected && !opening && !phaseChanged;
      const values = await observerWaitStable(account.name,directMonitor?0:1000,
        {monitoring:!opening&&!phaseChanged,samples:directMonitor?1:3});
      assertExecution(epoch);
      for (const item of observedAccounts) item.current = item.name === account.name;
      Object.assign(cached[0], values, { current: true });
      const synchronized = await observerPublish();
      acceptSession(synchronized);
      assertExecution(epoch);
      account = synchronized.accounts?.find(item => item.id === account.id) || { ...account, category: cached[0].category, identity: cached[0].identity };
      if (account.retired_failed) return;
      if(account.phase_pending){
        const active=queryOne('[data-testid="account-tab-active-positions"]');
        if(active&&!isSelected(active))await selectTab('[data-testid="account-tab-active-positions"]',()=>document,()=>assertExecution(epoch));
        await updateCurrentObservation();return;
      }
      config = autoConfig(account, true);
      const prepared = await prepareAccount(() => assertExecution(epoch), opening);
      assertExecution(epoch);
      const completedFlat = account.category === 'passed' && prepared.position_open === false;
      const current = !opening || account.category === 'passed' ? prepared : readyCheck();
      const result = await call('poll', { account_id: account.id, session_id: executionSession,
        mode_epoch: epoch, identity: account.identity, phase:account.phase, snapshot: current, ready: !completedFlat });
      if (!result.job) { acceptSession(result); return; }
      receivedJob = result.job;
      try {
        assertExecution(epoch);
        const observed = await execute(receivedJob);
        if (await reportCurrentTerminal(account.name)) return;
        await call('result', { account_id: account.id, session_id: executionSession, mode_epoch: epoch,
          result: { job_id: receivedJob.id, success: true, confirmed: true, snapshot: observed } });
      } catch (error) {
        if (await advanceCompletedPhase(epoch, receivedJob)) return;
        if (await reportCurrentTerminal(account.name)) return;
        if (await reportCurrentPhaseChange(account)) return;
        if (accountTerminated(account)) return;
        let observed;
        try { observed = assertAccount(receivedJob.type === 'open'); } catch { observed = unknownSnapshot(); }
        await call('result', { account_id: account.id, session_id: executionSession, mode_epoch: epoch,
          result: { job_id: receivedJob.id, success: false, confirmed: false, error: error.message, snapshot: observed } });
        throw error;
      }
    } catch (error) {
      lastDiagnostic = error.message;
      if (!receivedJob && error.code === 'EXECUTION_CONNECTION_PENDING') return;
      if (!receivedJob) {
        try { if (await advanceCompletedPhase(epoch)) return; }
        catch (phaseError) { error = phaseError; lastDiagnostic = phaseError.message; }
      }
      try { if (await reportCurrentTerminal(account?.name)) return; } catch (failureError) { lastDiagnostic=failureError.message; }
      try { if (await reportCurrentPhaseChange(account)) return; } catch (phaseError) { lastDiagnostic=phaseError.message; }
      if (accountTerminated(account)) return;
      if (!receivedJob && error.code==='OBSERVATION_PENDING') {
        // No action was delivered: the next tick only reads/prepares again.
        // Keep the user's execute permission and never replay a submitted order.
        lastDiagnostic=error.message+'；等待读数更新后继续';
        return;
      }
      if (executionMode === 'execute' && executionEpoch === epoch) {
        profitClaimFlow?.dispose();
        executionMode = 'readonly'; preparingEpoch = null;
        if (config) config.armed = false;
        try { await call('execution-failed', { error: error.message, mode_epoch:epoch }); } catch {}
      }
    } finally { busy = false; }
  }
  function preparationBanner(text) {
    let banner = document.getElementById('vest-preparation-banner');
    if (!banner) {
      banner = document.createElement('div'); banner.id = 'vest-preparation-banner';
      Object.assign(banner.style, { position:'fixed',top:'10px',left:'10px',right:'10px',zIndex:'2147483647',
        padding:'12px 16px',border:'1px solid #e7b74a',borderRadius:'8px',background:'#15171c',color:'#e7b74a',
        font:'13px sans-serif',pointerEvents:'none' });
      document.body.append(banner);
    }
    banner.textContent = '下一批预备窗口 · 只读，禁止下单和平仓 · ' + text;
    if (!document.title.startsWith('预备·只读')) document.title = '预备·只读 | ' + document.title;
  }
  function selectorFor(el) {
    const testId = el.getAttribute('data-testid');
    if (testId) return `[data-testid="${CSS.escape(testId)}"]`;
    if (el.id && document.querySelectorAll('#' + CSS.escape(el.id)).length === 1) return '#' + CSS.escape(el.id);
    let node = el;
    const parts = [];
    while (node && node !== document.body) {
      const tag = node.tagName.toLowerCase();
      const siblings = [...node.parentElement.children].filter((item) => item.tagName === node.tagName);
      parts.unshift(tag + (siblings.length > 1 ? `:nth-of-type(${siblings.indexOf(node) + 1})` : ''));
      const selector = parts.join(' > ');
      if (document.querySelectorAll(selector).length === 1) return selector;
      node = node.parentElement;
    }
    return 'body > ' + parts.join(' > ');
  }
  function startPicker(key) {
    pickCleanup?.();
    const overlay = document.createElement('div');
    Object.assign(overlay.style, { position: 'fixed', zIndex: '2147483647', pointerEvents: 'none', border: '2px solid #eac657', background: '#eac65722' });
    const label = document.createElement('div');
    Object.assign(label.style, { position: 'fixed', top: '10px', left: '50%', transform: 'translateX(-50%)', zIndex: '2147483647', pointerEvents: 'none', background: '#15171c', color: '#eac657', padding: '12px 18px', border: '1px solid #eac657', borderRadius: '8px', font: '14px sans-serif' });
    label.textContent = 'VEST Control · 点击目标控件进行映射（不会操作该控件）· Esc 取消';
    document.body.append(overlay, label);
    const move = (event) => {
      const rect = event.target.getBoundingClientRect();
      Object.assign(overlay.style, { left: rect.left + 'px', top: rect.top + 'px', width: rect.width + 'px', height: rect.height + 'px' });
    };
    // Radix controls can react on pointerdown/mousedown before click.
    const blockPointer = (event) => { event.preventDefault(); event.stopImmediatePropagation(); };
    const select = async (event) => {
      event.preventDefault(); event.stopImmediatePropagation();
      let el = event.target;
      // Prefer the actual input/button when a child inside it was clicked.
      if (['quantity', 'takeProfit', 'stopLoss'].includes(key)) el = el.closest('input') || el.querySelector('input') || el;
      else if (['buy', 'sell', 'submit', 'close', 'closeConfirm'].includes(key)) el = el.closest('button,[role="button"]') || el;
      const selector = selectorFor(el);
      const text = core.normalized(elementText(el));
      pickCleanup();
      await call('pick-result', { field: key, selector, text });
    };
    const keydown = (event) => { if (event.key === 'Escape') pickCleanup(); };
    pickCleanup = () => {
      document.removeEventListener('mousemove', move, true);
      document.removeEventListener('click', select, true);
      document.removeEventListener('keydown', keydown, true);
      for (const name of ['pointerdown', 'pointerup', 'mousedown', 'mouseup']) document.removeEventListener(name, blockPointer, true);
      overlay.remove(); label.remove(); pickCleanup = null;
    };
    document.addEventListener('mousemove', move, true);
    document.addEventListener('click', select, true);
    document.addEventListener('keydown', keydown, true);
    for (const name of ['pointerdown', 'pointerup', 'mousedown', 'mouseup']) document.addEventListener(name, blockPointer, true);
  }
  function messageHandler(message, _sender, respond) {
    if (disposed) { respond({ok:false,error:'页面控制器已停止'}); return; }
    if (!bootReady && message.type !== 'bridge-ping') { booted.then(() => messageHandler(message,_sender,respond)); return true; }
    if (message.type === 'bridge-ping') {
      const answer = () => respond({ ok: true, protocol: 4, version: VERSION, instance_id: instanceId, busy, reading: observationEnabled && busy });
      if (!bootReady) { booted.then(answer); return true; }
      answer(); return;
    }
    if (message.type === 'connection-state') { acceptConnection(message); respond({ok:true}); return; }
    if (preparationOnly && !(message.type === 'control-mode' && message.mode === 'readonly')) {
      respond({ ok:false,error:'此窗口固定只读，仅用于下一批账户预备' }); return;
    }
    if (message.type === 'control-mode') {
      if (message.mode === 'readonly') {
        const epoch = Number.isInteger(message.mode_epoch) ? message.mode_epoch : executionEpoch + 1;
        if (message.revoke || epoch >= executionEpoch) {
          profitClaimFlow?.dispose();
          executionMode = 'readonly'; preparingEpoch = null; executionEpoch = Math.max(executionEpoch, epoch);
          if (config) config.armed = false;
        }
        respond({ ok: true, mode: executionMode, mode_epoch: executionEpoch }); return;
      }
      if (message.mode !== 'execute' || !message.session_id || !Array.isArray(message.accounts) || !message.accounts.length || !Number.isInteger(message.mode_epoch) || message.mode_epoch < executionEpoch) { respond({ ok: false, error: '执行授权已过期或账户未识别' }); return; }
      executionMode = 'execute'; executionEpoch = message.mode_epoch; executionSession = message.session_id;
      profitClaimFlow?.dispose();
      connectionBlocked = false; connectionRevision = 0;
      sessionAccounts = message.accounts; observationEnabled = true; preparingEpoch = null;
      respond({ ok: true, mode: executionMode, mode_epoch: executionEpoch }); return;
    }
    if (message.type === 'prepare-execution') {
      if (busy) {
        // Let an already-started observation finish before reserving the mode
        // epoch; its late read-only response must not revoke the new grant.
        (async () => {
          const deadline = Date.now()+5000;
          while (busy && !disposed && message.mode_epoch>=executionEpoch && Date.now()<deadline) await delay(50);
          if (disposed || busy) { respond({ok:false,error:'页面仍在读取账户或等待交易结果，请完成后再切换模式'}); return; }
          messageHandler(message,_sender,respond);
        })().catch(error => respond({ok:false,error:error.message}));
        return true;
      }
      try {
        assertLive();
        const currentName = observerIdentity().name;
        const matches = observedAccounts.filter(item => item.name === currentName);
        if (matches.length !== 1) throw new Error('请先读取账户，且当前账户名称必须唯一');
        observerAssertPage(true);
        if (!Number.isInteger(message.mode_epoch) || message.mode_epoch < executionEpoch || !message.session_id) throw new Error('执行授权已过期');
        preparingEpoch = message.mode_epoch; executionEpoch = Math.max(executionEpoch, message.mode_epoch); executionSession = message.session_id;
        // Mode preflight confirms connection and identity only. The current
        // view may be the excluded main account or have no opening form.
        if (config) config.armed = false;
        config = null;
        respond({ ok:true, identity:observerIdentity(), deferred:true });
      } catch (error) { preparingEpoch = null; respond({ ok: false, error: error.message }); return; }
      return;
    }
    if (message.type === 'configure') { config = message.config; tabKey = message.tabKey; if (config?.armed) observationEnabled = false; respond({ ok: true }); return; }
    if (message.type === 'observe-start-scan') {
      if (busy) { respond({ ok: false, error: '正在处理交易或读取任务，请等待完成或先停止读取' }); return; }
      const scanId = globalThis.crypto?.randomUUID?.() || String(Date.now());
      // Acknowledge independently of the popup lifetime; progress is persisted
      // through background messages and survives closing/reopening the popup.
      let timeout;
      try {timeout=core.accountScanTimeoutSeconds(message.account_timeout_seconds??3);} catch(error) {respond({ok:false,error:error.message});return;}
      const scanning = discoverAccounts({account_timeout_seconds:timeout,scan_id:scanId});
      respond({ ok: true, started: true, scan_id: scanId, account_timeout_seconds:timeout });
      scanning.catch(async (error) => {
        observationEnabled = false;
        try { if (!disposed) await observerPublish(error.message, { completed: 0, total: observedAccounts.length, done: true, restored: false }); } catch {}
      });
      return;
    }
    if (message.type === 'observe-accounts') {
      discoverAccounts({account_timeout_seconds:message.account_timeout_seconds??3}).then((result) => respond({ ok: true, ...result })).catch((error) => respond({ ok: false, error: error.message }));
      return true;
    }
    if (message.type === 'observation-stop') { observationEnabled = false; respond({ ok: true, scan_running:scanRunning }); return; }
    if (message.type === 'observe-current') {
      try { respond({ ok: true, snapshot: observerValues(),execution_costs:orderExecutionCosts() }); }
      catch (error) { respond({ ok: false, error: error.message }); }
      return;
    }
    if (message.type === 'pick') {
      if (busy) { respond({ ok: false, error: '正在处理交易任务，请先停用执行并等待结果' }); return; }
      startPicker(message.field); respond({ ok: true }); return;
    }
    if (message.type === 'inspect') {
      try { const current = readyCheck(); respond({ ok: true, snapshot: current, identity: elementText(field('identity', true)), unit: elementText(field('unit', true)) }); }
      catch (error) { respond({ ok: false, error: error.message }); }
      return;
    }
    if (message.type === 'capture') {
      try { respond({ ok: true, identity: elementText(field('identity', true)), unit: elementText(field('unit', true)), snapshot: snapshot() }); }
      catch (error) { respond({ ok: false, error: error.message }); }
      return;
    }
    if (message.type === 'prepare') {
      if (busy) { respond({ ok: false, error: '正在处理交易任务，不能同时准备表单' }); return; }
      busy = true;
      prepare().then((value) => { busy = false; respond({ ok: true, snapshot: value }); })
        .catch((error) => { busy = false; respond({ ok: false, error: error.message }); });
      return true;
    }
  }
  function storageHandler(changes) { if (!disposed && !preparationOnly && tabKey && changes[tabKey]) config = changes[tabKey].newValue; }
  function dispose() {
    if (disposed) return;
    disposed = true;
    observationEnabled = false;
    executionMode = 'readonly'; preparingEpoch = null;
    if (config) config.armed = false;
    if (timerId !== null) clearInterval(timerId);
    try { pickCleanup?.(); } catch {}
    try { runtime.onMessage.removeListener(messageHandler); } catch {}
    try { chrome.storage.onChanged.removeListener(storageHandler); } catch {}
    for (const event of ['pointerdown','mousedown','click','submit','keydown']) document.removeEventListener(event,blockPreparationTrade,true);
    // The public controller may remain on window after an extension reload.
    // Release the retired actor's cached account data and weak-map metadata.
    config = null; observedAccounts = []; sessionAccounts = [];
    profitClaimFlow?.dispose();
    phaseAdvance = null; liveAdvance = null; terminalDismissals = new WeakMap();
  }
  const instanceId = globalThis.crypto?.randomUUID?.() || String(Date.now()) + Math.random();
  profitClaimFlow=globalThis.VestProfitClaimFlow?.create({
    guard:assertExecution,identity:observerIdentity,visible,text:elementText,money:core.parseMoney,click,call,
    isTrade:core.isNqTradePath,
    async select(name,epoch){
      const cached=observedAccounts.filter(a=>a.name===name);
      if(cached.length!==1)throw new Error('领取账户无法唯一对应');
      if(observerIdentity().name!==name)await observerSelect(cached[0],()=>assertExecution(epoch));
    },
    async flat(name,epoch){
      const account=sessionAccounts.find(a=>a.name===name&&a.phase==='live');
      if(!account)throw new Error('领取账户未登记为 Live');
      config=autoConfig(account,false);
      await prepareAccount(()=>assertExecution(epoch),false);
      const values=await observerWaitStable(name,300,{monitoring:true,samples:3});
      assertExecution(epoch);const proof=values.phase_snapshot;
      if(!proof||proof.position_open!==false||proof.phase!=='live')throw new Error('领取账户缺少明确空仓证据');
      for(const cached of observedAccounts){cached.current=cached.name===name;if(cached.current)Object.assign(cached,values);}
      return proof;
    },
  })||null;
  globalThis[CONTROLLER] = { version: VERSION, protocol: 4, instance_id: instanceId, dispose };
  runtime.onMessage.addListener(messageHandler);
  chrome.storage.onChanged.addListener(storageHandler);
  // The popup sends a current tab-bound configuration; a reload requires reconnection.
  // Never silently reuse another tab's settings or arm a newly opened tab.
  runtime.sendMessage({ type: 'observation-restore' }).then((result) => {
    if (disposed) return;
    if (result?.data?.preparation) { preparationOnly = true; protectPreparationWindow(); }
    if (preparationOnly) { preparationBanner('此功能已取消，请关闭此旧窗口'); return; }
    if (!disposed && result?.ok && result.data?.enabled) {
      observationEnabled = true;
      observedAccounts = Array.isArray(result.data.accounts) ? result.data.accounts : [];
    }
    if (result?.ok && result.data) { executionSession = result.data.session_id || null; executionEpoch = result.data.mode_epoch || 0; }
  }).catch(() => {}).finally(() => { bootReady = true; finishBoot(); });
  timerId = setInterval(() => {
    try { assertLive(); } catch { return; }
    if (!bootReady) return;
    if (preparationOnly) return;
    if (preparingEpoch !== null) return;
    void executionHeartbeat();
    return executionMode === 'execute' ? executionTick() : observationEnabled ? observationTick() : tick();
  }, 1000);
})();
