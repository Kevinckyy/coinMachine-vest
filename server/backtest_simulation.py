"""Event-driven account simulation with lazy pools and streamed detail records."""
import hashlib
import heapq
import json
import math
import time

from .backtesting import PHASES,RULE_SOURCES,ny_day,validate_config
from .backtest_runtime import UnusedAccounts,DetailPreview
from .engine import ValidationError
from .strategies import require_strategy,choose_batch,choose_side
from .backtest_random import create_stream


def simulate(raw,bars,instrument,progress=lambda d:None,*,detail_sink=None):
    c=validate_config(raw)
    if not bars:raise ValidationError('选定范围内没有本地K线，请先拉取行情')
    if len(bars)<2:raise ValidationError('回测至少需要两根已完成K线')
    if any(bars[i]['time']<=bars[i-1]['time'] for i in range(1,len(bars))):raise ValidationError('回测K线必须按时间严格递增且不重复')
    if instrument.get('symbol')!=c['symbol'] or instrument.get('root')!=c['root']:raise ValidationError('历史数据元信息与回测合约不符')
    unlimited=c['account_count']=='infinity';initial_phase=PHASES.index(c['initial_phase'])
    unused=UnusedAccounts(0 if unlimited else c['account_count'])
    pv=instrument['point_value'];tick=instrument['tick_size'];rate=c['fee_bps']/10000
    capital=c['initial_capital'];interval=c['interval'];rng=create_stream(c)
    # USD is a fixed whole-order cash cost per side. Keep price rounding and
    # position sizing independent of quantity-dependent price impact.
    fixed_slippage=c['slippage_usd'] if c['slippage_mode']=='usd' else 0.
    active={};positions={};ready=[];ready_index={};pending=[];details=DetailPreview(detail_sink)
    totals=dict(trades=0,wins=0,pnl=0.,fees=0.,fixed_slippage=0.,funding=0.,paid=0.,pending=0.,paid_accounts=0,failed=0,passed=0,funded=0,skipped=0,created=0,claims=0)
    stages={phase:dict(accounts=0,passed=0,failed=0,trades=0,closed=0,holding=0.,pnl=0.) for phase in PHASES}
    if not unlimited:stages[c['initial_phase']]['accounts']=c['account_count']
    if not unlimited and initial_phase==2:totals['funded']=c['account_count']
    curve=[];batch=[];queue=[];next_order=bars[0]['time'];next_batch=next_order;last_day=None;last_progress=time.monotonic()

    def phase_record(now):return dict(entered_at=now,trades=0,closed=0,holding=0,pnl=0,passed=False)

    def account_row(a):
        return dict(id=a['id'],phase=PHASES[a['phase']],status=a['status'],balance=round(a['balance'],4),
            pnl=round(a['pnl'],4),payout=round(a['paid'],4),pending_payout=round(a['pending'],4),phase_records=a['phase_records'])

    def record_account(a):details.record('accounts',account_row(a))

    def create(aid,now):
        a=dict(id=aid,phase=initial_phase,balance=capital,status='active',position=None,pnl=0.,paid=0.,pending=0.,
            last_entry={},used=set(),lock_day=None,risk_day=None,daily_floor=None,phase_records={c['initial_phase']:phase_record(now)})
        active[aid]=a;totals['created']+=1
        if unlimited:stages[c['initial_phase']]['accounts']+=1
        if initial_phase==2 and unlimited:totals['funded']+=1
        return a

    def eligible(a,day):
        return a['status']=='active' and not a['position'] and a['lock_day']!=day and a['last_entry'].get(a['phase'])!=day and (c['repeat_daily'] or a['phase'] not in a['used'])

    def offer(a,day):
        if eligible(a,day) and a['id'] not in ready_index:
            ready_index[a['id']]=len(ready);ready.append(a['id'])

    def remove_ready(indices):
        for index in sorted(indices,reverse=True):
            aid=ready[index];last=ready[-1];ready[index]=last;ready_index[last]=index
            ready.pop();del ready_index[aid]

    def retire(a):
        if a['status']=='active' and not c['repeat_daily'] and not a['position'] and a['phase'] in a['used']:
            a['status']='exhausted'
        if a['status']!='active':active.pop(a['id'],None)

    def reset_risk(a,risk_day):
        if a['risk_day']!=risk_day:
            a['risk_day']=risk_day;a['daily_floor']=a['balance']*(1-c['daily_loss_percent']/100) if c['daily_loss_percent'] else None

    def fill(price,direction):
        value=price*(1+direction*c['slippage_bps']/10000) if c['slippage_mode']=='bps' else price+direction*c['slippage_ticks']*tick if c['slippage_mode']=='ticks' else price
        if value<=0:raise ValidationError('滑点假设使成交价非正，请检查费用参数')
        return (math.ceil(value/tick-1e-10) if direction>0 else math.floor(value/tick+1e-10))*tick

    def paid_through(ts):
        while pending and pending[0][0]<=ts:
            _,_,a,claim=heapq.heappop(pending);amount=claim['amount']
            if amount>0 and a['paid']==0:totals['paid_accounts']+=1
            a['paid']+=amount;a['pending']=max(0.,a['pending']-amount)
            totals['paid']+=amount;totals['pending']=max(0.,totals['pending']-amount)
            claim['status']='completed';details.record('claims',claim);record_account(a)

    def close(a,raw_price,ts,reason):
        p=a['position'];exit_price=fill(raw_price,-p['direction'])
        gross=(exit_price-p['entry_price'])*p['quantity']*pv*p['direction'];fee=abs(exit_price*p['quantity']*pv)*rate
        pnl=gross-p['entry_fee']-fee-p['funding']-p['entry_slippage']-fixed_slippage;a['balance']+=gross-fee-fixed_slippage;a['pnl']+=pnl
        totals['trades']+=1;totals['wins']+=pnl>0;totals['pnl']+=pnl;totals['fees']+=fee;totals['fixed_slippage']+=fixed_slippage
        phase=PHASES[a['phase']];record=a['phase_records'][phase];held=max(0,ts-p['opened_at']);stage=stages[phase]
        for r in (record,stage):r['closed']+=1;r['holding']+=held;r['pnl']+=pnl
        details.record('trades',dict(id=totals['trades'],account_id=a['id'],phase=phase,symbol=c['symbol'],side='buy' if p['direction']>0 else 'sell',quantity=p['quantity'],
            opened_at=p['opened_at'],closed_at=ts,entry_price=p['entry_price'],exit_price=exit_price,gross_pnl=round(gross,8),fees=round(p['entry_fee']+fee,8),
            slippage_mode=c['slippage_mode'],fixed_slippage_usd=round(p['entry_slippage']+fixed_slippage,8),
            funding=round(p['funding'],8),pnl=round(pnl,8),stop_loss=p['stop_loss'],remaining_drawdown=p['remaining_drawdown'],reason=reason,holding_seconds=held))
        a['position']=None;positions.pop(a['id'],None);a['lock_day']=ny_day(ts,c['strategy_reset_hour'])
        floor=max(capital-c['max_drawdown'],a['daily_floor'] if a['daily_floor'] is not None else -math.inf)
        if reason in ('max_drawdown','daily_loss') or a['balance']<floor-1e-8:
            a['status']='failed';record.update(ended_at=ts,outcome='failed');stage['failed']+=1;totals['failed']+=1
        else:
            goal=c['phase1_goal'] if a['phase']==0 else c['phase2_goal'] if a['phase']==1 else None
            if goal is not None and a['balance']>=capital+goal-1e-6:
                record.update(passed=True,ended_at=ts,outcome='passed');stage['passed']+=1;a['phase']+=1
                a.update(balance=capital,risk_day=None,daily_floor=None,lock_day=None)
                phase=PHASES[a['phase']];a['phase_records'][phase]=phase_record(ts);stages[phase]['accounts']+=1
                if a['phase']==2:totals['passed']+=1;totals['funded']+=1
            elif a['phase']==2 and a['balance']-capital>=c['payout_min_profit']:
                gross_claim=a['balance']-capital;amount=round(gross_claim*c['profit_split_percent']/100,8)
                a['balance']-=gross_claim
                if a['daily_floor'] is not None:a['daily_floor']-=gross_claim
                a['pending']+=amount;totals['pending']+=amount;totals['claims']+=1
                claim=dict(id=totals['claims'],account_id=a['id'],phase='live',requested_at=ts,credit_at=ts+c['payout_delay_hours']*3600,
                    gross_profit=round(gross_claim,8),amount=amount,status='pending')
                heapq.heappush(pending,(claim['credit_at'],claim['id'],a,claim));details.record('claims',claim)
        retire(a);offer(a,ny_day(ts,c['strategy_reset_hour']));record_account(a)

    def target_price(a,p,goal):
        q=p['quantity']*pv;d=p['direction'];needed=capital+goal-a['balance']+fixed_slippage
        exit_fill=(needed+d*q*p['entry_price'])/(q*(d-rate))
        exit_fill=(math.ceil(exit_fill/tick-1e-10) if d>0 else math.floor(exit_fill/tick+1e-10))*tick
        raw_price=exit_fill/(1-d*c['slippage_bps']/10000) if c['slippage_mode']=='bps' else exit_fill+d*c['slippage_ticks']*tick if c['slippage_mode']=='ticks' else exit_fill
        return (math.ceil(raw_price/tick-1e-10) if d>0 else math.floor(raw_price/tick+1e-10))*tick

    for index,b in enumerate(bars):
        now=b['time'];paid_through(now);day=ny_day(now,c['strategy_reset_hour']);risk_day=ny_day(now,c['risk_reset_hour'])
        if day!=last_day:
            ready.clear();ready_index.clear();queued={a['id'] for a in queue}
            for a in active.values():
                if a['id'] not in queued:offer(a,day)
            last_day=day
        for a in positions.values():reset_risk(a,risk_day)
        if batch and not queue and all(a['position'] is None for a in batch):
            batch=[];next_batch=now+rng.uniform(c['batch_delay_min'],c['batch_delay_max'])
        if not batch and now>=next_batch:
            if unlimited:
                for _ in range(max(0,c['batch_size']-len(ready))):
                    a=create(totals['created']+1,now);offer(a,day)
            available=unused.remaining+len(ready)
            if available>=c['batch_size']:
                chosen=choose_batch(c['strategy_id'],rng,range(available),c['batch_size']);fresh=[];old=[];batch=[]
                for i in chosen:
                    if i<unused.remaining:
                        a=create(unused.value(i),now);fresh.append(i)
                    else:
                        j=i-unused.remaining;a=active[ready[j]];old.append(j)
                    batch.append(a)
                unused.discard(fresh);remove_ready(old);queue=list(batch);next_order=now
        while queue and now>=next_order:
            a=queue.pop(0);reset_risk(a,risk_day)
            direction=1 if choose_side(c['strategy_id'],rng,c['side'])=='buy' else -1;entry=fill(b['open'],direction)
            q=(a['balance']*c['size_percent']/100-fixed_slippage)/(entry*pv*(1/c['leverage']+rate))
            q=math.floor(q) if c['sizing']=='micro_contracts' else math.floor(q*10000)/10000
            a['last_entry'][a['phase']]=day;a['used'].add(a['phase'])
            if q<=0:
                totals['skipped']+=1;retire(a);record_account(a)
            else:
                remaining=a['balance']-(capital-c['max_drawdown'])
                stop=None if c['skip_stop_loss_on_low_drawdown'] and remaining<c['drawdown_stop_loss_threshold'] else c['stop_loss']
                fee=entry*q*pv*rate;a['balance']-=fee+fixed_slippage;totals['fees']+=fee;totals['fixed_slippage']+=fixed_slippage
                a['position']=dict(entry_price=entry,quantity=q,direction=direction,entry_fee=fee,entry_slippage=fixed_slippage,funding=0.,opened_at=now,stop_loss=stop,remaining_drawdown=remaining)
                positions[a['id']]=a;a['phase_records'][PHASES[a['phase']]]['trades']+=1;stages[PHASES[a['phase']]]['trades']+=1
            next_order=now+rng.uniform(c['order_delay_min'],c['order_delay_max'])
        for a in tuple(positions.values()):
            p=a['position'];q=p['quantity']*pv;d=p['direction'];adverse=b['low'] if d>0 else b['high'];favorable=b['high'] if d>0 else b['low']
            def accrue(duration):
                funding=q*b['open']*c['funding_bps_hourly']/10000*duration/3600*d
                a['balance']-=funding;p['funding']+=funding;totals['funding']+=funding
            gap=0 if p['opened_at']==now else max(0,now-bars[index-1]['time']-interval);accrue(gap)
            opening_equity=a['balance']+(b['open']-p['entry_price'])*q*d
            breached=[(f,r) for f,r in [(capital-c['max_drawdown'],'max_drawdown'),(a['daily_floor'],'daily_loss')] if f is not None and opening_equity<f-1e-8]
            if breached:close(a,b['open'],now,max(breached)[1]);continue
            if p['stop_loss'] is not None and (b['open']-(p['entry_price']-d*p['stop_loss']/q))*d<=0:close(a,b['open'],now,'stop_loss');continue
            goal=c['phase1_goal'] if a['phase']==0 else c['phase2_goal'] if a['phase']==1 else c['live_profit_target'] or None
            if goal is not None and (b['open']-target_price(a,p,goal))*d>=0:close(a,b['open'],now,'phase_target' if a['phase']<2 else 'payout_target');continue
            accrue(interval);levels=[]
            if p['stop_loss'] is not None:
                price=p['entry_price']-d*p['stop_loss']/q
                if (adverse-price)*d<=0:levels.append((price,'stop_loss'))
            for floor,reason in [(capital-c['max_drawdown'],'max_drawdown'),(a['daily_floor'],'daily_loss')]:
                if floor is not None and a['balance']+(adverse-p['entry_price'])*q*d<floor-1e-8:
                    price=p['entry_price']+d*(floor-a['balance'])/q-d*tick;levels.append((price,reason))
            if levels:
                price,reason=max(levels,key=lambda x:d*x[0]);price=min(price,b['open']) if d>0 else max(price,b['open']);close(a,price,now+interval,reason)
            elif goal is not None:
                price=target_price(a,p,goal)
                if (favorable-price)*d>=0:
                    price=max(price,b['open']) if d>0 else min(price,b['open']);close(a,price,now+interval,'phase_target' if a['phase']<2 else 'payout_target')
        if index%max(1,len(bars)//500)==0:
            cost=c['account_fee']*(totals['created'] if unlimited else c['account_count'])
            curve.append(dict(time=now,pnl=round(totals['pnl'],4),payout=round(totals['paid'],4),cost=cost,cash=round(totals['paid']-cost,4)))
        if index%1000==0 or time.monotonic()-last_progress>=.5:
            progress(dict(completed=index+1,total=len(bars),trades=totals['trades'],accounts=totals['created'],active_positions=len(positions)));last_progress=time.monotonic()
    end=bars[-1]['time']+interval
    for a in tuple(positions.values()):close(a,bars[-1]['close'],end,'end_of_data')
    paid_through(end)
    payout_at_cutoff=totals['paid'];pending_at_cutoff=totals['pending'];paid_at_cutoff=totals['paid_accounts']
    cost=c['account_fee']*(totals['created'] if unlimited else c['account_count'])
    def cash_point(ts):
        return dict(time=ts,pnl=round(totals['pnl'],4),payout=round(totals['paid'],4),cost=cost,cash=round(totals['paid']-cost,4))
    curve.append(cash_point(end));settlement_end=end;settlement_events=0
    # Trading has stopped. Only process the existing credit queue; no candles,
    # accounts, orders, funding or trading PnL are created during this tail.
    while pending:
        settlement_end=pending[0][0];paid_through(settlement_end);curve.append(cash_point(settlement_end));settlement_events+=1
        if settlement_events%1000==0 or time.monotonic()-last_progress>=.5:
            progress(dict(completed=len(bars),total=len(bars),trades=totals['trades'],accounts=totals['created'],active_positions=0,stage='settling'))
            last_progress=time.monotonic()
    for a in active.values():record_account(a)
    count=totals['created'] if unlimited else c['account_count'];examined=count if initial_phase<2 else 0
    stage_metrics=[]
    for phase,r in stages.items():
        stage_metrics.append(dict(phase=phase,accounts=r['accounts'],passed=r['passed'],failed=r['failed'],trades=r['trades'],
            avg_trades=r['trades']/r['accounts'] if r['accounts'] else None,avg_holding_seconds=r['holding']/r['closed'] if r['closed'] else None,
            closed_trades=r['closed'],total_holding_seconds=r['holding'],pnl=round(r['pnl'],4)))
    cost=c['account_fee']*count
    metrics=dict(total_trades=totals['trades'],win_rate=totals['wins']/totals['trades']*100 if totals['trades'] else None,total_pnl=round(totals['pnl'],4),
        exam_pass_rate=totals['passed']/examined*100 if examined else None,passed_accounts=totals['passed'],funded_accounts=totals['funded'],
        payout_rate=totals['paid_accounts']/count*100 if count else 0,total_payout=round(totals['paid'],4),avg_payout_per_account=round(totals['paid']/count,4) if count else 0,
        pending_payout=round(totals['pending'],4),paid_accounts=totals['paid_accounts'],failed_accounts=totals['failed'],total_fees=round(totals['fees'],4),total_fixed_slippage_usd=round(totals['fixed_slippage'],4),total_funding=round(totals['funding'],4),
        pending_payout_at_cutoff=round(pending_at_cutoff,4),settled_after_cutoff=round(totals['paid']-payout_at_cutoff,4),
        account_purchase_cost=c['account_fee']*count,net_cash=round(totals['paid']-c['account_fee']*count,4),skipped_insufficient_margin=totals['skipped'],
        total_accounts=count,started_accounts=totals['created'],unstarted_accounts=unused.remaining,
        avg_trades_per_account=totals['trades']/count if count else None,
        total_holding_seconds=sum(r['holding'] for r in stages.values()),closed_trades=totals['trades'],
        avg_holding_seconds=sum(r['holding'] for r in stages.values())/totals['trades'] if totals['trades'] else None)
    progress(dict(completed=len(bars),total=len(bars),trades=totals['trades'],accounts=totals['created'],active_positions=0))
    return dict(config=c,simulation_version=6,strategy_version=require_strategy(c['strategy_id'])['version'],metrics=metrics,stages=stage_metrics,curve=curve,
        randomness=dict(algorithm=c['rng_algorithm'],seed=c['seed'],seed_policy=c['seed_policy'],
            seed_bits=128 if isinstance(c['seed'],str) else 32,sampler_version=1,library_version=getattr(rng,'library_version',None)),
        settlement=dict(policy='settle_submitted_claims',trading_end=end,settlement_end=settlement_end,
            payout_at_cutoff=round(payout_at_cutoff,4),pending_at_cutoff=round(pending_at_cutoff,4),paid_accounts_at_cutoff=paid_at_cutoff),
        trades=details.get('trades'),claims=details.get('claims'),accounts=details.get('accounts'),
        detail_counts=dict(trades=totals['trades'],accounts=totals['created'],claims=totals['claims']),details_streamed=detail_sink is not None,
        dataset=dict(symbol=c['symbol'],root=c['root'],interval=interval,bars=len(bars),first_time=bars[0]['time'],last_time=bars[-1]['time'],
            sha256=hashlib.sha256(json.dumps(bars,sort_keys=True,separators=(',',':')).encode()).hexdigest(),
            gaps=sum(bars[i]['time']-bars[i-1]['time']>interval for i in range(1,len(bars))),point_value=pv,tick_size=tick,
            source=instrument.get('source_description','NT8 本地历史缓存'),continuous=bool(instrument.get('continuous')),
            adjustment=instrument.get('adjustment'),source_url=instrument.get('source_url'),source_commit=instrument.get('source_commit')),
        sources=RULE_SOURCES,assumptions=[
            'CME 月份合约作为 Vest 永续价格代理；不拼接、不补造缺口，不能重建基差、盘口深度或真实排队。',
            '市价单开平仓均扣单边手续费并承受不利滑点；费用和滑点参数及来源随结果保存。',
            'tick 滑点体现在成交价格；USD 滑点按整笔订单每边固定金额单独扣减，开仓预留，平仓及达标计算包含该成本，不随数量放大。',
            '同根K线同时触及止损/风控与盈利目标时先处理不利退出；已知开盘先于高低点，跳空止损按更差开盘价处理。',
            '秒级随机间隔向下一根K线开盘取整；阶段达标按扣除离场费用后的净值确认。',
            '每日重复为可选调度；关闭后同账户每阶段仅一次。开仓额度/风控按 17:00/20:00 ET。∞ 模式仅在不足一批时补充新号，持续到数据结束。',
            'live 出金目标是模拟平仓条件，非平台强制目标；空仓后扣除全部申请利润，分润份额延迟到账，已申请分润按设定延迟结算。',
            '截止后停止交易，仅将已申请分润按原到账时间全部结算；最终出金、出金率和现金曲线包含结算尾段，不等同链上提现。',
            '出金率和单号平均出金以固定池全部账户为分母（包含未参与），∞ 模式以实际生成账户为分母。',
            '各阶段平均单数以进入该阶段的账户为分母，固定池初始阶段包含全部计划账户；持仓时间以已平仓笔数为分母，期末按最后收盘价结算。',
            '资金费采用固定每小时假设、默认 0；详细记录分批写入本地，结果预览最多各 500 条，完整明细可分页查询。'])
