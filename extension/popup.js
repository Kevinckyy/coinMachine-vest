'use strict';
const $ = (id) => document.getElementById(id);
let tab;
let scanActive = false;
let mode = 'readonly';
let observationStatus = null;
let changing = false;
let modeRequest = 0;
let preparationOnly = false;
let initialized = false;
const extensionVersion = chrome.runtime.getManifest().version;
function renderExecutionError(details) {
  $('executionError').hidden = !details?.message;
  $('executionError').textContent = details?.message ? `上次执行错误（${new Date(details.time).toLocaleTimeString()}）：${details.message}` : '';
}
function show(message, error = false) { $('status').textContent = typeof message === 'string' ? message : JSON.stringify(message, null, 2); $('status').className = error ? 'error' : 'success'; }
async function background(message, timeout = 25000) {
  let timer;
  try {
    const response = await Promise.race([chrome.runtime.sendMessage(message), new Promise((_, reject) => { timer = setTimeout(() => reject(new Error('页面连接超时，请检查扩展权限及 Vest 页面；未自动重复操作')), timeout); })]);
    if (!response?.ok) throw new Error(response?.error || '扩展后台未连接，请重新加载扩展');
    return response.data;
  } catch (error) {
    if (/Receiving end|Could not establish connection|context invalidated/i.test(error.message)) throw new Error(`请在扩展管理页重新加载 VEST Control ${extensionVersion}，再在 Vest 页面打开扩展`);
    throw error;
  } finally { clearTimeout(timer); }
}
function renderMode(next) {
  mode = !preparationOnly && next === 'execute' ? 'execute' : 'readonly';
  $('modeSwitch').checked = mode === 'execute';
  $('modeBadge').textContent = mode === 'execute' ? '执行' : '只读';
  $('modeDescription').textContent = mode === 'execute' ? '工作台可向已识别的账户下单或运行策略；账户和页面状态会自动校验。' : '只同步账户信息；工作台不能向此页面发送交易操作。';
  $('readAccounts').disabled = scanActive || mode === 'execute';
  $('modeSwitch').disabled = scanActive || !initialized;
  if (preparationOnly) {
    $('modeSwitch').disabled = true; $('readAccounts').disabled = true;
    $('modeBadge').textContent = '已停用';
    $('modeDescription').textContent = '旧预备窗口功能已取消；请关闭此窗口并返回原交易窗口。';
  }
}
function showObservation(status) {
  if (!status) return;
  observationStatus = status;
  const progress = status.progress;
  scanActive = status.phase === 'connecting' || status.phase === 'scanning' || !!(progress && !progress.done);
  $('accountCount').textContent = `${status.count || 0} 个账户`;
  let text = status.phase === 'connecting' ? '正在连接页面…' : scanActive ? progress?.total ? `正在读取 ${Math.min(progress.completed + 1, progress.total)} / ${progress.total}：${progress.name || '账户'}` : '正在读取账户列表…' : `已读取 ${status.count || 0} 个账户，结果显示在工作台。`;
  if (status.error) text += ' ' + status.error;
  $('readProgress').textContent = text;
  $('readProgress').className = status.error ? 'hint error' : 'hint';
  renderMode(mode);
}
function summary(accounts) {
  $('accountsSummary').replaceChildren();
  $('accountCount').textContent = `${accounts?.length || 0} 个账户`;
  for (const account of (accounts || []).slice(0, 10)) {
    const row = document.createElement('div'); row.className = 'summary-row';
    const name = document.createElement('span'); name.textContent = account.name;
    const info = document.createElement('small'); info.textContent = VestBridgeCore.executionExcluded(account) ? '永久排除 · 只读' : account.ambiguous ? '名称重复' : account.category || '已识别';
    row.append(name, info); $('accountsSummary').append(row);
  }
}
async function init() {
  [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab?.id) throw new Error('请在 Vest 交易页面打开扩展');
  const observationKey = 'observation:' + tab.id;
  const statusKey = 'observation-status:' + tab.id;
  const modeKey = 'control-mode:' + tab.id;
  const errorKey = 'execution-error:' + tab.id;
  const saved = await chrome.storage.local.get([observationKey, statusKey, errorKey]);
  renderExecutionError(saved[errorKey]);
  $('token').value = saved[observationKey]?.token || '';
  summary(saved[observationKey]?.accounts);
  showObservation(saved[statusKey]);
  try { const current = await background({ type: 'control-status', tabId: tab.id }); preparationOnly = current.preparation === true; renderMode(current.mode); } catch { renderMode('readonly'); }
  chrome.storage.onChanged.addListener((changes, area) => {
    if (area === 'session' && changes[modeKey]) renderMode(changes[modeKey].newValue?.mode);
    if (area === 'local' && changes[statusKey]) showObservation(changes[statusKey].newValue);
    if (area === 'local' && changes[observationKey]) summary(changes[observationKey].newValue?.accounts);
    if (area === 'local' && changes[errorKey]) renderExecutionError(changes[errorKey].newValue);
  });
  $('modeSwitch').addEventListener('change', async () => {
    const requestId = ++modeRequest;
    const requested = $('modeSwitch').checked ? 'execute' : 'readonly';
    const token = $('token').value.trim();
    changing = true;
    if (requested === 'readonly') renderMode('readonly');
    try {
      if (!token && requested === 'execute') throw new Error('请先填写本地配对码并读取账户');
      show(requested === 'execute' ? '正在自动识别账户并准备执行模式…' : '正在撤销执行权限…');
      const result = await background({ type: 'control-mode', tabId: tab.id, mode: requested, token });
      if (requestId !== modeRequest) return;
      renderMode(result.mode);
      show(result.mode === 'execute' ? '执行模式已开启。可直接在工作台选择账户下单或开启策略。' : '已切为只读，后续操作已停止；已提交订单继续核对结果。');
    } catch (error) { if (requestId === modeRequest) { renderMode('readonly'); show(error.message, true); } }
    finally { if (requestId === modeRequest) { changing = false; renderMode(mode); } }
  });
  $('readAccounts').addEventListener('click', async () => {
    try {
      const token = $('token').value.trim();
      if (!token) throw new Error('请先粘贴工作台的本地配对码');
      if (mode === 'execute') throw new Error('请先切回只读再刷新完整账户列表');
      scanActive = true; renderMode(mode);
      $('readProgress').textContent = '正在连接页面并读取账户…';
      const result = await background({ type: 'observe-start', tabId: tab.id, token });
      if (!result?.started) throw new Error('未能启动读取，请检测页面连接');
      show('账户正在后台读取。完成后自动出现在工作台，无需新增或绑定账户。');
    } catch (error) { scanActive = false; renderMode(mode); $('readProgress').textContent = error.message; $('readProgress').className = 'hint error'; show(error.message, true); }
  });
  $('connectPage').addEventListener('click', async () => {
    $('connectPage').disabled = true;
    try { const response = await background({ type: 'page-command', tabId: tab.id, message: { type: 'bridge-ping' } }); if (!response?.ok) throw new Error(response?.error || '页面未连接'); show(`页面脚本已连接 · ${response.version}`); }
    catch (error) { show(error.message, true); }
    finally { $('connectPage').disabled = false; }
  });
  setInterval(() => {
    if (scanActive && observationStatus?.time && Date.now() - Date.parse(observationStatus.time) > 65000) {
      scanActive = false; renderMode(mode); $('readProgress').textContent = '读取进度超时，请检查页面后重新读取。'; $('readProgress').className = 'hint error';
    }
  }, 1000);
  initialized = true;
  renderMode(mode);
}
init().catch(error => show(error.message, true));
