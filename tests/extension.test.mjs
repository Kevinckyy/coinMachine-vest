import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const sandbox = { URL };
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(new URL('../extension/core.js', import.meta.url), 'utf8'), sandbox);
const { parseMoney, positionState, parseSide, sameQuantity, matchesPosition, validateJob, defaults } = sandbox.VestBridgeCore;

test('Live activation notice requires both completed evaluation and explicitly live account text',()=>{
  const parse=sandbox.VestBridgeCore.liveActivationNotice;
  const zh='恭喜 - 您已通过评估阶段。\n您的 $5,000 Funded Account 现已转为实盘。';
  const en="Congratulations - you've passed the Evaluation Phase. Your $10,000 Funded Account is now live.";
  assert.equal(parse('评估通过',zh),true);assert.equal(parse('Evaluation Passed',en),true);
  for(const [title,description] of [['付款完成',zh],['第 1 阶段已完成！',zh],['评估通过','请购买新的 Funded Account'],['Evaluation Passed',en.replace('is now live','may become live')]])assert.equal(parse(title,description),false);
});

test('account scan timeout defaults to three seconds and validates finite numeric settings',()=>{
  const parse=sandbox.VestBridgeCore.accountScanTimeoutSeconds;
  assert.equal(parse(),3);
  for(const value of [0.5,3,7.5,120])assert.equal(parse(value),value);
  for(const value of [0,-1,120.1,NaN,Infinity,null,'3',true])assert.throws(()=>parse(value),/0.5/);
  assert.equal(sandbox.VestBridgeCore.accountScanIdleTimeoutMs(),65000);
  assert.equal(sandbox.VestBridgeCore.accountScanIdleTimeoutMs({account_timeout_seconds:120}),140000);
});

test('terminal notice classification requires an observed closure, not a risk warning or paid retry',()=>{
  const parse=sandbox.VestBridgeCore.terminalNoticeReason;
  assert.equal(parse('您的每日亏损额度已用完','您的账户已触及每日亏损上限，现已关闭。请查看下方交易，或购买新的 Funded Account。'),'daily_loss');
  assert.equal(parse('Your daily loss limit has been reached','Your account has reached its daily loss limit and is now closed.'),'daily_loss');
  assert.equal(parse('Your Daily Loss Ran Out','Your account has reached its Daily Loss Limit and is now closed. Review the trades below, or purchase a new Funded Account.'),'daily_loss');
  assert.equal(parse('Your Drawdown Ran Out','Your account has reached its Drawdown Limit and is now closed.'),'max_drawdown');
  assert.equal(parse('您的回撤额度已用完','您的账户已触及回撤上限，现已关闭。'),'max_drawdown');
  assert.equal(parse('Account closed','Your account has been closed after reaching maximum drawdown.'),'max_drawdown');
  for(const [title,description] of [
    ['Risk warning','Your account may be closed if the daily loss limit is reached.'],
    ['您的每日亏损额度已用完','您的账户即将关闭，请确认风险。'],
    ['Confirm order','Your account has been closed after reaching the daily loss limit.'],
    ['Your daily loss limit has been reached','Your account is still active.'],
    ['Retry - $14.25','Your account has been closed after reaching the daily loss limit.'],
  ]) assert.equal(parse(title,description),null,title);
});

test('strategy stop loss uses a fresh drawdown pair and a strict less-than boundary', () => {
  const decide=sandbox.VestBridgeCore.strategyStopLoss;
  const job={source:'strategy',stop_loss:130,skip_stop_loss_on_low_drawdown:true,drawdown_stop_loss_threshold:200};
  const metric={current:801,limit:1000,percent:null,observed_at:100,phase:'1/2'};
  assert.equal(decide(job,metric,'1/2',100).stop_loss,null);
  assert.equal(decide(job,{...metric,current:800},'1/2',100).stop_loss,130);
  assert.equal(decide(job,{...metric,current:799},'1/2',100).stop_loss,130);
  assert.equal(decide({...job,skip_stop_loss_on_low_drawdown:false},null,'1/2',100).stop_loss,130);
  for(const reading of [null,{...metric,observed_at:94},{...metric,observed_at:101},{...metric,phase:'2/2'},
    {...metric,current:null,limit:null,percent:80},{...metric,update_pending:true},{...metric,current:true}]) {
    assert.throws(()=>decide(job,reading,'1/2',100));
  }
});

test('only verified NQ aliases normalize to the canonical instrument', () => {
  const {canonicalNqPosition}=sandbox.VestBridgeCore;
  assert.equal(canonicalNqPosition('NDX','/trade/NDX-USD-PERP','https://next.vestmarkets.com/trade/NQ-PERP'),'NQ-PERP');
  assert.equal(canonicalNqPosition('NQ','/trade/NQ-PERP','https://next.vestmarkets.com/trade/NQ-PERP'),'NQ-PERP');
  for(const [label,link] of [['ES','/trade/NQ-PERP'],['NQ','/trade/ES-PERP'],['NQ','https://evil.example/trade/NDX-USD-PERP']]) assert.throws(()=>canonicalNqPosition(label,link,'https://next.vestmarkets.com/trade/NQ-PERP'));
});

test('NQ micro input converts to canonical Vest quantity without changing the order size', () => {
  const {nqInputToVest,nqVestToInput}=sandbox.VestBridgeCore;
  assert.equal(nqInputToVest(3,'micro'),6);
  assert.equal(nqVestToInput(6,'micro'),3);
  assert.equal(nqInputToVest(3,'token'),3);
  for(const [qty,unit] of [[0,'micro'],[0.5,'micro'],[1,'usd'],[1,'mini'],[NaN,'micro']]) assert.throws(()=>nqInputToVest(qty,unit));
  assert.throws(()=>nqVestToInput(1,'micro'),'partial micros must never be silently rounded');
});

test('capital indicators preserve used / limit pairs and percentage-only readings', () => {
  const parse = sandbox.VestBridgeCore.parseCapitalMetric;
  for (const [text,current,limit] of [['$12.50 / $130.00',12.5,130],['+$0.00 / $1,000.00',0,1000],['1.25 USDC / 250.5 USDC',1.25,250.5]]) {
    const value=parse(text);assert.equal(value.current,current);assert.equal(value.limit,limit);assert.equal(value.percent,null);
  }
  assert.equal(parse('3%').percent,3);
  assert.equal(parse('3%').limit,null);
  for (const value of ['—','****','0 / —','12 / 100 / 200','10% / 20%','20 / -1','1K / 2K','1.2.3 / 100']) assert.equal(parse(value),null,String(value));
});

test('primary accounts are excluded from both opening and closing even with valid permissions', () => {
  const { executionExcluded } = sandbox.VestBridgeCore;
  for (const account of [{name:'主账户'},{name:'Main Account'},{category:'default'},{execution_excluded:true,name:'Renamed'}]) {
    assert.equal(executionExcluded(account),true);
    for (const type of ['open','close']) assert.throws(()=>validateJob(
      {id:'old-job',account_id:'main',type,symbol:'NQ-PERP',side:'buy',quantity:1,stop_loss:130},
      {accountId:'main',accountName:account.name,accountCategory:account.category,execution_excluded:account.execution_excluded}),/永久排除/);
  }
  assert.equal(executionExcluded({name:'Main Account 2',category:'eval'}),false);
  assert.equal(executionExcluded({name:'主账户 2',category:'eval'}),false);
});

test('evaluation status requires an explicit unambiguous platform badge', () => {
  const parse = sandbox.VestBridgeCore.accountCategory;
  assert.equal(parse(['Passed']),'passed');
  assert.equal(parse(['已通过']),'passed');
  assert.equal(parse(['Evaluation']),'eval');
  assert.equal(parse(['Funded']),'funded');
  assert.equal(parse(['Evaluation','Passed']),null);
  assert.equal(parse(['Account Passed 12']),null);
  assert.equal(parse(['Trading']),null);
});

test('explicit failure badges override the evaluation phase and failed accounts never execute', () => {
  const {accountCategory,assertExecutionAllowed}=sandbox.VestBridgeCore;
  for(const labels of [['Evaluation','Failed'],['评估失败'],['Funded Account Liquidated']]) assert.equal(accountCategory(labels),'failed');
  assert.equal(accountCategory(['Passed','Failed']),null);
  assert.equal(accountCategory(['Failed Account 12']),null);
  for(const labels of [['Closed'],['已关闭'],['已關閉'],['Evaluation','Closed']]) assert.equal(accountCategory(labels),'closed');
  assert.equal(accountCategory(['Passed','Closed']),null);
  assert.equal(accountCategory(['Account Closed 12']),null);
  assert.equal(accountCategory(['主账户','Closed']),'default');
  assert.throws(()=>assertExecutionAllowed({category:'closed'}));
  for(const account of [{category:'failed'},{retired_failed:true},{archived:true}]) assert.throws(()=>assertExecutionAllowed(account));
});

test('preserves Vest signed currency and grouped financial amounts', () => {
  for (const [input, expected] of [
    ['-$1,234.56', -1234.56], ['+$1,234.56', 1234.56], ['$0.00', 0],
    ['−$87.50', -87.5], ['$-42.25', -42.25], ['($53.10)', -53.1],
    ['25,000.01 USDC', 25000.01], ['浮盈 - $123.45', -123.45],
    [' + 25.50 ', 25.5], [-18.5, -18.5],
  ]) assert.equal(parseMoney(input), expected, input);
});

test('unknown, hidden, abbreviated, percentage or ambiguous values cannot become balances', () => {
  for (const value of [null, undefined, '-', '****', '', '1.2K', '25%', 'NaN', 'Infinity', Infinity, '100 / 200', '12,34', '1.2.3']) {
    assert.equal(parseMoney(value), null, String(value));
  }
});

test('both or neither position markers produce unknown rather than an empty account', () => {
  assert.equal(positionState(true, false), true);
  assert.equal(positionState(false, true), false);
  assert.equal(positionState(false, false), null);
  assert.equal(positionState(true, true), null);
});

test('rejects cross-account, unsupported and invalid live jobs', () => {
  const config = { accountId: 'browser-1' };
  const job = { id: 'job-1', type: 'open', account_id: 'browser-1', symbol: 'NQ-PERP', side: 'buy', quantity: 2, take_profit: 100, stop_loss: 50 };
  assert.equal(validateJob(job, config), job);
  for (const change of [{ account_id: 'other' }, { side: 'random' }, { type: 'transfer' }, { quantity: NaN }, { stop_loss: -1 }, { symbol: 'BTC-PERP' }, { quantity: '2' }]) {
    assert.throws(() => validateJob({ ...job, ...change }, config));
  }
});

test('position mappings use position ids rather than assuming API or display symbols', () => {
  assert.equal(defaults.positionOpen, '[data-testid^="position-symbol-"]');
  assert.equal(defaults.close, '[data-testid^="position-close-"]');
  assert.equal(defaults.entryPrice, '[data-testid^="position-entry-"]');
});

test('percentage jobs accept only the four page presets without inventing a fixed quantity', () => {
  const config={accountId:'pct-account'};
  const job={id:'pct-job',account_id:config.accountId,type:'open',symbol:'NQ-PERP',side:'buy',take_profit:100,stop_loss:50};
  for(const size_percent of [25,50,75,100]) assert.equal(validateJob({...job,size_percent},config).size_percent,size_percent);
  for(const size_percent of [0,1,20,101,'25',null,true,NaN]) assert.throws(()=>validateJob({...job,size_percent},config));
});

test('no-TP jobs require a positive stop loss and accept null take profit', () => {
  const config={accountId:'stop-only'};
  const job={id:'stop-only-job',account_id:config.accountId,type:'open',symbol:'NQ-PERP',side:'buy',size_percent:25,take_profit:null,stop_loss:130};
  assert.equal(validateJob(job,config),job);
  for(const change of [{stop_loss:0},{stop_loss:null},{stop_loss:-130},{take_profit:0},{take_profit:-1}]) assert.throws(()=>validateJob({...job,...change},config));
});

test('recognizes only verified English and Chinese position direction labels', () => {
  for (const [label, side] of [['Long 10x','buy'], ['Short 50x','sell'], ['多单 10x','buy'], ['空单 50x','sell']]) {
    assert.equal(parseSide(label), side, label);
  }
  for (const label of ['Shortly 2x', 'Longer', 'Buy / Long', '', null]) assert.equal(parseSide(label), null);
});

test('position confirmation requires matching id, market, direction and actual filled quantity', () => {
  const actual = { position_open: true, position_id: 'vest-position-123', symbol: 'NQ-PERP', side: 'buy', quantity: 2 };
  assert.equal(matchesPosition(actual, actual), true);
  for (const change of [{ position_id: 'different' }, { position_id: null }, { symbol: 'BTC-PERP' },
    { side: 'sell' }, { quantity: 1.5 }, { quantity: NaN }, { quantity: '2' }, { position_open: false }]) {
    assert.equal(matchesPosition({ ...actual, ...change }, actual), false, JSON.stringify(change));
  }
  assert.equal(sameQuantity(2, 2.0000000001), true);
  assert.equal(sameQuantity(0, 0), false);
  assert.equal(parseMoney('Close 2.0000 NQ'), 2);
  assert.equal(parseMoney('平仓 2.0000 NQ'), 2);
});


test('explicit account phase badges recognize three independent phases without guessing names or percentages',()=>{
  const parse=sandbox.VestBridgeCore.accountPhase;
  for(const [label,phase] of [['1/2','1/2'],['Step 1/2','1/2'],['Phase 2 of 2','2/2'],['第 2/2 阶段','2/2'],['Live','live'],['Funded Account','live'],['资金账户','live']]) assert.equal(parse([label]),phase,label);
  for(const labels of [['Evaluation'],['Account 1/2'],['50%'],['Passed'],['1/2','2/2'],['2/2','Live']]) assert.equal(parse(labels),null,labels.join(','));
});

test('a job from another account phase cannot execute even if the account id is equal',()=>{
  const job={id:'phase-job',account_id:'phase-account',phase:'1/2',type:'open',symbol:'NQ-PERP',side:'buy',quantity:1,stop_loss:130};
  assert.throws(()=>validateJob(job,{accountId:'phase-account',phase:'2/2'}),/阶段/);
  assert.equal(validateJob({...job,phase:'2/2'},{accountId:'phase-account',phase:'2/2'}).phase,'2/2');
});

test('passed account stage is read from explicit goal labels and conflicting evidence stays unknown',()=>{
  const parse=sandbox.VestBridgeCore.accountPhase;
  for(const label of ['第 2 阶段盈利目标: +$251.72 / $250','第2階段盈利目標：+$250 / $250','Step 2 Profit Target: +$250 / $250']){
    assert.equal(parse(['评估已通过',label]),'2/2');
    assert.equal(parse(['1/2',label]),null);
    assert.equal(parse(['Live',label]),null);
  }
  assert.equal(parse(['第 1 阶段盈利目标: $100 / $200']),'1/2');
  for(const label of ['评估已通过','恭喜通过第二阶段','Account 2 Profit Target: 250','第 2 阶段盈利目标可能达到250'])assert.equal(parse([label]),null);
});

test('duplicate native navigation is accepted only for one exact destination',()=>{
  const choose=sandbox.VestBridgeCore.sameDestinationNavigation;
  const first={href:'https://next.vestmarkets.com/trade'};
  assert.equal(choose([first,{...first}]),first);
  assert.equal(choose([]),null);
  assert.equal(choose([first,{href:first.href+'/NQ-PERP'}]),null);
  assert.equal(choose([first,{href:first.href+'?account=other'}]),null);
});
