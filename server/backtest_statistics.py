"""Round-weighted outcomes, pooled trade statistics and cash-flow curves."""
from bisect import bisect_right
from .backtest_batches import MetricSummary

STATISTICS_VERSION=2


def cash_points(result,starts=None,without_fees=False):
    if without_fees:
        if result.get('fee_comparison_settlement_complete') is False:return None
        curve=result.get('cash_curve_without_fees')
        if curve is None and result['config'].get('fee_bps',0)!=0:return None
        curve=curve if curve is not None else result.get('curve',[])
    else:curve=result.get('curve',[])
    if not curve:return None
    if all('cash' in p for p in curve):return [dict(time=p['time'],value=p['cash']) for p in curve]
    count=result['config']['account_count'];fee=result['config'].get('account_fee',0)
    if count=='infinity':
        expected=result['metrics'].get('started_accounts',len(result.get('accounts',[])))
        if starts is None or len(starts)!=expected:return None
        starts=sorted(starts)
    return [dict(time=p['time'],value=round(p['payout']-fee*(bisect_right(starts,p['time']) if count=='infinity' else count),4)) for p in curve]


class CurveSummary:
    def __init__(self):self.count=0;self.deltas={};self.first=None;self.best=None;self.worst=None;self.incompatible=False

    def add(self,result,points):
        if not points:return
        times=[p['time'] for p in points]
        if any(a>=b for a,b in zip(times,times[1:])) or (self.count and times[0]!=self.first):self.incompatible=True;return
        if not self.count:self.first=times[0]
        self.count+=1
        previous=0.
        for p in points:
            self.deltas[p['time']]=self.deltas.get(p['time'],0.)+p['value']-previous;previous=p['value']
        row=dict(seed=result['config']['seed'],round_index=result.get('round_index',0),run_id=result.get('id'),final_cash=points[-1]['value'],points=points)
        if self.best is None or (row['final_cash'],-row['round_index'])>(self.best['final_cash'],-self.best['round_index']):self.best=row
        if self.worst is None or (row['final_cash'],row['round_index'])<(self.worst['final_cash'],self.worst['round_index']):self.worst=row

    def result(self,total):
        available=self.count==total and total>0 and not self.incompatible
        times=sorted(self.deltas);average=[];value=0.
        if available:
            for t in times:
                value+=self.deltas[t];average.append(dict(time=t,value=round(value/self.count,6)))
        def align(row):
            if not available:return None
            points=row['points'];i=0;aligned=[]
            for t in times:
                while i+1<len(points) and points[i+1]['time']<=t:i+=1
                aligned.append(dict(time=t,value=points[i]['value']))
            return {**row,'points':aligned}
        return dict(available=available,rounds=self.count,best=align(self.best),worst=align(self.worst),average=average,
            reason='' if available else '完成至少一轮后显示资金曲线' if not total else '历史轮次缺少该口径的资金曲线或时间轴不一致，请重新回测')


class BatchStatistics:
    def __init__(self):
        self.metrics=MetricSummary();self.count=0;self.accounts=0;self.trades=0;self.closed=0;self.holding=0.;self.stages={}
        self.with_fees=CurveSummary();self.without_fees=CurveSummary();self.dataset=None
        self.settled_rounds=0;self.trading_end=None;self.settlement_end=None

    def add_result(self,result,starts=None):
        if self.dataset is None:self.dataset=result.get('dataset')
        settlement=result.get('settlement') or {}
        if settlement.get('policy')=='settle_submitted_claims':self.settled_rounds+=1
        for key in ('trading_end','settlement_end'):
            if settlement.get(key) is not None:setattr(self,key,max(getattr(self,key) or 0,settlement[key]))
        metrics=dict(result['metrics']);count=metrics.get('total_accounts')
        if count is None:count=result['config']['account_count']
        if count=='infinity':count=metrics.get('started_accounts',len(result.get('accounts',[])))
        trades=metrics['total_trades'];closed=metrics.get('closed_trades',trades)
        holding=0.
        for stage in result.get('stages',[]):
            n=stage.get('closed_trades',stage['trades']);held=stage.get('total_holding_seconds')
            if held is None:held=(stage.get('avg_holding_seconds') or 0)*n
            holding+=held
            pool=self.stages.setdefault(stage['phase'],dict(phase=stage['phase'],accounts=0,trades=0,closed_trades=0,total_holding_seconds=0.))
            pool['accounts']+=stage['accounts'];pool['trades']+=stage['trades'];pool['closed_trades']+=n;pool['total_holding_seconds']+=held
        holding=metrics.get('total_holding_seconds',holding)
        metrics.update(avg_trades_per_account=trades/count if count else None,avg_holding_seconds=holding/closed if closed else None)
        self.metrics.add(metrics);self.count+=1;self.accounts+=count;self.trades+=trades;self.closed+=closed;self.holding+=holding
        self.with_fees.add(result,cash_points(result,starts));self.without_fees.add(result,cash_points(result,starts,True))

    def result(self):
        phases=[]
        for phase in ('1/2','2/2','live'):
            r=self.stages.get(phase,dict(phase=phase,accounts=0,trades=0,closed_trades=0,total_holding_seconds=0.))
            phases.append({**r,'avg_trades':r['trades']/r['accounts'] if r['accounts'] else None,
                'avg_holding_seconds':r['total_holding_seconds']/r['closed_trades'] if r['closed_trades'] else None})
        return dict(statistics_version=STATISTICS_VERSION,metrics=self.metrics.result(),dataset=self.dataset,
            settlement=dict(settled_rounds=self.settled_rounds,legacy_rounds=self.count-self.settled_rounds,
                trading_end=self.trading_end,settlement_end=self.settlement_end),
            trading=dict(total_accounts=self.accounts,total_trades=self.trades,closed_trades=self.closed,total_holding_seconds=self.holding,
                avg_trades_per_account=self.trades/self.accounts if self.accounts else None,avg_holding_seconds=self.holding/self.closed if self.closed else None,stages=phases),
            cash_curves=dict(with_fees=self.with_fees.result(self.count),without_fees=self.without_fees.result(self.count)))
