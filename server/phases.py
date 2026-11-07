"""Account-local phase ledgers; no guessed phase or execution settlement."""
import copy
import math
from datetime import datetime

PHASES=('1/2','2/2','live')


def phase_rank(value):
    return PHASES.index(value)+1 if value in PHASES else 0


def finite(value):
    return isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value)


class PhaseAccounting:
    PHASE_FIELDS=('initial_balance','balance','equity','unrealized_pnl','realized_pnl',
                  'observed_at','capital_metrics','profit_target','max_loss','status','evaluation_passed_at')

    def _sync_phase_book(self, account):
        phase=account.get('phase')
        if phase not in PHASES:
            return
        book=account.setdefault('phase_books',{}).setdefault(phase,{'phase':phase,'started_at':account.get('phase_started_at')})
        book.update({k:copy.deepcopy(account.get(k)) for k in self.PHASE_FIELDS})
        book['phase_observed_at']=account.get('phase_observed_at')

    @staticmethod
    def _quota_phase(account, record):
        # Old records remain financially unassigned. Conservatively count their
        # daily entry against the first observed phase, never a later phase.
        return record.get('phase') or account.get('legacy_phase_scope')

    def _phase_flat_proof(self, row):
        proof=row.get('phase_snapshot') or {}
        return bool(row.get('current') is True and proof.get('phase')==row.get('phase')
            and proof.get('position_open') is False and finite(proof.get('unrealized_pnl'))
            and abs(proof['unrealized_pnl'])<1e-8 and finite(proof.get('balance'))
            and finite(row.get('balance')) and finite(row.get('unrealized_pnl'))
            and abs(row['unrealized_pnl'])<1e-8
            and math.isclose(proof['balance'],row['balance'],rel_tol=1e-12,abs_tol=.02)
            and finite(proof.get('price')) and proof['price']>0
            and finite(proof.get('observed_at')) and abs(self.clock()-proof['observed_at'])<=5)

    def _apply_phase_observation(self, account, row):
        """Return whether this row may update the account's current finances."""
        incoming=row.get('phase'); current=account.get('phase')
        if incoming is None:
            return current is None
        stamp=row.get('phase_observed_at')
        if incoming==current:
            if finite(stamp) and stamp>=(account.get('phase_observed_at') or 0):
                account['phase_observed_at']=stamp
            return True
        if (phase_rank(incoming)<phase_rank(current) or not finite(stamp)
            or abs(self.clock()-stamp)>5 or stamp<(account.get('phase_observed_at') or 0)):
            return False
        aid=account['id']
        positions=[p for p in self.data['positions'].values() if p['account_id']==aid]
        jobs=self._pending(aid)
        proof=self._phase_flat_proof(row)
        known_open=(positions or account.get('external_position') or
            self.data['bridge_sessions'].get(aid,{}).get('snapshot',{}).get('position_open') is True)
        uncertain=any(j['status'] in ('delivered','unknown') for j in jobs)
        if current and (known_open or uncertain) and not proof:
            account['phase_pending']={'phase':incoming,'observed_at':stamp,'reason':'等待新阶段明确空仓读数'}
            return False
        original={k:copy.deepcopy(account.get(k)) for k in self.PHASE_FIELDS}
        self._sync_phase_book(account)
        if not current:
            account.setdefault('phase_books',{}).setdefault('unknown',{'phase':None,**original})
            account.setdefault('legacy_phase_scope',incoming)
        old_jobs=copy.deepcopy(jobs) if current else []
        old_positions=copy.deepcopy(positions) if current else []
        if current:
            book=account['phase_books'][current]
            book.update({'ended_at':self.clock(),'settlement_pending':bool(positions or uncertain)})
            for job in jobs:
                if job['status']=='queued':
                    job.update({'status':'cancelled','cancel_reason':'阶段已切换，取消旧阶段未派发请求'})
                else:
                    job.update({'original_status':job['status'],'status':'archived_phase','archived_at':self.clock(),
                                'archive_reason':'新阶段明确空仓；旧阶段异常留存审计，不作为成交或损益确认'})
            for position in positions:
                del self.data['positions'][position['id']]
            self.data['bridge_sessions'].pop(aid,None)
        account.update({'phase':incoming,'phase_started_at':self.clock(),'phase_observed_at':stamp,
            'capital_metrics':{},'initial_balance':None,'balance':None,'equity':None,
            'unrealized_pnl':None,'realized_pnl':None,'observed_at':None,'profit_target':None,'max_loss':None})
        account.pop('phase_pending',None)
        if current or (incoming in ('2/2','live') and account.get('status')=='passed'):
            account.pop('evaluation_passed_at',None)
            if not account.get('retired_failed') and not account.get('archived'):
                account.update({'status':'active','lock_reason':None})
            if account.get('strategy_lock_source')!='manual' and not account.get('execution_excluded'):
                account.update({'strategy_locked':False,'strategy_lock_reason':None,
                                'strategy_lock_source':None,'strategy_locked_at':None})
        if proof:
            account['external_position']=False
        strategy=self.data['strategy']
        if current:
            strategy['order_queue']=[a for a in strategy.get('order_queue',[]) if a!=aid]
            if strategy.get('phase')=='submitting' and not strategy['order_queue']:
                strategy.update({'phase':'monitoring','next_action_at':None})
            if (strategy['status'] in self.ACTIVE and aid in strategy.get('config',{}).get('account_ids',[])
                and account.get('enabled') and not account.get('strategy_locked')
                and not account.get('archived') and not account.get('retired_failed') and not account.get('execution_excluded')
                and aid not in strategy.get('remaining_accounts',[])):
                strategy.setdefault('remaining_accounts',[]).append(aid)
        self.data['audit'].append({'event':'account_phase_change','time':self.clock(),'account_id':aid,
            'account_name':account['name'],'from_phase':current,'to_phase':incoming,'phase_observed_at':stamp,
            'previous_finances':original,'original_jobs':old_jobs,'positions_archived_without_execution':old_positions,
            'new_phase_flat_proof':copy.deepcopy(row.get('phase_snapshot')) if proof else None,
            'note':'阶段独立记录；未识别的旧历史不补填阶段，不推测跨阶段平仓损益'})
        self.data['audit']=self.data['audit'][-1000:]
        self._log('info',f"{account['name']} 阶段 {current or '未识别'} → {incoming}；新阶段独立结算并重置当日开仓额度")
        return True

    def _phase_summary(self, account, history, window):
        books=account.setdefault('phase_books',{})
        for phase in PHASES:
            trades=[t for t in history if t['account_id']==account['id'] and t.get('phase')==phase]
            if phase not in books and not trades:
                continue
            book=books.setdefault(phase,{'phase':phase})
            book['history_realized_pnl']=round(sum(t['pnl'] for t in trades),2)
            book['history_trade_count']=len(trades)
            def closed_at(trade):
                value=trade.get('closed_at')
                if finite(value):
                    return value/1000 if value>1e12 else value
                try:
                    return datetime.fromisoformat(str(value).replace('Z','+00:00')).timestamp()
                except (ValueError,TypeError,OverflowError):
                    return -math.inf
            daily=[t for t in trades if window['start']<=closed_at(t)<window['end'] and closed_at(t)<=self.clock()]
            book['daily_realized_pnl']=round(sum(t['pnl'] for t in daily),2)
            book['daily_trade_count']=len(daily)
            book['daily_entry_used']=self._daily_entry_used(account['id'],phase=phase)
        current=books.get(account.get('phase'),{})
        account['phase_realized_pnl']=current.get('history_realized_pnl',0) if account.get('phase') else None
