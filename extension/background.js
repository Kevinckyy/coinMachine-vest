'use strict';
importScripts('core.js');
const ENDPOINT = 'http://127.0.0.1:8765';
const BRIDGE_PROTOCOL = 4;
const contentConnections = new Map();
const scanStarts = new Map();
const controlChanges = new Map();
const modeWrites = new Map();
const activationWrites = new Map();
const volatileModes = {};
const PREPARATION_HASH = '#vest-preparation=';
const modeStorage = chrome.storage.session || {
  async get(key) { return { [key]: volatileModes[key] }; },
  async set(values) { Object.assign(volatileModes, values); },
};
const PAGE_COMMANDS = new Set(['bridge-ping', 'configure', 'capture', 'inspect', 'pick', 'prepare', 'observe-current']);

function bounded(promise, milliseconds, message) {
  let timer;
  return Promise.race([
    promise,
    new Promise((_, reject) => { timer = setTimeout(() => reject(new Error(message)), milliseconds); }),
  ]).finally(() => clearTimeout(timer));
}

function extensionPage(sender) {
  // An extension page opened in a full tab is trusted in the same way as its popup.
  return !sender.tab || (typeof sender.url === 'string' && sender.url.startsWith(chrome.runtime.getURL('')));
}

function uuid() { return globalThis.crypto?.randomUUID?.() || Date.now().toString(36) + Math.random().toString(36).slice(2); }

async function serializeActivation(key, action) {
  const next=(activationWrites.get(key)||Promise.resolve()).catch(()=>{}).then(action);
  activationWrites.set(key,next);
  try {return await next;} finally {if(activationWrites.get(key)===next)activationWrites.delete(key);}
}

async function observationConfig(tabId) {
  const key = 'observation:' + tabId;
  const saved = await chrome.storage.local.get([key, 'installation-id']);
  const observation = { accounts: [], mode_epoch: 0, ...saved[key] };
  if (!observation.session_id) {
    const installationId = saved['installation-id'] || uuid();
    observation.session_id = installationId + ':' + uuid();
    await chrome.storage.local.set({ 'installation-id': installationId, [key]: observation });
  }
  return observation;
}

async function controlState(tabId) {
  const observation = await observationConfig(tabId);
  const stored = await modeStorage.get('control-mode:' + tabId);
  return { mode: 'readonly', mode_epoch: observation.mode_epoch || 0, session_id: observation.session_id, ...stored['control-mode:' + tabId] };
}

async function preparationRole(tabId, url) {
  if (!Number.isInteger(tabId)) return null;
  const saved = (await chrome.storage.local.get('preparation-tab:' + tabId))['preparation-tab:' + tabId];
  if (saved) return saved;
  if (!url) { try { url = (await chrome.tabs.get(tabId)).url; } catch {} }
  try { if (new URL(url).hash.startsWith(PREPARATION_HASH)) return { orphan: true }; } catch {}
  return null;
}

async function closePreparation(ownerTabId) {
  const key = 'preparation-owner:' + ownerTabId;
  const saved = (await chrome.storage.local.get(key))[key];
  await chrome.storage.local.remove(key);
  if (Number.isInteger(saved?.tabId)) {
    try { await chrome.tabs.remove(saved.tabId); } catch {}
  }
}

async function closeRetiredPreparations() {
  const saved=await chrome.storage.local.get(null);
  for (const key of Object.keys(saved)) if (key.startsWith('preparation-owner:')) {
    const tabId=Number(key.slice('preparation-owner:'.length));
    if (Number.isInteger(tabId)) await closePreparation(tabId);
  }
}

async function saveControl(tabId, state) {
  const write = (modeWrites.get(tabId) || Promise.resolve()).catch(() => {}).then(async () => {
    const current = (await modeStorage.get('control-mode:' + tabId))['control-mode:' + tabId];
    if (current && current.mode_epoch > state.mode_epoch) return current;
    await modeStorage.set({ ['control-mode:' + tabId]: state });
    const observation = await observationConfig(tabId);
    await chrome.storage.local.set({ ['observation:' + tabId]: { ...observation, mode_epoch: Math.max(observation.mode_epoch || 0, state.mode_epoch) } });
    return state;
  });
  modeWrites.set(tabId, write);
  try { return await write; } finally { if (modeWrites.get(tabId) === write) modeWrites.delete(tabId); }
}

function revokeActor(tabId) {
  // Revoke locally before waiting for storage, injection or a network request.
  return chrome.tabs.sendMessage(tabId, { type: 'control-mode', mode: 'readonly', revoke: true }, { frameId: 0 }).catch(() => {});
}

async function saveExecutionError(tabId, error, epoch) {
  const key = 'execution-error:' + tabId;
  const saved = (await chrome.storage.local.get(key))[key];
  if (saved?.mode_epoch > epoch) return;
  const observation = await observationConfig(tabId);
  const message = error ? String(error.message || error).split(observation.token || '\0').join('[redacted]').slice(0,500) : '';
  await chrome.storage.local.set({ [key]: { message, time:new Date().toISOString(), mode_epoch:epoch } });
}

async function sessionRequest(tabId, options = {}) {
  const observation = await observationConfig(tabId);
  const state = options.control_state ? { ...options.control_state } : await controlState(tabId);
  if (!observation.token) throw new Error('请先填写本地配对码并读取账户');
  const tab = await targetTab(tabId);
  const result = await localRequest('/api/bridge/session', {
    session_id: observation.session_id, mode: state.mode, mode_epoch: state.mode_epoch,
    tab_url: tab.url, accounts: observation.accounts || [],
    ...(options.current_account ? { current_account: options.current_account } : {}),
    heartbeat_only: !!options.heartbeat_only,
    ...(state.revocation_source ? { revocation_source: state.revocation_source } : {}),
    ...(options.execution_error ? {execution_error:String(options.execution_error).slice(0,500)} : {}),
    ...(options.authorize === true ? { authorize: true, mode: 'execute' } : {}),
  }, observation.token);
  const latest = await controlState(tabId);
  if (latest.mode_epoch !== state.mode_epoch) return { mode: latest.mode, mode_epoch: latest.mode_epoch, accounts: [], next_account: null, stale_response: true };
  if (typeof result.mode_epoch === 'number' && result.mode_epoch < latest.mode_epoch) return { ...latest, accounts: [], next_account: null, stale_response: true };
  if (Array.isArray(result.accounts)) {
    const fresh = await observationConfig(tabId);
    await chrome.storage.local.set({ ['observation:' + tabId]: { ...fresh, registrations: result.accounts } });
  }
  if (result.mode === 'readonly' && latest.mode === 'execute') {
    void revokeActor(tabId);
    await saveControl(tabId, { ...latest, mode: 'readonly', mode_epoch: Math.max(latest.mode_epoch, result.mode_epoch || 0) });
    await saveExecutionError(tabId,'本地服务已撤销执行许可或连接已过期，请重新开启执行模式',Math.max(latest.mode_epoch,result.mode_epoch || 0));
  }

  return result;
}

async function changeControlMode(tabId, requested, token, failureReason = '') {
  await closeRetiredPreparations();
  if (await preparationRole(tabId)) throw new Error('此窗口固定为下一批预备只读窗口，请返回原执行窗口');
  if (!['readonly', 'execute'].includes(requested)) throw new Error('控制模式无效');
  const revoking = revokeActor(tabId);
  const changeId = uuid();
  controlChanges.set(tabId, changeId);
  const observation = await observationConfig(tabId);
  const current = await controlState(tabId);
  let state = { session_id: observation.session_id, mode: 'readonly', mode_epoch: Math.max(observation.mode_epoch || 0, current.mode_epoch || 0) + 1 };
  await saveControl(tabId, state);
  if (token) {
    const saved = await observationConfig(tabId);
    await chrome.storage.local.set({ ['observation:' + tabId]: { ...saved, token, enabled: true } });
  }
  await revoking;
  const validChange = () => controlChanges.get(tabId) === changeId;
  try {
    if (requested === 'readonly') {
      await closePreparation(tabId);
      try { await sessionRequest(tabId, { heartbeat_only: true, execution_error:failureReason }); } catch { /* Local revocation is already effective; the server lease also expires. */ }
      return { ...state, accounts: (await observationConfig(tabId)).registrations || [] };
    }
    if (!observation.accounts?.length) throw new Error('请先读取 Vest 账户列表，再切换为执行');
    await ensureContentScript(tabId);
    if (!validChange()) throw new Error('模式切换已取消');
    // Injection may have retired an old page actor and incremented its epoch.
    // Reserve a newer authorization so that the actor's delayed revocation can
    // never overwrite the user's newly requested execution permission.
    const preparedState = await controlState(tabId);
    if (!validChange()) throw new Error('模式切换已取消');
    state = { ...state, mode_epoch: Math.max(state.mode_epoch, preparedState.mode_epoch) + 1 };
    await saveControl(tabId, state);
    const prepared = await sendPageCommand(tabId, { type: 'prepare-execution', mode_epoch: state.mode_epoch, session_id: state.session_id, accounts: observation.accounts }, validChange);
    if (!prepared?.ok) throw new Error(prepared?.error || '无法确认页面连接和当前账户身份');
    if (!validChange()) throw new Error('模式切换已取消');
    const granted = await sessionRequest(tabId, { authorize: true, heartbeat_only: true });
    if (!validChange() || granted.stale_response || granted.mode !== 'execute') throw new Error('执行权限未生效，请检查账户状态后重试');
    const active = { ...state, mode: 'execute', mode_epoch: granted.mode_epoch ?? state.mode_epoch };
    await saveControl(tabId, active);
    const configured = await sendPageCommand(tabId, { type: 'control-mode', ...active, accounts: granted.accounts }, validChange);
    if (!configured?.ok) throw new Error(configured?.error || '页面未能启用执行模式');
    if (!validChange()) throw new Error('模式切换已取消');
    await saveExecutionError(tabId,null,active.mode_epoch);
    return { ...active, accounts: granted.accounts };
  } catch (error) {
    if (validChange()) {
      void revokeActor(tabId);
      const latest = await controlState(tabId);
      await saveControl(tabId, { ...latest, mode: 'readonly', mode_epoch: latest.mode_epoch + 1 });
      await saveExecutionError(tabId,error,latest.mode_epoch+1);
      try { await sessionRequest(tabId, { heartbeat_only: true, execution_error:error.message }); } catch {}
    }
    throw error;
  }
}

async function targetTab(tabId) {
  if (!Number.isInteger(tabId) || tabId < 0) throw new Error('未找到目标标签页，请在 Vest 交易页面打开扩展');
  let tab;
  try { tab = await bounded(chrome.tabs.get(tabId), 2500, '读取标签页超时，请重新打开扩展'); }
  catch { throw new Error('Vest 标签页已关闭或不可访问，请重新打开交易页面'); }
  if (!vestTab(tab.url)) throw new Error('请在 https://next.vestmarkets.com/trade/NQ-PERP 标签页打开扩展');
  return tab;
}

function validHandshake(reply) {
  const version = chrome.runtime.getManifest?.()?.version;
  return reply?.ok === true && reply.protocol === BRIDGE_PROTOCOL && (!version || reply.version === version);
}

async function ensureContentScript(tabId) {
  if (contentConnections.has(tabId)) return contentConnections.get(tabId);
  const connecting = (async () => {
    await targetTab(tabId);
    try {
      const reply = await bounded(chrome.tabs.sendMessage(tabId, { type: 'bridge-ping' }, { frameId: 0 }), 1500, '页面握手超时');
      if (validHandshake(reply)) return reply;
    } catch { /* Only the idempotent handshake is retried; never a business command. */ }
    if (!chrome.scripting?.executeScript) throw new Error('扩展尚未加载新版脚本权限，请在扩展管理页重新加载 VEST Control，并允许访问 Vest 网站');
    try {
      await bounded(chrome.scripting.executeScript({
        target: { tabId, frameIds: [0] }, world: 'ISOLATED', files: ['core.js', 'content.js'],
      }), 7000, '加载页面脚本超时');
    } catch (error) {
      throw new Error('无法连接 Vest 页面脚本：' + error.message + '。请检查扩展的站点访问权限，然后刷新 Vest 页面');
    }
    try {
      const reply = await bounded(chrome.tabs.sendMessage(tabId, { type: 'bridge-ping' }, { frameId: 0 }), 2000, '页面脚本未响应握手');
      if (validHandshake(reply)) return reply;
    } catch (error) {
      throw new Error('页面脚本加载后仍未连接：' + error.message + '。请刷新 Vest 页面后重试');
    }
    throw new Error('页面脚本版本不兼容，请刷新 Vest 页面后重新打开扩展');
  })();
  contentConnections.set(tabId, connecting);
  try { return await connecting; }
  finally { if (contentConnections.get(tabId) === connecting) contentConnections.delete(tabId); }
}

async function sendPageCommand(tabId, message, canSend = null) {
  await ensureContentScript(tabId);
  if (canSend && !canSend()) throw new Error('页面操作已取消');
  // Navigation can invalidate this connection after a command was received.
  // Do not inject-and-replay: even a failed reply does not prove an action did not run.
  try {
    return await bounded(chrome.tabs.sendMessage(tabId, message, { frameId: 0 }), 10000,
      '页面操作响应超时，未自动重试，请核对当前页面状态');
  } catch (error) {
    throw new Error('页面通信中断：' + error.message + '。该操作未自动重试，请检查页面后再操作');
  }
}

async function observationFailure(tabId, error) {
  const key = 'observation:' + tabId;
  const saved = await chrome.storage.local.get(key);
  await chrome.storage.local.set({
    [key]: { ...saved[key], enabled: false },
    ['observation-status:' + tabId]: {
      time: new Date().toISOString(), phase: 'error', progress: null,
      error: error.message, count: saved[key]?.accounts?.length || 0,
    },
  });
}

function vestTab(url) {
  try { const parsed = new URL(url); return parsed.origin === 'https://next.vestmarkets.com' && ['/trade/NQ-PERP','/trade/NDX-USD-PERP'].includes(parsed.pathname.replace(/\/$/, '')); }
  catch { return false; }
}

async function localRequest(path, body, token) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 8000);
  try {
    const response = await fetch(ENDPOINT + path, {
      method: body === undefined ? 'GET' : 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Bridge-Token': token || '' },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      signal: controller.signal,
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || `HTTP ${response.status}`);
    return result;
  } finally { clearTimeout(timeout); }
}

chrome.runtime.onMessage.addListener((message, sender, respond) => {
  (async () => {
    const roleTabId = extensionPage(sender) ? message.tabId : sender.tab?.id;
    const preparation = await preparationRole(roleTabId, sender.tab?.id === roleTabId ? sender.url : undefined);
    if (preparation) {
      if (message.type === 'control-status' && extensionPage(sender)) return { mode: 'readonly', preparation: true };
      if (message.type === 'observation-restore' && sender.tab && vestTab(sender.url)) return { enabled: false, preparation: true, mode: 'readonly' };
      if (message.type === 'page-command' && extensionPage(sender) && message.message?.type === 'bridge-ping') return await sendPageCommand(message.tabId, message.message);
      if (['preparation-next','preparation-observed','preparation-error'].includes(message.type)) return {active:false,accounts:[],disabled:true};
      throw new Error('预备窗口仅允许读取下一批账户，禁止执行、下单、平仓或交易回执');
    }
    if (['preparation-next','preparation-observed','preparation-error'].includes(message.type)) throw new Error('此标签页不是预备窗口');
    if (message.type === 'control-status') {
      if (!extensionPage(sender)) throw new Error('控制模式仅可从插件查看');
      return await controlState(message.tabId);
    }
    if (message.type === 'control-mode') {
      if (!extensionPage(sender)) throw new Error('只读/执行只能通过插件开关控制');
      return await changeControlMode(message.tabId, message.mode, message.token);
    }
    if (message.type === 'execution-next' || message.type === 'mode-heartbeat') {
      if (message.type==='mode-heartbeat' && sender.tab && sender.url==='https://next.vestmarkets.com/portfolio') {
        const control=await controlState(sender.tab.id);
        const pending=(await chrome.storage.local.get('live-activation-tab:'+sender.tab.id))['live-activation-tab:'+sender.tab.id];
        if (control.mode==='execute' && pending?.session_id===control.session_id && pending.mode_epoch===control.mode_epoch &&
            pending.status==='pending' && Date.now()-pending.started_at>=0 && Date.now()-pending.started_at<20000) {
          // The platform temporarily routes an activation to Portfolio. No trade
          // API or fictitious NQ heartbeat is sent from that page; the actor has
          // less than the existing lease duration to return through the UI.
          return {...control,accounts:[],next_account:null,phase_activation_pending:true};
        }
        throw new Error('Live 激活衔接已过期，请返回 NQ 页面核对');
      }
      if (!sender.tab || !vestTab(sender.url)) throw new Error('执行调度仅接受 Vest 页面');
      const state = await controlState(sender.tab.id);
      if (state.mode !== 'execute') return { ...state, accounts: [], next_account: null };
      try {
        return await sessionRequest(sender.tab.id, { heartbeat_only: message.type === 'mode-heartbeat', current_account: message.current_account });
      } catch (error) {
        const latest = await controlState(sender.tab.id);
        if (latest.mode_epoch===state.mode_epoch && latest.mode==='execute') {
          void revokeActor(sender.tab.id);
          await closePreparation(sender.tab.id);
          await saveControl(sender.tab.id, { ...latest, mode: 'readonly', mode_epoch: latest.mode_epoch + 1 });
          await saveExecutionError(sender.tab.id,error,latest.mode_epoch+1);
        }
        throw error;
      }
    }
    if (message.type==='live-activation-reserve' || message.type==='live-activation-complete') {
      if (!sender.tab || !vestTab(sender.url)) throw new Error('Live 激活仅接受已连接的 NQ 页面');
      const control=await controlState(sender.tab.id),observation=await observationConfig(sender.tab.id);
      if (control.mode!=='execute' || control.mode_epoch!==message.mode_epoch) throw new Error('Live 激活执行许可已变化');
      const matches=(observation.registrations||[]).filter(a=>a.id===message.account_id && a.name===message.name && !a.execution_excluded);
      if (matches.length!==1) throw new Error('Live 激活账号无法唯一对应');
      const account=matches[0],key='live-activation:'+control.session_id+':'+account.id,tabKey='live-activation-tab:'+sender.tab.id;
      return await serializeActivation(key,async()=>{
      const latest=await controlState(sender.tab.id);
      if(latest.mode!=='execute'||latest.mode_epoch!==control.mode_epoch)throw new Error('Live 激活执行许可已变化');
      const saved=(await chrome.storage.local.get(key))[key];
      if (message.type==='live-activation-complete') {
        if (!saved || saved.status!=='pending' || account.phase!=='live' || account.phase_pending) throw new Error('Live 阶段尚未被服务确认');
        await chrome.storage.local.set({[key]:{...saved,status:'completed',completed_at:Date.now()},[tabKey]:null});
        return {completed:true};
      }
      if (account.phase!=='2/2') throw new Error('只有已登记二阶段账号可进入 Live');
      if (saved) return {...saved,duplicate:true};
      const pending={session_id:control.session_id,mode_epoch:control.mode_epoch,account_id:account.id,name:account.name,
        started_at:Date.now(),status:'pending',tab_id:sender.tab.id};
      await chrome.storage.local.set({[key]:pending,[tabKey]:pending});
      return pending;
      });
    }
    if (message.type === 'execution-failed') {
      if (!sender.tab || !vestTab(sender.url)) throw new Error('执行状态来源无效');
      const current = await controlState(sender.tab.id);
      if (message.mode_epoch !== undefined && message.mode_epoch !== current.mode_epoch) return current;
      const result = await changeControlMode(sender.tab.id, 'readonly',undefined,message.error || '页面状态无法确认');
      await saveExecutionError(sender.tab.id,message.error || '页面状态无法确认，已切回只读',result.mode_epoch);
      const observation = await observationConfig(sender.tab.id);
      await chrome.storage.local.set({ ['observation-status:' + sender.tab.id]: {
        time: new Date().toISOString(), phase: 'error', progress: null,
        count: observation.accounts?.length || 0, error: String(message.error || '页面状态无法确认，已切回只读').slice(0, 500),
      } });
      return result;
    }
    if (message.type === 'page-command') {
      if (!extensionPage(sender)) throw new Error('页面连接必须从扩展设置页发起');
      if (!message.message || !PAGE_COMMANDS.has(message.message.type)) throw new Error('不允许的页面操作');
      return await sendPageCommand(message.tabId, message.message);
    }
    if (message.type === 'observe-start' || message.type === 'observe-stop') {
      if (!extensionPage(sender)) throw new Error('只读扫描必须从扩展设置页启动');
      const tab = await targetTab(message.tabId);
      const observationKey = 'observation:' + tab.id;
      const bindingKey = 'tab:' + tab.id;
      const saved = await chrome.storage.local.get([observationKey, bindingKey]);
      if (message.type === 'observe-stop') {
        await changeControlMode(message.tabId, 'readonly');
        scanStarts.delete(message.tabId);
        await chrome.storage.local.set({ [observationKey]: { ...saved[observationKey], enabled: false } });
        let stopResponse=null;
        try { stopResponse=await bounded(chrome.tabs.sendMessage(message.tabId, { type: 'observation-stop' }, { frameId: 0 }), 1500, '停止连接超时'); }
        catch { /* Stopping a disconnected tab must never inject or restart it. */ }
        const latest=(await chrome.storage.local.get('observation-status:'+message.tabId))['observation-status:'+message.tabId];
        const stopping=stopResponse?.scan_running && !latest?.progress?.done;
        await chrome.storage.local.set({ ['observation-status:' + message.tabId]: { time: new Date().toISOString(), phase: stopping?'stopping':'stopped', progress: stopping?{...latest?.progress,stopping:true,done:false}:latest?.progress?.done?latest.progress:null, error: '', count: saved[observationKey]?.accounts?.length || 0 } });
        return { ok: true };
      }
      if (scanStarts.has(message.tabId)) throw new Error('正在连接此标签页，请稍候或先停止读取');
      const startId = Symbol('scan-start');
      // Claim synchronously before any storage await so two popups cannot both start.
      scanStarts.set(message.tabId, startId);
      try {
        const previousStatus = await chrome.storage.local.get('observation-status:' + message.tabId);
        const status = previousStatus['observation-status:' + message.tabId];
        if (status?.phase === 'scanning' && Date.now() - Date.parse(status.time) < VestBridgeCore.accountScanIdleTimeoutMs(status.progress)) {
          const activeScan = new Error('账户扫描仍在进行，请查看读取进度或先停止读取');
          activeScan.preserveObservation = true;
          throw activeScan;
        }
        if (!message.token || typeof message.token !== 'string') throw new Error('请填写本地配对密钥');
        const control = await controlState(message.tabId);
        if (control.mode === 'execute') { const active = new Error('请先切回只读，再刷新完整账户列表'); active.preserveObservation = true; throw active; }
        await chrome.storage.local.set({ ['observation-status:' + message.tabId]: { time: new Date().toISOString(), phase: 'connecting', progress: null, error: '', count: 0 } });
        // Pair first; injecting a listener never grants permission to trade.
        await localRequest('/api/bridge/accounts', undefined, message.token);
        await ensureContentScript(message.tabId);
        if (scanStarts.get(message.tabId) !== startId) throw new Error('账户连接已取消');
        const observation = { ...await observationConfig(message.tabId), token: message.token, enabled: true };
        await chrome.storage.local.set({ [observationKey]: observation });
        if (saved[bindingKey]) {
          const binding = { ...saved[bindingKey], armed: false };
          await chrome.storage.local.set({ [bindingKey]: binding });
          const configured = await sendPageCommand(message.tabId, { type: 'configure', config: binding, tabKey: bindingKey });
          if (!configured?.ok) throw new Error(configured?.error || '无法停用原交易执行，请先核对标签页');
        }
        const scanSettings=(await chrome.storage.local.get('account-scan-settings'))['account-scan-settings'];
        const accountTimeout=VestBridgeCore.accountScanTimeoutSeconds(scanSettings?.timeout_seconds??3);
        const response = await sendPageCommand(message.tabId, { type: 'observe-start-scan', account_timeout_seconds:accountTimeout }, () => scanStarts.get(message.tabId) === startId);
        if (!response?.ok || !response.started) throw new Error(response?.error || '页面未能启动扫描，请检查连接状态');
        return response;
      } catch (error) {
        if (scanStarts.get(message.tabId) === startId && !error.preserveObservation) await observationFailure(message.tabId, error);
        throw error;
      } finally {
        if (scanStarts.get(message.tabId) === startId) scanStarts.delete(message.tabId);
      }
    }
    if (message.type === 'observe' || message.type === 'observation-restore') {
      if (!sender.tab || !vestTab(sender.url)) throw new Error('只允许读取 Vest 交易标签页');
      const observationKey = 'observation:' + sender.tab.id;
      const stored = await chrome.storage.local.get(observationKey);
      const observation = stored[observationKey];
      if (message.type === 'observation-restore') {
        // A new page actor never inherits execution permission from the old page.
        const previous = await controlState(sender.tab.id);
        const readonly = { ...previous, mode: 'readonly', mode_epoch: previous.mode_epoch + 1, revocation_source:'page-actor-restore' };
        await saveControl(sender.tab.id, readonly);
        await closePreparation(sender.tab.id);
        // Capture the revocation's epoch now. If this asynchronous request reads
        // a later preflight epoch, its late readonly heartbeat can revoke a newly
        // granted execute lease at that SAME epoch.
        void sessionRequest(sender.tab.id, { heartbeat_only: true, control_state:readonly }).catch(() => {});
        return { enabled: !!observation?.enabled, accounts: observation?.accounts || [], ...readonly };
      }
      if ((!observation?.enabled && !message.progress?.done) || !observation?.token) throw new Error('只读连接未配对或已停止');
      const accounts = Array.isArray(message.accounts) ? message.accounts : [];
      const progress = message.progress || null;
      const previousStatus=(await chrome.storage.local.get('observation-status:'+sender.tab.id))['observation-status:'+sender.tab.id];
      const displayProgress=progress || (previousStatus?.progress?.done ? previousStatus.progress : null);
      await chrome.storage.local.set({
        [observationKey]: { ...observation, accounts },
        ['observation-status:' + sender.tab.id]: { time: new Date().toISOString(), phase: progress ? progress.done ? message.error ? 'error' : 'complete' : 'scanning' : message.error ? 'error' : 'observing', progress:displayProgress, error: message.error || '', count: accounts.length },
      });
      if (progress?.done && progress.restored !== true) {
        const latest = await chrome.storage.local.get(observationKey);
        await chrome.storage.local.set({ [observationKey]: { ...latest[observationKey], enabled: false } });
      }
      const result = await localRequest('/api/bridge/observe', { tab_url: sender.url, accounts, error: message.error || '', progress }, observation.token);
      const session = await sessionRequest(sender.tab.id, { heartbeat_only: true });
      return { ...result, ...session };
    }
    if (message.type === 'accounts') {
      if (!extensionPage(sender)) throw new Error('仅扩展设置页可以读取账户列表');
      const result = await localRequest('/api/bridge/accounts', undefined, message.token);
      return { accounts: result.accounts };
    }
    if (message.type === 'pick-result') {
      if (!sender.tab || !sender.url?.startsWith('https://next.vestmarkets.com/')) throw new Error('页面来源不匹配');
      const key = 'tab:' + sender.tab.id;
      const stored = await chrome.storage.local.get(key);
      const config = stored[key];
      if (!config) throw new Error('请先保存标签页绑定');
      config.selectors[message.field] = message.selector;
      if (message.field === 'identity') config.identityText = message.text;
      if (message.field === 'unit') config.unitText = message.text;
      config.armed = false;
      await chrome.storage.local.set({ [key]: config });
      return { ok: true };
    }
    if (message.type === 'inspect-current') {
      if (!sender.tab || !vestTab(sender.url)) throw new Error('只读核对仅接受 Vest 页面');
      const observation=await observationConfig(sender.tab.id);
      const account=observation.registrations?.find(account=>account.name===message.name);
      if (!account) throw new Error('只读核对账户尚未识别');
      return await localRequest('/api/bridge/inspect',{account_id:account.id,name:account.name,
        session_id:observation.session_id,snapshot:message.snapshot,tab_url:sender.url},observation.token);
    }
    if (message.type === 'cost-observation') {
      if (!sender.tab || !vestTab(sender.url)) throw new Error('费用报价只接受 Vest 交易页面的只读观察');
      const observation=await observationConfig(sender.tab.id);
      if (!observation.token || !message.quote || typeof message.quote!=='object') throw new Error('费用观察尚未配对');
      return await localRequest('/api/backtests/cost-observation',message.quote,observation.token);
    }
    if (message.type === 'size') {
      if (!sender.tab || !vestTab(sender.url)) throw new Error('仓位大小只能在 Vest 执行窗口确认');
      const observation = await observationConfig(sender.tab.id);
      const state = await controlState(sender.tab.id);
      if (state.mode!=='execute' || message.mode_epoch!==state.mode_epoch || message.session_id!==observation.session_id) throw new Error('执行权限已更新，禁止开仓');
      const account = observation.registrations?.find(account=>account.id===message.account_id && account.identity===message.identity);
      if (!account) throw new Error('无法核对仓位大小对应的账户');
      if (account.execution_excluded) throw new Error('主账户永久排除所有执行');
      return await localRequest('/api/bridge/size',{
        job_id:message.job_id,account_id:account.id,identity:account.identity,session_id:observation.session_id,
        mode_epoch:state.mode_epoch,phase:account.phase,size_percent:message.size_percent,quantity:message.quantity,snapshot:message.snapshot,tab_url:sender.url,
        ...(message.drawdown !== undefined ? {drawdown:message.drawdown,stop_loss:message.stop_loss} : {}),
      },observation.token);
    }
    if (message.type === 'poll' || message.type === 'result') {
      if (!sender.tab || !sender.url?.startsWith('https://next.vestmarkets.com/trade/')) throw new Error('必须在 Vest 交易标签页运行');
      const key = 'tab:' + sender.tab.id;
      const stored = await chrome.storage.local.get(key);
      const config = stored[key];
      if (message.session_id) {
        const observation = await observationConfig(sender.tab.id);
        const state = await controlState(sender.tab.id);
        if (message.session_id !== observation.session_id) throw new Error('页面账户会话不匹配');
        if (message.type === 'poll') {
          if (state.mode !== 'execute' || message.mode_epoch !== state.mode_epoch) return { job: null, mode: state.mode, mode_epoch: state.mode_epoch };
          const account = observation.registrations?.find(account => account.id === message.account_id && account.identity === message.identity);
          if (!account) throw new Error('账户尚未自动识别或名称已变化');
          if (account.execution_excluded) throw new Error('主账户永久排除所有执行');
          return await localRequest('/api/bridge/poll', {
            account_id: account.id, session_id: observation.session_id, mode_epoch: state.mode_epoch,
            identity: account.identity, phase:message.phase,snapshot: message.snapshot, ready: message.ready === true, tab_url: sender.url,
          }, observation.token);
        }
        // Delivered actions retain their original epoch for reconciliation after a switch to read-only.
        return await localRequest('/api/bridge/result', {
          ...message.result, account_id: message.account_id, session_id: observation.session_id, mode_epoch: message.mode_epoch,
        }, observation.token);
      }
      if (!config?.token || !config.accountId || !config.enabled) return { job: null };
      if (message.type === 'poll') {
        const status = {
          time: new Date().toISOString(), ready: !!message.ready && !!config.armed,
          snapshot: message.snapshot, diagnostic: message.diagnostic || '',
        };
        await chrome.storage.local.set({ ['status:' + sender.tab.id]: status });
        return await localRequest('/api/bridge/poll', {
          account_id: config.accountId,
          snapshot: message.snapshot,
          ready: status.ready,
          tab_url: sender.url,
        }, config.token);
      }
      return await localRequest('/api/bridge/result', {
        ...message.result, account_id: config.accountId,
      }, config.token);
    }
    throw new Error('未知桥接消息');
  })().then((data) => respond({ ok: true, data })).catch((error) => respond({ ok: false, error: error.message }));
  return true;
});

chrome.tabs.onRemoved.addListener((tabId) => {
  (async () => {
    const role = await preparationRole(tabId, '');
    if (role && !role.orphan) {
      await chrome.storage.local.remove(['preparation-tab:'+tabId,'preparation-owner:'+role.ownerTabId]);
      return;
    }
    await closePreparation(tabId);
    const key = 'observation:' + tabId;
    const saved = (await chrome.storage.local.get(key))[key];
    const mode = (await modeStorage.get('control-mode:' + tabId))['control-mode:' + tabId];
    if (saved?.session_id && saved.token) {
      try { await localRequest('/api/bridge/session', { session_id: saved.session_id,
        mode: 'readonly', mode_epoch: Math.max(saved.mode_epoch || 0, mode?.mode_epoch || 0) + 1,
        tab_url: 'https://next.vestmarkets.com/trade/NQ-PERP', accounts: saved.accounts || [], heartbeat_only: true }, saved.token); } catch {}
    }
    await chrome.storage.local.remove(['tab:' + tabId, 'status:' + tabId, key, 'observation-status:' + tabId, 'execution-error:' + tabId]);
    try { await modeStorage.remove?.('control-mode:' + tabId); } catch {}
  })().catch(() => {});
});

closeRetiredPreparations().catch(() => {});
