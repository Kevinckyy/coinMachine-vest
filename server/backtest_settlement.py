"""Reconstruct final settlement from archived claims without replaying trades."""
from collections import defaultdict
import math

POLICY='settle_submitted_claims'


def settle_archived(result,claims):
    if (result.get('settlement') or {}).get('policy')==POLICY:return result
    r={**result,'metrics':dict(result['metrics'])};m=r['metrics'];cfg=r['config']
    curve=list(result.get('curve',[]));dataset=result.get('dataset') or {}
    end=dataset.get('last_time')
    end=end+cfg.get('interval',60) if end is not None else curve[-1]['time'] if curve else cfg.get('to')
    if end is None:
        r['settlement']=dict(policy='trading_cutoff_only',reason='历史记录缺少交易截止时间，需重新回测')
        r['fee_comparison_settlement_complete']=False
        return r
    late=defaultdict(float);first_credit={};records=0
    for claim in claims:
        records+=1;amount=claim['amount'];ts=claim['credit_at']
        if amount>0:first_credit[claim['account_id']]=min(first_credit.get(claim['account_id'],ts),ts)
        if ts>end:late[ts]+=amount
    pending=m.get('pending_payout',0);expected=result.get('detail_counts',{}).get('claims',records)
    complete=math.isclose(sum(late.values()),pending,abs_tol=.00011) and (records>=expected or pending==0)
    if not complete:
        r['settlement']=dict(policy='trading_cutoff_only',reason='历史分润记录不完整，需重新回测以结算全部申请')
        r['fee_comparison_settlement_complete']=False
        return r
    old_paid=m.get('total_payout',0);old_paid_accounts=m.get('paid_accounts',0);paid=old_paid
    cost=m.get('account_purchase_cost',0);pnl=m.get('total_pnl',0)
    for ts,amount in sorted(late.items()):
        paid+=amount
        if curve:curve.append(dict(time=ts,pnl=pnl,payout=round(paid,4),cost=cost,cash=round(paid-cost,4)))
    count=m.get('total_accounts',cfg['account_count'])
    if count=='infinity':count=m.get('started_accounts',len(result.get('accounts',[])))
    paid_accounts=old_paid_accounts+sum(ts>end for ts in first_credit.values())
    m.update(total_payout=round(paid,4),pending_payout=0,pending_payout_at_cutoff=pending,
        settled_after_cutoff=round(paid-old_paid,4),paid_accounts=paid_accounts,payout_rate=paid_accounts/count*100 if count else 0,
        avg_payout_per_account=round(paid/count,4) if count else 0,net_cash=round(paid-cost,4))
    r['curve']=curve;r['settlement']=dict(policy=POLICY,trading_end=end,settlement_end=max(late,default=end),
        payout_at_cutoff=old_paid,pending_at_cutoff=pending,paid_accounts_at_cutoff=old_paid_accounts,reconstructed=True)
    # Older paired runs saved only the fee-free curve, not its pending claims.
    # Their post-cutoff counterpart cannot be inferred from the actual run.
    r['fee_comparison_settlement_complete']=cfg.get('fee_bps',0)==0
    if cfg.get('fee_bps',0)==0:r['cash_curve_without_fees']=curve
    return r
