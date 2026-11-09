(function (root) {
  'use strict';
  const FIELDS = [
    ['identity', '账户标识', '[data-testid="account-selector-trigger"]'],
    ['balance', '账户净值', '[data-testid="account-value"]'],
    ['unrealized', '未实现盈亏', '[data-testid="account-upnl"]'],
    ['price', '盘口中间价', '[data-testid="book-mark"]'],
    ['quantity', '下单数量输入', '[data-testid="size-input"]'],
    ['unit', '数量单位标识', '[data-testid="size-unit-select"]'],
    ['takeProfit', '止盈金额输入（USDC）', '[data-testid="tpsl-input-tp"]'],
    ['stopLoss', '止损金额输入（USDC）', '[data-testid="tpsl-input-sl"]'],
    ['buy', '做多按钮', '[data-testid="submit-long"]'],
    ['sell', '做空按钮', '[data-testid="submit-short"]'],
    ['submit', '风险警告按钮（仅检测，禁止自动确认）', '[data-testid="confirm-order-submit"]'],
    ['positionOpen', '仅持仓时出现的标记', '[data-testid^="position-symbol-"]'],
    ['positionEmpty', '仅空仓时出现的标记', 'text:No positions yet||未找到持仓'],
    ['entryPrice', '持仓成交均价', '[data-testid^="position-entry-"]'],
    ['positionSymbol', '持仓交易对', '[data-testid^="position-symbol-"]'],
    ['positionSide', '持仓方向', '[data-testid^="position-side-"]'],
    ['positionValue', '持仓价值（用于定位相邻数量）', '[data-testid^="position-value-"]'],
    ['close', '平仓按钮', '[data-testid^="position-close-"]'],
    ['closeConfirm', '最终确认平仓按钮（可选）', '[data-testid="close-submit"]']
  ];
  const defaults = Object.fromEntries(FIELDS.map(([key, , selector]) => [key, selector]));
  function parseMoney(value) {
    if (typeof value === 'number') return Number.isFinite(value) ? value : null;
    if (typeof value !== 'string') return null;
    let text = value.trim().replace(/[\u2212\u2013]/g, '-').replace(/\u00a0/g, ' ');
    const parts = text.match(/[+-]?\s*(?:\$\s*)?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?/g);
    if (!parts || parts.length !== 1) return null;
    const trailing = text.slice(text.indexOf(parts[0]) + parts[0].length).trim();
    if (/^[kmb%]/i.test(trailing)) return null;
    const number = Number(parts[0].replace(/[$,\s]/g, ''));
    return Number.isFinite(number) ? (text.includes('(') && text.includes(')') ? -Math.abs(number) : number) : null;
  }
  const normalized = (value) => String(value ?? '').replace(/\s+/g, ' ').trim();
  const isNqTradePath = path => ['/trade/NQ-PERP','/trade/NDX-USD-PERP'].includes(String(path).replace(/\/$/,''));
  function canonicalNqPosition(label, href, baseUrl) {
    if (!/^(?:NQ|NDX|NAS100)(?:-PERP)?$|^NDX-USD-PERP$/i.test(normalized(label))) throw new Error('发现非 NQ 或无法识别的持仓品种');
    if (href) {
      const link=new URL(href,baseUrl);
      if (link.username || link.password || ![new URL(baseUrl).origin,'https://next.vestmarkets.com'].includes(link.origin) || !isNqTradePath(link.pathname)) throw new Error('持仓品种链接与 NQ-PERP 不符');
    }
    return 'NQ-PERP';
  }
  const CAPITAL_INDICATORS = [
    { key:'daily_loss', testId:'capital-indicator-daily-loss', labels:['Max. Daily Loss','Max Daily Loss','最大日亏损','最大日虧損'] },
    { key:'drawdown', testId:'capital-indicator-drawdown', labels:['Max. Drawdown','Max Drawdown','最大回撤'] },
    { key:'goal', testId:'capital-indicator-goal', labels:['Step Goal','Evaluation Goal','阶段目标','階段目標','评估目标','評估目標'] },
  ];
  function parseCapitalMetric(value) {
    const text = normalized(value);
    const percent = text.match(/^(\d+(?:\.\d+)?)\s*%$/);
    if (percent && Number.isFinite(Number(percent[1]))) return { text, current:null, limit:null, percent:Number(percent[1]) };
    const parts = text.split('/').map(normalized);
    const currency = /^[+-]?\s*\$?\s*(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?\s*(?:USD[CT]?)?$/i;
    if (parts.length !== 2 || parts.some(part => !currency.test(part))) return null;
    const [current, limit] = parts.map(parseMoney);
    if (current === null || limit === null || limit < 0) return null;
    return { text, current, limit, percent:null };
  }
  function accountCategory(labels) {
    const states = { 'passed':'passed','已通过':'passed','通过':'passed',
      'failed':'failed','已失败':'failed','失败':'failed','evaluation failed':'failed','评估失败':'failed',
      'funded account liquidated':'failed','资金账户清算':'failed','evaluation':'eval','评估':'eval','考核':'eval',
      'closed':'closed','account closed':'closed','已关闭':'closed','已關閉':'closed','账户已关闭':'closed',
      'liquidated':'failed','已清算':'failed','已强平':'failed','爆仓':'failed',
      'funded':'funded','资金账户':'funded','default':'default','main':'default',
      'primary':'default','master':'default','默认账户':'default','主账户':'default','主账号':'default','main account':'default' };
    const found = [...new Set(labels.map(label => states[normalized(label).toLowerCase()]).filter(Boolean))];
    if (found.includes('default')) return 'default';
    if (found.includes('closed') && found.every(state=>['closed','failed','eval','funded'].includes(state))) return 'closed';
    if (found.includes('failed') && found.length===2 && found.some(state=>['eval','funded'].includes(state))) return 'failed';
    return found.length === 1 ? found[0] : null;
  }
  function terminalNoticeReason(title, description) {
    const heading=normalized(title), detail=normalized(description);
    // These are completed account-closure notices, never prospective risk warnings.
    const knownHeading=/^(?:您的每日亏损额度已用完|您的每日虧損額度已用完|您的回撤额度已用完|您已触及每日亏损上限|您的账户已关闭|您的帳戶已關閉|Your (?:daily loss|drawdown) ran out|Your daily loss (?:limit|allowance) (?:has been (?:reached|exceeded|exhausted)|is exhausted)|Daily loss limit (?:reached|exceeded)|Maximum drawdown (?:reached|exceeded)|Account closed|Your account (?:has been|is) closed)$/i;
    if (!knownHeading.test(heading)) return null;
    const closed=/(?:账户|帳戶).{0,80}(?:已关闭|已關閉)|\byour account\b.{0,100}\b(?:has (?:now )?been closed|is (?:now )?closed)\b/i.test(detail);
    if (!closed) return null;
    const text=heading+' '+detail;
    if (/每日(?:亏损|虧損)|daily loss/i.test(text)) return 'daily_loss';
    if (/回撤(?:上限|额度)|maximum drawdown|max[.]? drawdown|drawdown (?:limit|ran out)/i.test(text)) return 'max_drawdown';
    return null;
  }
  function positionState(openVisible, emptyVisible) {
    return openVisible === emptyVisible ? null : openVisible;
  }
  function accountPhase(labels) {
    const phases=labels.map(value=>{
      const text=normalized(value);
      if (/^(?:live(?: funded)?(?: account)?|funded(?: account)?|实盘(?:账户)?|實盤(?:帳戶)?|资金账户|資金帳戶)$/i.test(text)) return 'live';
      const match=text.match(/^(?:(?:step|stage|phase|evaluation)\s*)?([12])\s*(?:\/|of)\s*2$/i) ||
        text.match(/^第\s*([12])\s*\/\s*2\s*(?:阶段|階段)$/);
      return match ? match[1]+'/2' : null;
    }).filter(Boolean);
    const distinct=[...new Set(phases)];
    return distinct.length===1 ? distinct[0] : null;
  }
  function parseSide(value) {
    const text = normalized(value);
    if (/^(?:Long(?:\s|$)|多单(?:\s|\d|$))/i.test(text)) return 'buy';
    if (/^(?:Short(?:\s|$)|空单(?:\s|\d|$))/i.test(text)) return 'sell';
    return null;
  }
  function sameQuantity(actual, expected) {
    return typeof actual === 'number' && typeof expected === 'number' &&
      Number.isFinite(actual) && Number.isFinite(expected) && actual > 0 && expected > 0 &&
      Math.abs(actual - expected) <= Math.max(1e-9, Math.abs(expected) * 1e-9);
  }
  function nqInputToVest(quantity, unit) {
    if (!Number.isFinite(quantity) || quantity<=0) throw new Error('下单数量必须是正数');
    if (unit==='token') return quantity;
    if (unit==='micro' && Number.isInteger(quantity)) return quantity*2;
    throw new Error('NQ 微型合约数量必须为正整数，且数量单位必须明确');
  }
  function nqVestToInput(quantity, unit) {
    const value=unit==='micro' ? quantity/2 : quantity;
    if (!sameQuantity(nqInputToVest(value,unit),quantity)) throw new Error('微型合约数量换算不一致');
    return value;
  }
  function matchesPosition(actual, expected) {
    return actual?.position_open === true && !!actual.position_id &&
      (!expected.position_id || actual.position_id === expected.position_id) &&
      actual.symbol === expected.symbol && actual.side === expected.side &&
      sameQuantity(actual.quantity, expected.quantity);
  }
  function executionExcluded(account) {
    return !!account?.execution_excluded || ['default','main','primary','master'].includes(normalized(account?.category).toLowerCase()) ||
      ['主账户','主账号','主帳戶','主帳號','main account','primary account','master account'].includes(normalized(account?.name).toLowerCase());
  }
  function assertExecutionAllowed(account) {
    if (executionExcluded(account)) throw new Error('主账户永久排除所有执行，仅允许读取账户信息');
    if (account?.retired_failed || account?.archived || ['failed','closed'].includes(normalized(account?.category).toLowerCase())) throw new Error('账户评估失败、已关闭或已归档，禁止执行');
  }
  function validateJob(job, config) {
    assertExecutionAllowed({ name: config.accountName || config.identityText, category: config.accountCategory, execution_excluded: config.execution_excluded });
    assertExecutionAllowed(job);
    if (!job || typeof job.id !== 'string' || !job.id || job.account_id !== config.accountId) throw new Error('任务账户不匹配');
    if (config.phase && job.phase!==config.phase) throw new Error('任务阶段与当前账户阶段不符');
    if (!['open', 'close'].includes(job.type)) throw new Error('不支持的交易动作');
    if (job.type === 'open') {
      if (job.symbol !== 'NQ-PERP') throw new Error('桥接当前仅校准 NQ-PERP');
      if (!['buy', 'sell'].includes(job.side)) throw new Error('交易方向无效');
      if (job.size_percent !== undefined && ![25,50,75,100].includes(job.size_percent)) throw new Error('仓位大小仅支持 25%、50%、75%、100%');
      if (job.skip_stop_loss_on_low_drawdown !== undefined && typeof job.skip_stop_loss_on_low_drawdown !== 'boolean') throw new Error('回撤止损开关无效');
      if (job.skip_stop_loss_on_low_drawdown && (job.source !== 'strategy' || ![25,50,75,100].includes(job.size_percent) ||
          typeof job.drawdown_stop_loss_threshold !== 'number' || !Number.isFinite(job.drawdown_stop_loss_threshold) || job.drawdown_stop_loss_threshold <= 0)) throw new Error('回撤止损规则参数无效');
      for (const key of [...(job.size_percent === undefined ? ['quantity'] : []), ...(job.take_profit == null ? [] : ['take_profit']), 'stop_loss']) {
        if (typeof job[key] !== 'number' || !Number.isFinite(job[key]) || job[key] <= 0) throw new Error('交易参数无效: ' + key);
      }
    }
    return job;
  }
  function strategyStopLoss(job, metric, phase, now = Date.now()/1000) {
    const configured = job.configured_stop_loss ?? job.stop_loss;
    if (!job.skip_stop_loss_on_low_drawdown) return {stop_loss:configured};
    if (job.source !== 'strategy' || typeof configured !== 'number' || !Number.isFinite(configured) || configured <= 0 ||
        typeof job.drawdown_stop_loss_threshold !== 'number' || !Number.isFinite(job.drawdown_stop_loss_threshold) || job.drawdown_stop_loss_threshold <= 0) throw new Error('回撤止损规则参数无效');
    if (!metric || metric.update_pending || (phase && metric.phase !== phase) || metric.percent != null ||
        ![metric.current,metric.limit,metric.observed_at].every(value => typeof value === 'number' && Number.isFinite(value)) ||
        metric.limit <= 0 || now-metric.observed_at < 0 || now-metric.observed_at > 5) throw new Error('剩余最大回撤缺少当前阶段的新鲜金额读数，未提交订单');
    const remaining = Math.round((metric.limit-metric.current)*1e8)/1e8;
    return {stop_loss:remaining < job.drawdown_stop_loss_threshold ? null : configured,remaining_drawdown:remaining,drawdown:metric};
  }
  function liveActivationNotice(title, description) {
    if (!/^(?:评估通过|Evaluation Passed)$/i.test(normalized(title))) return false;
    return /^(?:恭喜\s*-\s*您已通过评估阶段。\s*您的\s*\$[\d,]+(?:\.\d+)?\s*Funded Account\s*现已转为实盘。|Congratulations\s*-\s*you've passed the Evaluation Phase\.\s*Your\s+\$[\d,]+(?:\.\d+)?\s+Funded Account is now live\.)$/i.test(normalized(description));
  }
  root.VestBridgeCore = { FIELDS, defaults, parseMoney, normalized, isNqTradePath, canonicalNqPosition, CAPITAL_INDICATORS, parseCapitalMetric, accountCategory, terminalNoticeReason, liveActivationNotice, accountPhase, positionState, parseSide, sameQuantity, nqInputToVest, nqVestToInput, matchesPosition, executionExcluded, assertExecutionAllowed, validateJob, strategyStopLoss };
})(globalThis);
