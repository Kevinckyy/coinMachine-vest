"""Payout breakdown and ideal, zero-cost continuation values of valid accounts."""
def coefficients(config):
    d=config['max_drawdown'];share=config['profit_split_percent']/100
    live=share;phase2=share*d/(d+config['phase2_goal'])
    return {'live':live,'2/2':phase2,'1/2':phase2*d/(d+config['phase1_goal'])}


def account_value(config,phase,balance,status='active'):
    if status=='failed':return 0.
    if status=='waiting_live':phase='live'
    return coefficients(config)[phase]*max(0.,balance-(config['initial_capital']-config['max_drawdown']))


def populate_metrics(result,remaining=None):
    r={**result,'metrics':dict(result['metrics'])};m=r['metrics'];c=r['config'];s=r.get('settlement') or {}
    count=m.get('total_accounts',c['account_count'])
    if count=='infinity':count=m.get('started_accounts',0)
    paid=s.get('payout_at_cutoff',m.get('total_payout',0))
    pending=s.get('pending_at_cutoff',m.get('pending_payout',0))
    if remaining is None:remaining=m.get('remaining_expectancy')
    m.update(remaining_expectancy=round(remaining,4) if remaining is not None else None,
        avg_credited_payout_per_account=round(paid/count,8) if count else 0.,
        avg_pending_payout_per_account=round(pending/count,8) if count else 0.,
        avg_remaining_expectancy_per_account=round(remaining/count,8) if count and remaining is not None else None,
        avg_total_expectancy_per_account=round((paid+pending+remaining)/count,8) if count and remaining is not None else None)
    r['expectancy']=dict(basis='fair_zero_cost_continuation',complete=remaining is not None,
        note='剩余期望按零成本、公平价格、无限生命周期模型估算；不属于已申请分润，不计入实际出金或现金曲线')
    return r


def from_accounts(result,accounts):
    if result['metrics'].get('remaining_expectancy') is not None:return populate_metrics(result)
    c=result['config'];m=result['metrics'];count=0;remaining=0.
    for a in accounts:
        count+=1;remaining+=account_value(c,a['phase'],a['balance'],a['status'])
    expected=m.get('started_accounts',result.get('detail_counts',{}).get('accounts'))
    if expected is None and c['account_count']!='infinity':expected=c['account_count']-m.get('unstarted_accounts',0)
    if expected is None or count!=expected:return populate_metrics(result)
    remaining+=m.get('unstarted_accounts',0)*account_value(c,c.get('initial_phase','1/2'),c['initial_capital'])
    return populate_metrics(result,remaining)
