import copy
import tempfile
import unittest
from pathlib import Path

from server.engine import Engine, ValidationError
from server.trading_day import trading_day_window


class PrimaryExclusionTests(unittest.TestCase):
    def setUp(self):
        self.now = 1793800000.0
        self.engine = Engine(':memory:', seed=False, clock=lambda: self.now)
        self.engine.settings({'mode':'browser'})

    def tearDown(self):
        self.engine.close()

    def session(self, names=None, sid='primary-test-session', epoch=1, mode='execute', **extra):
        rows = [{'name':name,'category':category,'current':index==0,'balance':1000,
                 'equity':1000,'unrealized_pnl':0,'observed_at':self.now}
                for index,(name,category) in enumerate(names or [('主账户','default'),('Evaluation 1','eval')])]
        return self.engine.bridge_session({'session_id':sid,'mode':mode,'mode_epoch':epoch,'authorize':True,
            'tab_url':'https://next.vestmarkets.com/trade/NQ-PERP','accounts':rows,**extra})

    def order(self, ids):
        return {'account_ids':ids,'side':'buy','quantity':1,'stop_loss':130,'idempotency_key':'primary-test-order'}

    def snapshot(self, **changes):
        return {'balance':1000,'equity':1000,'unrealized_pnl':0,'price':100,
                'position_open':False,'symbol':'NQ-PERP','observed_at':self.now,**changes}

    def poll(self, account):
        return self.engine.bridge_poll({'account_id':account['id'],'session_id':account['session_id'],
            'identity':account['identity'],'mode_epoch':1,'ready':True,'snapshot':self.snapshot(),
            'tab_url':'https://next.vestmarkets.com/trade/NQ-PERP'})

    def forbidden(self, action):
        with self.assertRaises(ValidationError) as caught:
            action()
        self.assertEqual(caught.exception.status,403)
        self.assertIn('永久排除',str(caught.exception))

    def position(self, aid):
        position = {'id':'old-main-position','account_id':aid,'account_name':'主账户','mode':'browser',
            'source':'strategy','symbol':'NQ-PERP','side':'buy','quantity':1,'take_profit':None,'stop_loss':130,
            'entry_price':100,'mark_price':100,'unrealized_pnl':-200,'opened_at':self.now,'status':'open',
            'balance_before':1000,'browser_position_id':'native-main-position'}
        self.engine.data['positions'][position['id']] = position
        return position

    def test_main_stays_readable_but_execute_mode_only_routes_other_accounts(self):
        reply = self.session()
        main, normal = reply['accounts']
        self.assertTrue(main['execution_excluded'])
        self.assertTrue(main['strategy_locked'])
        self.assertIsNone(main['strategy_unlock_at'])
        self.assertFalse(main['enabled'])
        self.assertEqual(main['execution_mode'],'readonly')
        self.assertFalse(main['executable'])
        self.assertFalse(main['strategy_executable'])
        self.assertEqual(main['balance'],1000)
        self.assertIsNone(reply['next_account'],'idle execution must not prepare any account')
        self.assertTrue(normal['executable'])
        # Sharing the excluded main account does not reserve execution in another browser.
        other = self.session([('主账户','default')],sid='other-browser-session')
        self.assertIsNone(other['next_account'])
        self.assertEqual(other['accounts'][0]['id'],main['id'])

    def test_only_real_jobs_or_managed_positions_are_routed_and_operation_matches(self):
        main, normal = self.session()['accounts']
        self.assertIsNone(self.session()['next_account'])
        self.engine.place_orders(self.order([normal['id']]))
        route = self.session()['next_account']
        self.assertEqual(route['id'],normal['id'])
        self.assertEqual(route['operation'],'open')
        job = self.poll(normal)['job']
        self.engine.bridge_result({'job_id':job['id'],'account_id':normal['id'],'session_id':normal['session_id'],
            'success':True,'confirmed':True,'snapshot':self.snapshot(position_open=True,entry_price=100,
                quantity=1,side='buy',position_id='normal-managed-position')})
        self.assertEqual(self.session()['next_account']['operation'],'monitor')
        position = self.engine._position(normal['id'])
        self.engine.close_position(position['id'])
        route = self.session()['next_account']
        self.assertEqual(route['operation'],'close')
        self.assertNotEqual(route['id'],main['id'])

    def test_pre_dispatch_failure_keeps_exact_reason_without_consuming_daily_entry(self):
        main,normal=self.session()['accounts']
        self.engine.start_strategy({**self.order([normal['id']]),'batch_size':1,
            'order_delay_min':0,'order_delay_max':0,'batch_delay_min':0,'batch_delay_max':0})
        self.engine.tick(100)
        reason='无法确认 USD 选中状态 '+self.engine.bridge_token
        self.session(epoch=2,mode='readonly',execution_error=reason)
        job=next(iter(self.engine.data['jobs'].values()))
        self.assertEqual(job['status'],'cancelled')
        self.assertIsNone(job['delivered_at'])
        self.assertIn('无法确认 USD',job['cancel_reason'])
        self.assertNotIn(self.engine.bridge_token,job['cancel_reason'])
        state=self.engine.state()
        self.assertEqual(state['strategy']['status'],'paused')
        self.assertFalse(next(a for a in state['accounts'] if a['id']==normal['id'])['daily_entry_used'])
        self.assertIn('无法确认 USD',state['bridge']['execution_sessions'][0]['error'])
        self.assertTrue(any('无法确认 USD' in row['message'] for row in state['logs']))
        self.session(epoch=3)
        self.assertEqual(self.engine.state()['bridge']['execution_sessions'][0]['error'],'')
        self.assertEqual(self.engine.state()['strategy']['status'],'paused','permission recovery must not resume a real strategy')

    def test_primary_detection_uses_exact_names_or_type_without_hiding_normal_accounts(self):
        reply = self.session([('主账号','eval'),('Main Account','unknown'),('Wallet 1','default'),
                              ('Main Account 2','eval'),('主账户 2','eval')])
        self.assertEqual([a['name'] for a in reply['accounts'] if a.get('execution_excluded')],
                         ['主账号','Main Account','Wallet 1'])
        self.assertEqual(len([a for a in reply['accounts'] if a['executable']]),2)

    def test_manual_bulk_strategy_unlock_and_enable_cannot_override_exclusion(self):
        main, normal = self.session()['accounts']
        self.forbidden(lambda:self.engine.place_orders(self.order([normal['id'],main['id']])))
        self.assertFalse(self.engine.data['jobs'],'a mixed bulk request must not partially submit')
        self.forbidden(lambda:self.engine.start_strategy({**self.order([main['id']]),'batch_size':1,
            'order_delay_min':0,'order_delay_max':0,'batch_delay_min':0,'batch_delay_max':0}))
        self.forbidden(lambda:self.engine.toggle_account(main['id'],True))
        self.forbidden(lambda:self.engine.strategy_lock_account(main['id'],False))
        self.forbidden(lambda:self.engine._new_job('open',main['id']))
        position = self.position(main['id'])
        self.forbidden(lambda:self.engine.close_position(position['id']))
        self.forbidden(lambda:self.engine._new_job('close',main['id'],position=position))
        result = self.engine.close_all()
        self.assertTrue(result['ok'])
        self.assertEqual(result['results'][0]['status'],'excluded')
        self.engine.tick(1)
        self.assertIn(position['id'],self.engine.data['positions'])
        self.assertFalse(self.engine.data['jobs'])
        self.assertFalse(self.engine.data['history'])

    def test_old_queued_opens_and_closes_are_cancelled_even_if_poll_claims_ready(self):
        main, normal = self.session()['accounts']
        self.engine.place_orders(self.order([normal['id']]))
        normal_job = next(iter(self.engine.data['jobs'].values()))
        for kind in ('open','close'):
            old = {**copy.deepcopy(normal_job),'id':'old-main-'+kind,'account_id':main['id'],'type':kind}
            self.engine.data['jobs'][old['id']] = old
        result = self.poll(main)
        self.assertIsNone(result['job'])
        self.assertTrue(all(j['status']=='cancelled' for j in self.engine.data['jobs'].values() if j['account_id']==main['id']))
        self.assertEqual(normal_job['status'],'queued')
        self.assertFalse(self.engine._ready(main['id']))

    def test_delivered_receipt_is_recorded_but_sizing_and_further_execution_are_blocked(self):
        main, normal = self.session()['accounts']
        self.engine.place_orders(self.order([normal['id']]))
        original = next(iter(self.engine.data['jobs'].values()))
        job = {**original,'id':'old-delivered-main','account_id':main['id'],'identity':main['identity'],
               'status':'delivered','delivered_at':self.now,'balance_before':1000,'size_percent':50}
        self.engine.data['jobs'][job['id']] = job
        self.forbidden(lambda:self.engine.bridge_size({'job_id':job['id'],'account_id':main['id']}))
        receipt = self.engine.bridge_result({'job_id':job['id'],'account_id':main['id'],
            'session_id':main['session_id'],'mode_epoch':1,'success':True,'confirmed':True,
            'snapshot':self.snapshot(position_open=True,entry_price=100,quantity=1,side='buy',position_id='old-native-main')})
        self.assertTrue(receipt['ok'],'already delivered results must not be discarded')
        self.assertIsNotNone(self.engine._position(main['id']))
        self.assertFalse(self.engine._execution_capable(main['id']))
        self.assertEqual(len([j for j in self.engine.data['jobs'].values() if j['account_id']==main['id']]),1)

    def test_exclusion_survives_daily_rollover_restart_merge_and_changed_browser_metadata(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent.parent/'runtime') as folder:
            self.engine.close()
            database = str(Path(folder)/'primary.sqlite')
            self.engine = Engine(database,seed=False,clock=lambda:self.now)
            main = self.session([('Wallet','default')])['accounts'][0]
            duplicate = {**self.engine.data['accounts'][main['id']],'id':'old-primary-copy',
                'category':'eval','execution_excluded':False,'execution_exclusion_source':None}
            self.engine.data['accounts'][duplicate['id']] = duplicate
            self.now = trading_day_window(self.now)['end']+1
            self.engine.close()
            self.engine = Engine(database,seed=False,clock=lambda:self.now)
            try:
                reply = self.session([('Wallet','eval')],sid='new-browser-session')
                saved = reply['accounts'][0]
                self.assertTrue(saved['execution_excluded'])
                self.assertTrue(saved['strategy_locked'])
                self.assertIsNone(saved['strategy_unlock_at'])
                self.assertFalse(saved['executable'])
                self.assertEqual(saved['category'],'eval')
                self.assertIsNone(reply['next_account'])
                self.assertEqual(len(self.engine.state()['accounts']),1)
                self.forbidden(lambda:self.engine.place_orders(self.order([saved['id']])))
            finally:
                self.engine.close()
                self.engine = Engine(':memory:',seed=False,clock=lambda:self.now)
