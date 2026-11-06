import copy
import random
import tempfile
import unittest
from pathlib import Path

from server.engine import Engine, ValidationError
from server.trading_day import trading_day_window


class BrowserBatchCompletionTests(unittest.TestCase):
    def setUp(self):
        self.now=1793860000.0
        self.engine=Engine(':memory:',seed=False,clock=lambda:self.now,rng=random.Random(7))
        self.sid='batch-completion-session'
        self.accounts=self.register()['accounts']
        self.engine.settings({'mode':'browser'})

    def tearDown(self):
        self.engine.close()

    def register(self,mode='execute',epoch=1,names=('A','B')):
        return self.engine.bridge_session({'session_id':self.sid,'mode':mode,'mode_epoch':epoch,'authorize':True,
            'tab_url':'https://next.vestmarkets.com/trade/NDX-USD-PERP','accounts':[
                {'name':name,'category':'eval','balance':5000,'equity':5000,'unrealized_pnl':0,
                 'current':index==0,'observed_at':self.now} for index,name in enumerate(names)]})

    def snapshot(self,**changes):
        return {'balance':5000,'equity':5000,'unrealized_pnl':0,'price':100,'symbol':'NQ-PERP',
            'position_open':False,'observed_at':self.now,**changes}

    def poll(self,account,snapshot=None):
        return self.engine.bridge_poll({'account_id':account['id'],'session_id':self.sid,'mode_epoch':1,
            'identity':account['identity'],'ready':True,'tab_url':'https://next.vestmarkets.com/trade/NDX-USD-PERP',
            'snapshot':snapshot or self.snapshot()})

    def strategy(self,ids):
        return {'account_ids':ids,'batch_size':1,'quantity':4,'side':'buy','stop_loss':250,
            'order_delay_min':0,'order_delay_max':0,'batch_delay_min':15,'batch_delay_max':15}

    def inspect(self,account,**changes):
        return self.engine.bridge_inspect({'account_id':account['id'],'name':account['name'],'session_id':self.sid,
            'tab_url':'https://next.vestmarkets.com/trade/NDX-USD-PERP','snapshot':self.snapshot(),**changes})

    def unknown_open(self):
        account=self.accounts[0]
        self.engine.place_orders({'account_ids':[account['id']],'side':'buy','quantity':4,'stop_loss':250,
            'idempotency_key':'unknown-completion-order'})
        job=self.poll(account)['job']
        self.engine.bridge_result({'job_id':job['id'],'account_id':account['id'],'session_id':self.sid,
            'success':False,'confirmed':False,'error':'Old client rejected native market link','snapshot':self.snapshot()})
        return account,job

    def test_native_stop_flat_confirmation_locks_account_then_advances_after_batch_delay(self):
        self.engine.start_strategy(self.strategy([a['id'] for a in self.accounts]))
        self.engine.tick(100)
        first=self.engine._account(self.engine.data['strategy']['batch_account_ids'][0])
        job=self.poll(first)['job']
        self.engine.bridge_result({'job_id':job['id'],'account_id':first['id'],'session_id':self.sid,'success':True,'confirmed':True,
            'snapshot':self.snapshot(position_open=True,position_id='native-nq-position',side='buy',quantity=4,entry_price=100)})
        self.assertIsNotNone(self.engine._position(first['id']))
        # Native protection closes between polls; no second close click is needed.
        self.now+=1
        self.poll(first,self.snapshot(balance=4750,equity=4750))
        self.assertIsNone(self.engine._position(first['id']))
        self.assertTrue(first['strategy_locked'])
        self.assertEqual(first['strategy_lock_source'],'stop_loss')
        self.engine.tick(100)
        self.assertEqual(self.engine.data['strategy']['phase'],'cooldown')
        self.assertEqual(len(self.engine.data['jobs']),1)
        self.now=self.engine.data['strategy']['next_action_at']
        self.engine.tick(100)
        state=self.engine.state()
        self.assertEqual(state['strategy']['batch_number'],2)
        self.assertNotIn(first['id'],state['strategy']['batch_account_ids'])
        self.assertEqual(len(self.engine.data['jobs']),2)
        self.assertEqual(state['history'][0]['pnl'],-250)
        self.assertEqual(state['strategy']['upcoming_account_ids'],[])

    def test_unknown_prior_order_blocks_new_run_before_creating_another_account_job(self):
        first,job=self.unknown_open()
        self.register(epoch=2)
        before=copy.deepcopy(self.engine.data['jobs'])
        with self.assertRaisesRegex(ValidationError,'A.*结果待核验'):
            self.engine.start_strategy(self.strategy([self.accounts[1]['id']]))
        with self.assertRaisesRegex(ValidationError,'A.*结果待核验'):
            self.engine.place_orders({'account_ids':[self.accounts[1]['id']],'quantity':4,'side':'buy','idempotency_key':'no-new-unknown-job'})
        self.assertEqual(self.engine.data['jobs'],before)
        self.assertEqual(self.engine.data['jobs'][job['id']]['status'],'unknown')

    def test_readonly_inspection_never_resolves_job_or_renews_permission_and_explicit_reconcile_keeps_daily_usage(self):
        account,job=self.unknown_open()
        self.register(mode='readonly',epoch=2)
        self.now+=2
        sessions=copy.deepcopy(self.engine.data['execution_sessions'])
        result=self.inspect(account)
        self.assertTrue(result['read_only'])
        self.assertEqual(self.engine.data['execution_sessions'],sessions)
        self.assertEqual(self.engine.data['jobs'][job['id']]['status'],'unknown')
        self.assertFalse(self.engine._ready(account['id']))
        with self.assertRaises(ValidationError):self.engine.reconcile({'account_id':account['id']})
        self.now+=31
        self.inspect(account)
        self.engine.reconcile({'account_id':account['id'],'acknowledge':True})
        self.assertEqual(self.engine.data['jobs'][job['id']]['status'],'reconciled')
        self.assertTrue(self.engine._daily_entry_used(account['id']))
        self.assertFalse(self.engine.data['history'],'manual verification must not invent a trade history')
        self.assertNotIn(self.engine.data['strategy']['status'],self.engine.ACTIVE)

    def test_inspection_requires_exact_account_fresh_consistent_snapshot_and_preserves_platform_failure(self):
        account,job=self.unknown_open()
        self.register(mode='readonly',epoch=2)
        for changes in ({'name':'B'},{'session_id':'unregistered'},{'snapshot':self.snapshot(observed_at=self.now-6)},
                        {'snapshot':self.snapshot(equity=4999)},{'tab_url':'https://evil.example/trade/NQ-PERP'}):
            with self.subTest(changes=changes),self.assertRaises(ValidationError):self.inspect(account,**changes)
        self.now+=31
        self.engine._account(account['id'])['category']='failed'
        self.inspect(account)
        self.engine.reconcile({'account_id':account['id'],'acknowledge':True})
        self.assertEqual(self.engine._account(account['id'])['status'],'locked')
        self.assertIn('failed',self.engine._account(account['id'])['lock_reason'])

    def confirm_failure(self,account,**changes):
        return self.engine.reconcile({'account_id':account['id'],'acknowledge':True,'terminal_status':'failed',**changes})

    def disappeared_unknown(self):
        account,job=self.unknown_open()
        self.register(mode='readonly',epoch=2,names=('B',))
        self.now+=31
        return self.engine._account(account['id']),self.engine.data['jobs'][job['id']]

    def test_confirmed_failure_archives_unknown_without_inventing_execution_or_resuming_strategy(self):
        account,job=self.disappeared_unknown()
        before=copy.deepcopy(account)
        original=copy.deepcopy(job)
        strategy=copy.deepcopy(self.engine.data['strategy'])
        sessions=copy.deepcopy(self.engine.data['execution_sessions'])
        self.assertFalse(self.engine._fresh(account['id']))
        self.assertTrue(self.confirm_failure(account)['ok'])
        self.assertEqual(job['status'],'archived_failed')
        self.assertEqual(job['original_status'],'unknown')
        for key,value in original.items():
            if key!='status':self.assertEqual(job[key],value)
        for key in ('balance','equity','unrealized_pnl','realized_pnl','observed_at'):
            self.assertEqual(account[key],before[key])
        audit=self.engine.data['audit'][-1]
        self.assertEqual(audit['event'],'user_confirmed_evaluation_failure')
        self.assertEqual(audit['account_before'],before)
        self.assertEqual(audit['original_jobs'],[original])
        self.assertFalse(self.engine.data['history'])
        self.assertFalse(self.engine.data['positions'])
        self.assertTrue(self.engine._daily_entry_used(account['id']))
        self.assertEqual(self.engine.data['strategy'],strategy)
        self.assertEqual(self.engine.data['execution_sessions'],sessions)
        self.assertEqual(self.engine.state()['bridge']['pending_jobs'],0)
        self.assertFalse(account['enabled'])
        self.assertEqual(account['category'],'failed')
        self.assertEqual(account['failure_confirmation_source'],'user')
        # Unrelated accounts require a fresh explicit permission grant and start.
        self.register(epoch=3)
        self.assertIsNone(self.register(epoch=3)['next_account'])
        self.engine.start_strategy(self.strategy([self.accounts[1]['id']]))
        self.engine.tick(100)
        new_jobs=[j for j in self.engine.data['jobs'].values() if j['status']=='queued']
        self.assertEqual([j['account_id'] for j in new_jobs],[self.accounts[1]['id']])

    def test_failure_confirmation_refuses_missing_ack_healthy_account_or_inflight_receipt(self):
        account,job=self.unknown_open()
        live=self.engine._account(account['id'])
        cases=[{'acknowledge':False},{'terminal_status':'passed'},{}]
        for changes in cases:
            with self.subTest(changes=changes):
                before=copy.deepcopy(self.engine.data)
                with self.assertRaises(ValidationError):self.confirm_failure(live,**changes)
                self.assertEqual(self.engine.data,before)
        self.register(mode='readonly',epoch=2,names=('B',))
        before=copy.deepcopy(self.engine.data)
        with self.assertRaisesRegex(ValidationError,'等待回执'):self.confirm_failure(live)
        self.assertEqual(self.engine.data,before)

    def test_failure_confirmation_refuses_running_strategy_known_position_and_primary(self):
        account,job=self.disappeared_unknown()
        self.engine.data['strategy']['status']='running'
        before=copy.deepcopy(self.engine.data)
        with self.assertRaisesRegex(ValidationError,'停止策略'):self.confirm_failure(account)
        self.assertEqual(self.engine.data,before)
        self.engine.data['strategy']['status']='stopped'
        self.engine.data['positions']['known']={'id':'known','account_id':account['id']}
        before=copy.deepcopy(self.engine.data)
        with self.assertRaisesRegex(ValidationError,'本地持仓'):self.confirm_failure(account)
        self.assertEqual(self.engine.data,before)
        self.engine.data['positions'].clear()
        account['execution_excluded']=True
        before=copy.deepcopy(self.engine.data)
        with self.assertRaisesRegex(ValidationError,'主账户'):self.confirm_failure(account)
        self.assertEqual(self.engine.data,before)

    def test_failure_disposition_is_idempotent_and_cannot_be_reenabled_or_forgotten(self):
        account,job=self.disappeared_unknown()
        self.confirm_failure(account)
        before=copy.deepcopy(self.engine.data)
        self.assertTrue(self.confirm_failure(account)['duplicate'])
        self.assertEqual(self.engine.data,before)
        for action in (lambda:self.engine.toggle_account(account['id'],True),
                       lambda:self.engine.strategy_lock_account(account['id'],False),
                       lambda:self.engine._new_job('open',account['id']),
                       lambda:self.engine.delete_account(account['id'])):
            with self.subTest(action=action),self.assertRaises(ValidationError):action()
        # Ordinary fresh-flat reconciliation may update observed money, not undo
        # the explicit terminal disposition or rewrite the archived receipt.
        self.inspect(account)
        self.engine.reconcile({'account_id':account['id'],'acknowledge':True})
        self.assertTrue(account['retired_failed'])
        self.assertEqual(account['status'],'locked')
        self.assertFalse(account['enabled'])
        self.assertEqual(job['status'],'archived_failed')
        self.assertFalse(self.engine._execution_capable(account['id']))

    def test_failure_survives_next_day_restart_duplicate_merge_and_other_browser_rescan(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent.parent/'runtime') as folder:
            self.engine.close()
            database=str(Path(folder)/'failed.sqlite')
            self.engine=Engine(database,seed=False,clock=lambda:self.now)
            self.accounts=self.register()['accounts']
            self.engine.settings({'mode':'browser'})
            account,job=self.disappeared_unknown()
            self.confirm_failure(account)
            # A historical duplicate without the flag must not replace the
            # canonical failure when the full account name is merged.
            duplicate={**copy.deepcopy(account),'id':'older-copy','created_at':account['created_at']-1,
                       'retired_failed':False,'category':'eval','enabled':True,'lock_reason':None,'status':'active'}
            self.engine.data['accounts'][duplicate['id']]=duplicate
            self.now=trading_day_window(self.now)['end']+1
            self.engine.close()
            self.engine=Engine(database,seed=False,clock=lambda:self.now)
            try:
                self.sid='another-browser'
                saved=next(a for a in self.register()['accounts'] if a['name']=='A')
                self.assertTrue(saved['retired_failed'])
                self.assertFalse(saved['enabled'])
                self.assertFalse(saved['executable'])
                self.assertEqual(saved['execution_mode'],'readonly')
                self.assertEqual(saved['category'],'failed')
                self.assertEqual(saved['status'],'locked')
                self.assertIsNone(saved['strategy_unlock_at'])
                self.assertEqual(len([a for a in self.engine.state()['accounts'] if a['name']=='A']),1)
                self.assertEqual(self.engine.data['jobs'][job['id']]['status'],'archived_failed')
                self.assertFalse(self.engine.data['history'])
            finally:
                self.engine.close()
                self.engine=Engine(':memory:',seed=False,clock=lambda:self.now)
