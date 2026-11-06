import http.client
import copy
import json
import math
import random
import tempfile
import threading
import unittest
from pathlib import Path

from server.engine import Engine, ValidationError
from server.app import create_server


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.now = 1_790_000_000.0
        self.engine = Engine(':memory:', seed=False, clock=lambda: self.now, rng=random.Random(9))

    def tearDown(self):
        self.engine.close()

    def account(self, mode='paper', **extra):
        return self.engine.add_account({'name':'Test account','initial_balance':1000,
            'profit_target':100,'max_loss':50,'mode':mode, **extra})['id']

    def order(self, ids, **extra):
        return {'account_ids': ids, 'quantity':1, 'side':'buy','symbol':'NQ-PERP',
                'take_profit':10,'stop_loss':10,'idempotency_key':'test-key-00001', **extra}

    def strategy(self, ids, **extra):
        config = {'account_ids':ids,'batch_size':2,'quantity':1,'side':'buy','symbol':'NQ-PERP',
                'take_profit':10,'stop_loss':10,'order_delay_min':3,'order_delay_max':7,
                'batch_delay_min':5,'batch_delay_max':9, **extra}
        if 'size_percent' in extra:
            config.pop('quantity',None)
        return config

    def snapshot(self, **extra):
        return {'balance':1000,'equity':1000,'unrealized_pnl':0,'price':100,
                'side':'buy','quantity':1,'symbol':'NQ-PERP','position_id':'vest-position-123',
                'position_open':False,'observed_at':self.now, **extra}

    def observation_row(self, **extra):
        return {'name':'Account 12','category':'Evaluation','equity':512.25,
                'unrealized_pnl':12.25,'balance':500,'current':True,
                'observed_at':self.now, **extra}

    def observe(self, rows=None, **extra):
        return self.engine.observe_accounts({'tab_url':'https://next.vestmarkets.com/trade/NQ-PERP',
            'accounts':[self.observation_row()] if rows is None else rows, **extra})

    def plugin_session(self, *, mode='readonly', epoch=0, authorize=False, rows=None, sid='profile-tab-123456'):
        return self.engine.bridge_session({'session_id':sid,'mode':mode,'mode_epoch':epoch,'authorize':authorize,
            'tab_url':'https://next.vestmarkets.com/trade/NQ-PERP',
            'accounts':[self.observation_row()] if rows is None else rows})

    def plugin_poll(self, account, *, epoch=1, snapshot=None, ready=True, **extra):
        return self.engine.bridge_poll({'session_id':account['session_id'],'account_id':account['id'],
            'identity':account['identity'],'mode_epoch':epoch,'tab_url':'https://next.vestmarkets.com/trade/NQ-PERP',
            'ready':ready,'snapshot':self.snapshot(balance=500,equity=500) if snapshot is None else snapshot, **extra})

    def open_browser_strategy(self):
        account = self.plugin_session(mode='execute',epoch=1,authorize=True,
            rows=[self.observation_row(category='eval')])['accounts'][0]
        self.engine.settings({'mode':'browser'})
        self.engine.start_strategy(self.strategy([account['id']],batch_size=1))
        self.engine.tick(price=100)
        job = self.plugin_poll(account)['job']
        self.engine.bridge_result({'job_id':job['id'],'session_id':account['session_id'],
            'success':True,'confirmed':True,'snapshot':self.snapshot(position_open=True,
            entry_price=100,balance=500,equity=500)})
        return account, self.engine.state()['positions'][0]

    def test_strategy_manual_lock_excludes_only_automatic_orders(self):
        aid = self.account()
        self.engine.strategy_lock_account(aid,True)
        with self.assertRaises(ValidationError):
            self.engine.start_strategy(self.strategy([aid],batch_size=1))
        self.engine.place_orders(self.order([aid]))
        self.assertEqual(self.engine.state()['positions'][0]['source'],'manual')
        self.assertEqual(self.engine.data['accounts'][aid]['strategy_lock_source'],'manual')
        self.engine.strategy_lock_account(aid,False)
        self.assertFalse(self.engine.data['accounts'][aid]['strategy_locked'])
        for invalid in (None,'true',1):
            with self.subTest(invalid=invalid), self.assertRaises(ValidationError):
                self.engine.strategy_lock_account(aid,invalid)

    def percentage_job(self, percent=50):
        account = self.plugin_session(mode='execute',epoch=1,authorize=True,
            rows=[self.observation_row(category='eval')])['accounts'][0]
        self.engine.settings({'mode':'browser'})
        self.engine.start_strategy(self.strategy([account['id']],batch_size=1,size_percent=percent))
        self.engine.tick(price=100)
        return account,self.plugin_poll(account)['job']

    def sizing_body(self, account, job, quantity=3.25, **extra):
        return {'account_id':account['id'],'job_id':job['id'],'session_id':account['session_id'],
            'mode_epoch':1,'identity':account['identity'],'size_percent':job['size_percent'],'quantity':quantity,
            'tab_url':'https://next.vestmarkets.com/trade/NQ-PERP','snapshot':self.snapshot(balance=500,equity=500),**extra}

    def test_strategy_position_size_accepts_only_four_presets(self):
        aid=self.account()
        for percent in (0,1,24,26,101,True,None,'25',float('nan')):
            with self.subTest(percent=percent), self.assertRaises(ValidationError):
                self.engine.start_strategy(self.strategy([aid],batch_size=1,size_percent=percent))
        self.engine.tick(price=100)
        self.engine.start_strategy(self.strategy([aid],batch_size=1,size_percent=25))
        self.engine.tick(price=100)
        position=next(iter(self.engine.data['positions'].values()))
        self.assertEqual(position['size_percent'],25)
        self.assertEqual(position['quantity'],2.5,'paper mode explicitly uses 1x equity simulation')

    def test_percentage_order_pins_page_quantity_before_accepting_matching_fill(self):
        self.engine.settings({'mode':'browser'})
        for percent,quantity in ((25,0.4),(50,3.25),(75,17.5),(100,2.6)):
            with self.subTest(percent=percent):
                # Use independent sessions because prior confirmed positions remain protected.
                name='Pct '+str(percent)
                account=self.plugin_session(mode='execute',epoch=1,authorize=True,sid='percent-session-'+str(percent),rows=[self.observation_row(name=name,category='eval')])['accounts'][0]
                self.engine.stop_strategy()
                self.engine.start_strategy(self.strategy([account['id']],batch_size=1,size_percent=percent))
                self.engine.tick(price=100)
                job=self.plugin_poll(account)['job']
                self.assertNotIn('quantity',job)
                sized=self.engine.bridge_size(self.sizing_body(account,job,quantity))
                self.assertEqual(sized['quantity'],quantity)
                result=self.engine.bridge_result({'job_id':job['id'],'session_id':account['session_id'],'success':True,'confirmed':True,
                    'snapshot':self.snapshot(position_open=True,quantity=quantity,entry_price=100,balance=500,equity=500)})
                self.assertTrue(result['ok'])
                position=self.engine._position(account['id'])
                self.assertEqual(position['quantity'],quantity)
                self.assertEqual(position['size_percent'],percent)

    def test_percentage_fill_without_preclick_quantity_confirmation_is_unknown(self):
        account,job=self.percentage_job()
        result=self.engine.bridge_result({'job_id':job['id'],'session_id':account['session_id'],'success':True,'confirmed':True,
            'snapshot':self.snapshot(position_open=True,quantity=3.25,entry_price=100,balance=500,equity=500)})
        self.assertEqual(result['status'],'unknown')
        self.assertFalse(self.engine.data['positions'])

    def test_percentage_sizing_cannot_change_after_pinning_and_wrong_fill_is_refused(self):
        account,job=self.percentage_job()
        body=self.sizing_body(account,job)
        self.engine.bridge_size(body)
        self.assertTrue(self.engine.bridge_size(body)['ok'])
        with self.assertRaises(ValidationError):
            self.engine.bridge_size({**body,'quantity':4})
        result=self.engine.bridge_result({'job_id':job['id'],'session_id':account['session_id'],'success':True,'confirmed':True,
            'snapshot':self.snapshot(position_open=True,quantity=4,entry_price=100,balance=500,equity=500)})
        self.assertEqual(result['status'],'unknown')
        self.assertFalse(self.engine.data['positions'])

    def test_percentage_sizing_requires_matching_account_preset_fresh_empty_snapshot_and_execute(self):
        account,job=self.percentage_job()
        body=self.sizing_body(account,job)
        for changed in ({'account_id':'other'},{'size_percent':25},{'quantity':0},
            {'snapshot':self.snapshot(position_open=True)}, {'snapshot':self.snapshot(observed_at=self.now-10)}):
            with self.subTest(changed=changed), self.assertRaises(ValidationError):
                self.engine.bridge_size({**body,**changed})
        self.engine.strategy_lock_account(account['id'],True)
        with self.assertRaises(ValidationError):
            self.engine.bridge_size(body)
        self.engine.strategy_lock_account(account['id'],False)
        self.plugin_session(mode='readonly',epoch=2,rows=[self.observation_row(category='eval')])
        with self.assertRaises(ValidationError):
            self.engine.bridge_size(body)

    def test_retired_preparation_endpoint_cannot_create_jobs_change_accounts_or_renew_permission(self):
        rows=[self.observation_row(name='Retired '+str(i),category='eval') for i in range(4)]
        reply=self.plugin_session(mode='execute',epoch=1,authorize=True,rows=rows)
        self.engine.settings({'mode':'browser'})
        self.engine.start_strategy(self.strategy([a['id'] for a in reply['accounts']],batch_size=2))
        self.engine.tick(price=100)
        before=copy.deepcopy(self.engine.data)
        result=self.engine.bridge_preparation({'session_id':reply['session_id'],'mode_epoch':1,
            'observation':self.observation_row(category='passed')})
        self.assertFalse(result['active'])
        self.assertTrue(result['disabled'])
        self.assertEqual(result['accounts'],[])
        self.assertEqual(self.engine.data,before)
        self.assertEqual(self.engine.data['strategy']['upcoming_account_ids'],[])

    def test_strategy_lock_during_jitter_skips_unsent_account_and_keeps_batch_barrier(self):
        ids = [self.account() for _ in range(4)]
        self.engine.start_strategy(self.strategy(ids,order_delay_min=5,order_delay_max=5,
            batch_delay_min=0,batch_delay_max=0))
        self.engine.tick(price=100)
        active = self.engine.state()['positions'][0]['account_id']
        skipped = self.engine.data['strategy']['order_queue'][0]
        self.engine.strategy_lock_account(skipped,True)
        self.now += 6
        self.engine.tick(price=100)
        self.assertEqual([p['account_id'] for p in self.engine.state()['positions']],[active])
        self.assertEqual(self.engine.data['strategy']['phase'],'monitoring')
        self.engine.tick(price=111)
        self.assertTrue(self.engine.data['accounts'][active]['strategy_locked'])
        self.assertEqual(self.engine.data['accounts'][active]['strategy_lock_source'],'take_profit')
        self.engine.tick(price=111)
        self.assertEqual(self.engine.data['strategy']['batch_number'],2)
        self.assertTrue(all(p['account_id'] not in (active,skipped) for p in self.engine.state()['positions']))

    def test_strategy_take_profit_and_stop_loss_lock_paper_accounts(self):
        for mark, reason in ((111,'take_profit'),(89,'stop_loss')):
            with self.subTest(reason=reason):
                aid = self.account()
                self.engine.tick(price=100)
                self.engine.start_strategy(self.strategy([aid],batch_size=1))
                self.engine.tick(price=100)
                self.engine.tick(price=mark)
                account = self.engine.data['accounts'][aid]
                self.assertTrue(account['strategy_locked'])
                self.assertEqual(account['strategy_lock_source'],reason)
                self.assertEqual(account['status'],'active')
                self.engine.stop_strategy()
                with self.assertRaises(ValidationError):
                    self.engine.start_strategy(self.strategy([aid],batch_size=1))

    def test_manual_trade_completion_also_excludes_account_from_strategy(self):
        aid = self.account()
        self.engine.tick(price=100)
        self.engine.place_orders(self.order([aid]))
        self.engine.tick(price=111)
        self.assertTrue(self.engine.data['accounts'][aid]['strategy_locked'])

    def test_strategy_lock_cancels_only_queued_strategy_open_and_preserves_delivered(self):
        rows = [self.observation_row(name='Manual'),self.observation_row(name='Strategy')]
        a,b = self.plugin_session(mode='execute',epoch=1,authorize=True,rows=rows)['accounts']
        self.engine.settings({'mode':'browser'})
        manual = self.engine.place_orders(self.order([a['id']]))['orders'][0]['job_id']
        self.engine.start_strategy(self.strategy([b['id']],batch_size=1))
        self.engine.tick(price=100)
        self.engine.strategy_lock_account(a['id'],True)
        self.engine.strategy_lock_account(b['id'],True)
        jobs = list(self.engine.data['jobs'].values())
        self.assertEqual(self.engine.data['jobs'][manual]['status'],'queued')
        self.assertEqual(next(j for j in jobs if j.get('source')=='strategy')['status'],'cancelled')
        self.assertEqual(self.plugin_poll(a)['job']['id'],manual)
        self.engine.strategy_lock_account(a['id'],True)
        self.assertEqual(self.engine.data['jobs'][manual]['status'],'delivered')

    def test_browser_strategy_threshold_locks_before_close_confirmation_without_disabling_protection(self):
        account,position = self.open_browser_strategy()
        closing = self.plugin_poll(account,snapshot=self.snapshot(position_open=True,entry_price=100,
            price=111,balance=500,equity=511,unrealized_pnl=11))['job']
        self.assertEqual(closing['type'],'close')
        self.assertTrue(self.engine.data['accounts'][account['id']]['strategy_locked'])
        self.assertEqual(self.engine.data['accounts'][account['id']]['strategy_lock_source'],'take_profit')
        self.assertIn(position['id'],self.engine.data['positions'])
        self.assertEqual(self.engine.data['strategy']['completed_accounts'],[])
        self.engine.bridge_result({'job_id':closing['id'],'session_id':account['session_id'],
            'success':True,'confirmed':True,'snapshot':self.snapshot(position_open=False,
            price=111,balance=511,equity=511)})
        self.assertFalse(self.engine.data['positions'])
        self.assertEqual(self.engine.data['history'][0]['reason'],'take_profit')

    def test_evaluation_pass_locks_and_reconciles_existing_strategy_without_freezing_session(self):
        account,position = self.open_browser_strategy()
        passed = self.plugin_session(mode='execute',epoch=1,
            rows=[self.observation_row(category='passed',balance=511,equity=511,unrealized_pnl=0)])
        current = passed['accounts'][0]
        self.assertTrue(current['strategy_locked'])
        self.assertEqual(current['strategy_lock_source'],'evaluation_passed')
        self.assertEqual(current['status'],'passed')
        self.assertIsNone(current['lock_reason'])
        self.assertEqual(passed['mode'],'execute')
        self.assertEqual(self.plugin_poll(current,snapshot=self.snapshot(balance=511,equity=511))['job'],None)
        self.assertFalse(self.engine.data['positions'])
        self.assertEqual(self.engine.data['accounts'][account['id']]['strategy_lock_source'],'evaluation_passed')
        with self.assertRaises(ValidationError):
            self.engine.strategy_lock_account(account['id'],False)

    def test_browser_external_strategy_close_locks_without_claiming_a_stop_reason(self):
        account,_ = self.open_browser_strategy()
        self.plugin_poll(account,snapshot=self.snapshot(balance=505,equity=505,price=105))
        current = self.engine.data['accounts'][account['id']]
        self.assertEqual(current['strategy_lock_source'],'external_close')
        self.assertEqual(self.engine.data['history'][0]['reason'],'external')
        self.assertTrue(current['strategy_locked'])

    def test_passed_strategy_account_still_delivers_protective_close(self):
        account,position = self.open_browser_strategy()
        current = self.plugin_session(mode='execute',epoch=1,
            rows=[self.observation_row(category='passed',balance=500,equity=511,unrealized_pnl=11)])['accounts'][0]
        closing = self.plugin_poll(current,snapshot=self.snapshot(position_open=True,
            entry_price=100,price=111,balance=500,equity=511,unrealized_pnl=11))['job']
        self.assertEqual(closing['type'],'close')
        self.assertEqual(current['strategy_lock_source'],'evaluation_passed')
        self.assertIn(position['id'],self.engine.data['positions'])
        self.engine.bridge_result({'job_id':closing['id'],'session_id':account['session_id'],
            'success':True,'confirmed':True,'snapshot':self.snapshot(balance=511,equity=511,price=111)})
        self.assertFalse(self.engine.data['positions'])
        self.assertEqual(self.engine.data['accounts'][account['id']]['strategy_lock_source'],'evaluation_passed')

    def test_strategy_lock_persists_across_restart_and_unlock_preserves_risk_lock(self):
        runtime = Path(__file__).resolve().parent.parent/'runtime'
        runtime.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=runtime) as directory:
            db = str(Path(directory)/'locked.sqlite')
            e = Engine(db,seed=False,clock=lambda:self.now)
            aid = e.add_account({'name':'Persistent','initial_balance':1000,'profit_target':100,'max_loss':50})['id']
            e.strategy_lock_account(aid,True)
            e.close()
            e = Engine(db,seed=False,clock=lambda:self.now)
            self.assertTrue(e.data['accounts'][aid]['strategy_locked'])
            e.data['accounts'][aid].update({'status':'locked','lock_reason':'Unknown fill'})
            e.strategy_lock_account(aid,False)
            self.assertEqual(e.data['accounts'][aid]['status'],'locked')
            self.assertEqual(e.data['accounts'][aid]['lock_reason'],'Unknown fill')
            e.close()

    def test_plugin_readonly_auto_discovers_without_manual_binding_or_permission(self):
        result = self.plugin_session()
        self.assertEqual(result['mode'],'readonly')
        account = result['accounts'][0]
        self.assertTrue(account['auto_discovered'])
        self.assertFalse(account['executable'])
        self.assertEqual(account['initial_balance'],500)
        self.assertEqual(account['baseline_source'],'first_observed')
        self.assertIsNone(account['profit_target'])
        self.assertIsNone(account['max_loss'])
        self.assertIsNone(result['next_account'])
        self.engine.settings({'mode':'browser'})
        with self.assertRaises(ValidationError):
            self.engine.place_orders(self.order([account['id']]))
        self.assertFalse(self.engine.data['jobs'])
        unread = self.plugin_session(rows=[self.observation_row(name='Unread',equity=None,balance=None,unrealized_pnl=None,observed_at=None)])['accounts'][0]
        self.assertIsNone(unread['initial_balance'])
        self.assertIsNone(unread['balance'])
        self.assertIsNone(unread['equity'])
        self.assertIsNone(unread['realized_pnl'])

    def test_plugin_execution_queues_without_current_snapshot_but_delivers_only_fresh_matching_account(self):
        self.plugin_session()
        result = self.plugin_session(mode='execute',epoch=1,authorize=True)
        account = result['accounts'][0]
        self.engine.settings({'mode':'browser'})
        self.assertTrue(account['executable'])
        self.engine.place_orders(self.order([account['id']]))
        self.assertFalse(any(j['status']=='delivered' for j in self.engine.data['jobs'].values()))
        stale = self.snapshot(balance=500,equity=500,observed_at=self.now-6)
        self.assertIsNone(self.plugin_poll(account,snapshot=stale)['job'])
        with self.assertRaises(ValidationError):
            self.plugin_poll(account,identity='eval:Wrong account')
        with self.assertRaises(ValidationError):
            self.plugin_poll(account,session_id='different-profile')
        job = self.plugin_poll(account)['job']
        self.assertEqual(job['account_id'],account['id'])
        self.assertEqual(job['session_id'],account['session_id'])
        self.assertIsNone(self.plugin_poll(account)['job'])

    def test_plugin_readonly_cancels_queued_actions_and_pauses_strategy(self):
        account = self.plugin_session(mode='execute',epoch=1,authorize=True)['accounts'][0]
        self.engine.settings({'mode':'browser'})
        self.engine.start_strategy(self.strategy([account['id']],batch_size=1))
        self.engine.tick(price=100)
        self.assertTrue(self.engine.data['jobs'])
        result = self.plugin_session(mode='readonly',epoch=2)
        self.assertEqual(result['mode'],'readonly')
        self.assertEqual(self.engine.state()['strategy']['status'],'paused')
        self.assertTrue(all(j['status']=='cancelled' for j in self.engine.data['jobs'].values()))
        self.assertIsNone(self.plugin_poll(account,epoch=2)['job'])

    def test_plugin_stale_epochs_and_old_heartbeat_cannot_reauthorize(self):
        self.plugin_session(mode='execute',epoch=1,authorize=True)
        self.plugin_session(mode='readonly',epoch=2)
        with self.assertRaises(ValidationError):
            self.plugin_session(mode='execute',epoch=1,authorize=True)
        self.assertEqual(self.plugin_session(mode='execute',epoch=2,authorize=True)['mode'],'readonly')
        self.assertEqual(self.plugin_session(mode='execute',epoch=3,authorize=False)['mode'],'readonly')
        result = self.plugin_session(mode='execute',epoch=4,authorize=True)
        self.assertEqual(result['mode'],'execute')

    def test_plugin_lease_expiry_cancels_without_replaying_or_automatic_regrant(self):
        account = self.plugin_session(mode='execute',epoch=1,authorize=True)['accounts'][0]
        self.engine.settings({'mode':'browser'})
        self.engine.place_orders(self.order([account['id']]))
        self.now += 31
        self.engine.tick(price=100)
        state = self.engine.state()
        self.assertFalse(state['accounts'][0]['executable'])
        self.assertEqual(state['accounts'][0]['execution_mode'],'readonly')
        self.assertEqual(next(iter(self.engine.data['jobs'].values()))['status'],'cancelled')
        self.assertEqual(self.plugin_session(mode='execute',epoch=1)['mode'],'readonly')

    def test_plugin_delivered_receipt_is_accepted_after_switching_readonly(self):
        account = self.plugin_session(mode='execute',epoch=1,authorize=True)['accounts'][0]
        self.engine.settings({'mode':'browser'})
        self.engine.place_orders(self.order([account['id']]))
        job = self.plugin_poll(account)['job']
        self.plugin_session(mode='readonly',epoch=2)
        self.assertEqual(self.engine.data['jobs'][job['id']]['status'],'delivered')
        result = self.engine.bridge_result({'job_id':job['id'],'account_id':account['id'],
            'session_id':account['session_id'],'mode_epoch':1,'success':True,'confirmed':True,
            'snapshot':self.snapshot(position_open=True,entry_price=100,balance=500,equity=500)})
        self.assertTrue(result['ok'])
        self.assertEqual(len(self.engine.state()['positions']),1)
        with self.assertRaises(ValidationError):
            self.engine.close_position(self.engine.state()['positions'][0]['id'])

    def test_plugin_readonly_cancels_close_and_preserves_real_position(self):
        account = self.plugin_session(mode='execute',epoch=1,authorize=True)['accounts'][0]
        self.engine.settings({'mode':'browser'})
        self.engine.place_orders(self.order([account['id']]))
        job = self.plugin_poll(account)['job']
        self.engine.bridge_result({'job_id':job['id'],'session_id':account['session_id'],'success':True,'confirmed':True,
            'snapshot':self.snapshot(position_open=True,entry_price=100,balance=500,equity=500)})
        pid = self.engine.state()['positions'][0]['id']
        self.engine.close_position(pid)
        self.plugin_session(mode='readonly',epoch=2)
        self.assertEqual(self.engine.data['positions'][pid]['status'],'open')
        self.assertEqual([j for j in self.engine.data['jobs'].values() if j['type']=='close'][0]['status'],'cancelled')
        self.assertFalse(self.engine.data['history'])

    def test_plugin_one_actor_never_routes_or_delivers_another_account_during_an_inflight_action(self):
        rows = [self.observation_row(name='A'),self.observation_row(name='B')]
        accounts = self.plugin_session(mode='execute',epoch=1,authorize=True,rows=rows)['accounts']
        self.engine.settings({'mode':'browser'})
        self.engine.place_orders(self.order([a['id'] for a in accounts]))
        job = self.plugin_poll(accounts[0])['job']
        heartbeat = self.plugin_session(mode='execute',epoch=1,rows=rows)
        self.assertIsNone(heartbeat['next_account'])
        self.assertIsNone(self.plugin_poll(accounts[1])['job'])
        self.assertEqual(sum(j['status']=='delivered' for j in self.engine.data['jobs'].values()),1)
        self.engine.bridge_result({'job_id':job['id'],'session_id':accounts[0]['session_id'],'success':False,'confirmed':False})
        self.assertEqual(self.engine.data['execution_sessions'][accounts[0]['session_id']]['mode'],'readonly')

    def test_plugin_duplicate_names_different_categories_do_not_execute_and_category_changes_preserve_id(self):
        rows = [self.observation_row(name='Same',category='eval'),self.observation_row(name='Same',category='funded')]
        result = self.plugin_session(mode='execute',epoch=1,authorize=True,rows=rows)
        self.assertEqual(len(result['accounts']),1)
        self.assertTrue(result['accounts'][0]['ambiguous'])
        self.assertFalse(result['accounts'][0]['executable'])
        account = self.plugin_session(mode='readonly',epoch=2,rows=[self.observation_row(name='Same',category='eval')])['accounts'][0]
        passed = self.plugin_session(mode='readonly',epoch=2,rows=[self.observation_row(name='Same',category='passed')])['accounts'][0]
        self.assertEqual(account['id'],passed['id'])
        self.assertEqual(passed['status'],'passed')
        self.assertFalse(passed['executable'])

    def test_plugin_heartbeat_only_keeps_lease_without_advancing_account_router(self):
        rows = [self.observation_row(name='A'),self.observation_row(name='B')]
        result = self.plugin_session(mode='execute',epoch=1,authorize=True,rows=rows)
        sid = result['session_id']
        cursor = self.engine.data['execution_sessions'][sid]['cursor']
        self.now += 20
        result = self.engine.bridge_session({'session_id':sid,'mode':'execute','mode_epoch':1,
            'heartbeat_only':True,'tab_url':'https://next.vestmarkets.com/trade/NQ-PERP','accounts':rows})
        self.assertIsNone(result['next_account'])
        self.assertEqual(self.engine.data['execution_sessions'][sid]['cursor'],cursor)
        self.assertEqual(result['mode'],'execute')
        self.assertTrue(all(a['executable'] for a in result['accounts']))

    def test_plugin_restart_keeps_accounts_but_never_automatically_restores_execute(self):
        runtime = Path(__file__).resolve().parent.parent/'runtime'
        runtime.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=runtime) as temp:
            db = str(Path(temp)/'plugin.sqlite')
            engine = Engine(db,seed=False,clock=lambda:self.now)
            body = {'session_id':'restart-session-123','mode':'execute','mode_epoch':7,'authorize':True,
                'tab_url':'https://next.vestmarkets.com/trade/NQ-PERP','accounts':[self.observation_row()]}
            first = engine.bridge_session(body)
            aid = first['accounts'][0]['id']
            engine.close()
            engine = Engine(db,seed=False,clock=lambda:self.now)
            self.assertFalse(engine.state()['accounts'][0]['executable'])
            body['authorize'] = False
            resumed = engine.bridge_session(body)
            self.assertEqual(resumed['mode'],'readonly')
            self.assertEqual(resumed['accounts'][0]['id'],aid)
            body.update(mode_epoch=8,authorize=True)
            self.assertEqual(engine.bridge_session(body)['mode'],'execute')
            engine.close()

    def test_read_only_observation_is_separate_from_trading_state(self):
        aid = self.browser_account()
        self.engine.start_strategy(self.strategy([aid],batch_size=1))
        self.engine.tick(price=100)
        before = copy.deepcopy(self.engine.data)
        result = self.observe([self.observation_row(unrealized_pnl=42,equity=542,balance=500)])
        self.assertTrue(result['ok'])
        for key in before:
            if key != 'read_only_observation':
                self.assertEqual(self.engine.data[key],before[key],key)
        observation = self.engine.account_observation()
        self.assertTrue(observation['connected'])
        self.assertEqual(observation['accounts'][0]['equity'],542)
        self.assertEqual(observation['accounts'][0]['balance'],500)
        self.assertEqual(observation['accounts'][0]['source'],'vest-page')
        self.assertFalse(observation['accounts'][0]['stale'])
        self.assertTrue(observation['accounts'][0]['key'].startswith('observed-'))
        with self.assertRaises(ValidationError):
            self.engine.place_orders(self.order([observation['accounts'][0]['key']]))

    def test_read_only_initial_view_does_not_use_manually_entered_balance(self):
        self.account(mode='browser',name='Account 12',initial_balance=500)
        result = self.engine.account_observation()
        self.assertFalse(result['connected'])
        self.assertEqual(result['accounts'],[])

    def test_read_only_null_and_mismatched_balance_remain_unknown(self):
        rows = [self.observation_row(name='Unread',equity=None,unrealized_pnl=None,balance=999),
                self.observation_row(name='Mismatch',equity=512.25,unrealized_pnl=12.25,balance=501),
                self.observation_row(name='Derived',equity=512.25,unrealized_pnl=12.25,balance=None),
                self.observation_row(name='Zero',equity=0,unrealized_pnl=0,balance=0)]
        self.observe(rows)
        values = {r['name']:r for r in self.engine.account_observation()['accounts']}
        self.assertIsNone(values['Unread']['equity'])
        self.assertIsNone(values['Unread']['balance'])
        self.assertIsNone(values['Mismatch']['balance'])
        self.assertIn('不一致',values['Mismatch']['read_error'])
        self.assertEqual(values['Derived']['balance'],500)
        self.assertEqual(values['Zero']['balance'],0)

    def test_read_only_row_timestamps_expire_even_with_current_heartbeats(self):
        old = self.observation_row(name='Cached account',current=False)
        self.observe([old,self.observation_row(name='Current account')])
        self.now += 31
        self.observe([old,self.observation_row(name='Current account')])
        result = self.engine.account_observation()
        self.assertTrue(result['connected'])
        rows = {r['name']:r for r in result['accounts']}
        self.assertTrue(rows['Cached account']['stale'])
        self.assertEqual(rows['Cached account']['equity'],512.25)
        self.assertTrue(rows['Cached account']['update_pending'])
        self.assertEqual(rows['Cached account']['observed_equity'],512.25)
        self.assertEqual(rows['Cached account']['observed_at'],old['observed_at'])
        self.assertFalse(rows['Current account']['stale'])
        self.now += 31
        result = self.engine.account_observation()
        self.assertFalse(result['connected'])
        self.assertTrue(all(r['stale'] and r['update_pending'] and r['equity']==512.25 for r in result['accounts']))

    def test_read_only_duplicate_names_are_kept_and_marked_ambiguous(self):
        self.observe([self.observation_row(),self.observation_row(equity=None,unrealized_pnl=None,balance=None),
                      self.observation_row(category='Funded')])
        rows = self.engine.account_observation()['accounts']
        self.assertEqual(len(rows),3)
        self.assertEqual(len({r['key'] for r in rows}),3)
        self.assertTrue(rows[0]['ambiguous'])
        self.assertTrue(rows[1]['ambiguous'])
        self.assertFalse(rows[2]['ambiguous'])
        self.assertIn('不能作为交易身份',rows[0]['read_error'])
        self.assertIsNone(rows[1]['balance'])

    def test_read_only_validation_is_atomic_and_rejects_invalid_schema(self):
        self.observe()
        before = copy.deepcopy(self.engine.data['read_only_observation'])
        invalid_rows = [None,[],{'name':''},self.observation_row(current=1),
                        self.observation_row(equity=True),self.observation_row(balance='500'),
                        self.observation_row(unrealized_pnl=float('nan')),
                        self.observation_row(equity=float('inf')),self.observation_row(equity=10**400),
                        self.observation_row(observed_at=True),self.observation_row(observed_at='now'),
                        self.observation_row(observed_at=self.now+60),self.observation_row(identity={})]
        for invalid in invalid_rows:
            with self.subTest(invalid=repr(invalid)), self.assertRaises(ValidationError):
                self.observe([self.observation_row(),invalid])
            self.assertEqual(self.engine.data['read_only_observation'],before)
        invalid_bodies = [None,[],{'accounts':[]}, {'tab_url':'https://next.vestmarkets.com/trade/NQ-PERP','accounts':{}},
                          {'tab_url':'https://next.vestmarkets.com/trade/NQ-PERP','accounts':[],'error':True},
                          {'tab_url':'https://evil.example/trade/NQ-PERP','accounts':[]},
                          {'tab_url':'https://next.vestmarkets.com/trade/BTC-PERP','accounts':[]},
                          {'tab_url':'https://next.vestmarkets.com.evil.example/trade/NQ-PERP','accounts':[]},
                          {'tab_url':'https://user:pass@next.vestmarkets.com/trade/NQ-PERP','accounts':[]}]
        for body in invalid_bodies:
            with self.subTest(body=body),self.assertRaises(ValidationError):
                self.engine.observe_accounts(body)
            self.assertEqual(self.engine.data['read_only_observation'],before)

    def test_read_only_observation_persists_but_is_stale_after_restart(self):
        runtime = Path(__file__).resolve().parent.parent/'runtime'
        runtime.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=runtime) as directory:
            db = str(Path(directory)/'observations.sqlite')
            engine = Engine(db,seed=False,clock=lambda:self.now)
            engine.observe_accounts({'tab_url':'https://next.vestmarkets.com/trade/NQ-PERP',
                                     'accounts':[self.observation_row()]})
            engine.close()
            engine = Engine(db,seed=False,clock=lambda:self.now)
            result = engine.account_observation()
            self.assertFalse(result['connected'])
            self.assertEqual(result['last_seen'],0)
            self.assertEqual(len(result['accounts']),1)
            self.assertTrue(result['accounts'][0]['stale'])
            self.assertEqual(result['accounts'][0]['balance'],500)
            self.assertTrue(result['accounts'][0]['update_pending'])
            self.assertEqual(result['accounts'][0]['observed_balance'],500)
            self.assertEqual(engine.state()['accounts'],[])
            engine.close()

    def test_read_only_error_does_not_leak_pairing_token(self):
        self.observe([],error='Pairing failed '+self.engine.bridge_token)
        self.assertNotIn(self.engine.bridge_token,self.engine.account_observation()['error'])

    def test_read_only_unread_timestamp_and_scan_progress(self):
        self.observe([self.observation_row(equity=None,unrealized_pnl=None,balance=None,observed_at=None)],
            progress={'completed':0,'total':1,'name':'Account 12','done':False,'restored':False})
        result = self.engine.account_observation()
        self.assertTrue(result['connected'])
        self.assertIsNone(result['accounts'][0]['observed_at'])
        self.assertIsNone(result['accounts'][0]['equity'])
        self.assertTrue(result['accounts'][0]['stale'])
        self.assertEqual(result['progress']['completed'],0)
        self.assertFalse(result['progress']['done'])
        with self.assertRaises(ValidationError):
            self.observe(progress={'completed':True,'total':1,'name':'Invalid'})

    def poll(self, aid, snapshot=None, **extra):
        return self.engine.bridge_poll({'account_id':aid,'ready':True,
            'tab_url':'https://next.vestmarkets.com/trade/NQ-PERP',
            'snapshot':snapshot or self.snapshot(), **extra})

    def browser_account(self):
        aid = self.account(mode='browser')
        self.engine.settings({'mode':'browser'})
        self.poll(aid)
        return aid

    def test_batch_waits_for_all_positions_then_jitter(self):
        ids = [self.account() for _ in range(4)]
        self.engine.start_strategy(self.strategy(ids))
        self.engine.tick(price=100)
        s = self.engine.state()['strategy']
        first_due = s['next_action_at']
        self.assertTrue(self.now+3 <= first_due <= self.now+7)
        self.assertEqual(len(self.engine.state()['positions']),1)
        self.now = first_due-0.01
        self.engine.tick(price=100)
        self.assertEqual(len(self.engine.state()['positions']),1)
        self.now = first_due
        self.engine.tick(price=100)
        self.assertEqual(len(self.engine.state()['positions']),2)
        first_ids = set(self.engine.state()['strategy']['batch_account_ids'])
        # Manually close only one: no new batch may start.
        self.engine.close_position(self.engine.state()['positions'][0]['id'])
        self.now += 100
        self.engine.tick(price=100)
        self.assertEqual(self.engine.state()['strategy']['batch_number'],1)
        self.engine.tick(price=111)
        s = self.engine.state()['strategy']
        self.assertEqual(s['status'],'waiting')
        self.assertTrue(self.now+5 <= s['next_action_at'] <= self.now+9)
        self.now = s['next_action_at']-0.01
        self.engine.tick(price=100)
        self.assertFalse(self.engine.state()['positions'])
        self.now += 0.01
        self.engine.tick(price=100)
        s = self.engine.state()['strategy']
        self.assertEqual(s['batch_number'],2)
        self.assertFalse(first_ids.intersection(s['batch_account_ids']))

    def test_stop_blocks_unsent_orders_but_keeps_existing_protection(self):
        ids = [self.account(),self.account()]
        self.engine.start_strategy(self.strategy(ids))
        self.engine.tick(price=100)
        self.engine.stop_strategy()
        self.now += 100
        self.engine.tick(price=111)
        self.assertEqual(len(self.engine.state()['history']),1)
        self.assertEqual(self.engine.state()['strategy']['status'],'stopped')
        self.assertFalse(self.engine.state()['positions'])

    def test_batch_pool_is_used_once_and_requires_full_batch(self):
        ids = [self.account() for _ in range(3)]
        self.engine.start_strategy(self.strategy(ids,order_delay_min=0,order_delay_max=0,
                                                batch_delay_min=0,batch_delay_max=0))
        self.engine.tick(price=100)
        self.engine.tick(price=100)
        self.engine.tick(price=111)
        self.engine.tick(price=111)
        self.assertEqual(self.engine.state()['strategy']['status'],'completed')
        self.assertEqual(len(self.engine.state()['history']),2)
        self.assertEqual(len(self.engine.state()['strategy']['remaining_accounts']),1)

    def test_take_profit_and_stop_loss_are_per_account(self):
        a, b = self.account(), self.account()
        self.engine.tick(price=100)
        self.engine.place_orders(self.order([a]))
        self.engine.place_orders(self.order([b], side='sell',idempotency_key='other-order-key'))
        self.engine.tick(price=111)
        history = self.engine.state()['history']
        self.assertEqual({x['account_id']:x['pnl'] for x in history},{a:11,b:-11})
        self.assertEqual({x['reason'] for x in history},{'take_profit','stop_loss'})
        self.assertEqual(sum(a['balance'] for a in self.engine.state()['accounts']),2000)

    def test_max_drawdown_flattens_and_locks_account(self):
        aid = self.account(max_loss=25)
        self.engine.tick(price=100)
        self.engine.place_orders(self.order([aid],stop_loss=100))
        self.engine.tick(price=70)
        state = self.engine.state()
        self.assertFalse(state['positions'])
        self.assertEqual(state['accounts'][0]['status'],'locked')
        self.assertEqual(state['history'][0]['reason'],'max_loss')

    def test_profit_target_marks_account_passed(self):
        aid = self.account(profit_target=10)
        self.engine.tick(price=100)
        self.engine.place_orders(self.order([aid]))
        self.engine.tick(price=111)
        self.assertEqual(self.engine.state()['accounts'][0]['status'],'passed')
        with self.assertRaises(ValidationError):
            self.engine.place_orders(self.order([aid],idempotency_key='new-order-key'))

    def test_idempotent_orders_cannot_duplicate_or_change_payload(self):
        aid = self.account()
        first = self.engine.place_orders(self.order([aid]))
        self.assertEqual(first,self.engine.place_orders(self.order([aid])))
        self.assertEqual(len(self.engine.state()['positions']),1)
        with self.assertRaises(ValidationError):
            self.engine.place_orders(self.order([aid],quantity=2))
        with self.assertRaises(ValidationError):
            self.engine.place_orders(self.order([aid],idempotency_key='another-key'))

    def test_batch_validation_is_atomic(self):
        a, b = self.account(),self.account()
        self.engine.toggle_account(b,False)
        with self.assertRaises(ValidationError):
            self.engine.place_orders(self.order([a,b]))
        self.assertFalse(self.engine.state()['positions'])
        for quantity in (0,-1,float('nan'),float('inf'),True,None):
            with self.subTest(quantity=quantity), self.assertRaises(ValidationError):
                self.engine.place_orders(self.order([a],quantity=quantity))
        for batch in (0,-1,1.5,True,3):
            with self.subTest(batch=batch), self.assertRaises(ValidationError):
                self.engine.start_strategy(self.strategy([a],batch_size=batch))
        with self.assertRaises(ValidationError):
            self.engine.start_strategy(self.strategy([a],batch_size=1,order_delay_min=10,order_delay_max=1))

    def test_restart_pauses_strategy_and_keeps_paper_protection(self):
        runtime = Path(__file__).resolve().parent.parent / 'runtime'
        runtime.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=runtime) as directory:
            db = str(Path(directory)/'test.sqlite')
            e = Engine(db,seed=False,clock=lambda:self.now,rng=random.Random(9))
            aid = e.add_account({'name':'Persist','initial_balance':1000,'profit_target':100,'max_loss':50})['id']
            e.start_strategy(self.strategy([aid],batch_size=1))
            e.tick(price=100)
            token = e.bridge_token
            e.close()
            e = Engine(db,seed=False,clock=lambda:self.now)
            self.assertEqual(e.state()['strategy']['status'],'paused')
            self.assertEqual(e.bridge_token,token)
            e.tick(price=111)
            self.assertFalse(e.state()['positions'])
            self.assertEqual(len(e.state()['history']),1)
            e.close()

    def test_browser_requires_fresh_armed_empty_snapshot(self):
        aid = self.account(mode='browser')
        self.engine.settings({'mode':'browser'})
        with self.assertRaises(ValidationError):
            self.engine.place_orders(self.order([aid]))
        self.poll(aid,ready=False)
        with self.assertRaises(ValidationError):
            self.engine.place_orders(self.order([aid]))
        self.poll(aid)
        self.now += 6
        with self.assertRaises(ValidationError):
            self.engine.place_orders(self.order([aid]))
        self.poll(aid,self.snapshot(position_open=None))
        with self.assertRaises(ValidationError):
            self.engine.place_orders(self.order([aid]))
        self.assertEqual(self.engine.state()['market']['source'],'unavailable')
        self.assertLess(len(self.engine.state()['candles']),2)

    def test_browser_job_is_delivered_once_and_confirms_actual_entry(self):
        aid = self.browser_account()
        self.engine.place_orders(self.order([aid]))
        job = self.poll(aid)['job']
        self.assertEqual(job['type'],'open')
        self.assertIsNone(self.poll(aid)['job'])
        result = self.engine.bridge_result({'job_id':job['id'],'success':True,'confirmed':True,
            'snapshot':self.snapshot(position_open=True,entry_price=99,price=101,unrealized_pnl=2)})
        self.assertTrue(result['ok'])
        position = self.engine.state()['positions'][0]
        self.assertEqual(position['entry_price'],99)
        self.assertEqual(position['mark_price'],101)
        self.assertTrue(self.engine.bridge_result({'job_id':job['id']})['duplicate'])
        self.assertEqual(len(self.engine.state()['positions']),1)

    def test_browser_missing_entry_freezes_instead_of_inventing_fill(self):
        aid = self.browser_account()
        self.engine.place_orders(self.order([aid]))
        job = self.poll(aid)['job']
        result = self.engine.bridge_result({'job_id':job['id'],'success':True,'confirmed':True,
            'snapshot':self.snapshot(position_open=True)})
        self.assertFalse(result['ok'])
        self.assertFalse(self.engine.state()['positions'])
        self.assertEqual(self.engine.state()['accounts'][0]['status'],'locked')

    def test_browser_wrong_side_or_quantity_never_confirms(self):
        self.engine.settings({'mode':'browser'})
        for override in ({'side':'sell'},{'quantity':2},{'symbol':'BTC-PERP'},{'position_id':None}):
            with self.subTest(override=override):
                self.engine.close()
                self.engine=Engine(':memory:',seed=False,clock=lambda:self.now,rng=random.Random(9))
                self.engine.settings({'mode':'browser'})
                aid = self.account(mode='browser')
                self.poll(aid)
                self.engine.place_orders(self.order([aid],idempotency_key='order-'+aid))
                job = self.poll(aid)['job']
                snapshot = self.snapshot(position_open=True,entry_price=100,**override)
                result = self.engine.bridge_result({'job_id':job['id'],'success':True,'confirmed':True,'snapshot':snapshot})
                self.assertFalse(result['ok'])
                self.assertFalse(self.engine.state()['positions'])

    def test_observation_updates_values_without_permitting_trades(self):
        aid = self.account(mode='browser')
        self.engine.settings({'mode':'browser'})
        self.poll(aid,self.snapshot(balance=1005,price=123),ready=False)
        state = self.engine.state()
        self.assertEqual(state['accounts'][0]['equity'],1005)
        self.assertFalse(state['accounts'][0]['ready'])
        self.assertEqual(state['market']['price'],123)
        with self.assertRaises(ValidationError):
            self.engine.place_orders(self.order([aid]))

    def test_browser_candles_only_aggregate_observed_prices_and_preserve_paper(self):
        aid = self.account(mode='browser')
        paper_candles = self.engine.state()['candles']
        self.engine.settings({'mode':'browser'})
        self.assertEqual(self.engine.state()['candles'],[])
        self.poll(aid,self.snapshot(price=100),ready=False)
        self.now += 1
        self.poll(aid,self.snapshot(price=105),ready=False)
        candle = self.engine.state()['candles'][0]
        self.assertEqual((candle['open'],candle['high'],candle['low'],candle['close'],candle['volume']), (100,105,100,105,0))
        self.now += 60
        self.poll(aid,self.snapshot(price=102),ready=False)
        self.assertEqual(len(self.engine.state()['candles']),2)
        self.engine.settings({'mode':'paper'})
        self.assertEqual(self.engine.state()['candles'],paper_candles)

    def test_browser_restart_freezes_delivered_job_and_does_not_redeliver(self):
        runtime = Path(__file__).resolve().parent.parent/'runtime'
        runtime.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=runtime) as directory:
            db = str(Path(directory)/'browser.sqlite')
            e = Engine(db,seed=False,clock=lambda:self.now)
            aid = e.add_account({'name':'Browser','initial_balance':1000,'profit_target':100,'max_loss':50,'mode':'browser'})['id']
            e.settings({'mode':'browser'})
            poll = {'account_id':aid,'ready':True,'tab_url':'https://next.vestmarkets.com/trade/NQ-PERP','snapshot':self.snapshot()}
            e.bridge_poll(poll)
            e.place_orders(self.order([aid]))
            self.assertIsNotNone(e.bridge_poll(poll)['job'])
            e.close()
            e = Engine(db,seed=False,clock=lambda:self.now)
            self.assertEqual(e.state()['accounts'][0]['status'],'locked')
            self.assertIsNone(e.bridge_poll(poll)['job'])
            e.close()

    def test_browser_timeout_freezes_and_never_redelivers(self):
        aid = self.browser_account()
        self.engine.place_orders(self.order([aid]))
        job = self.poll(aid)['job']
        self.now += 31
        self.engine.tick()
        self.assertIsNone(self.poll(aid)['job'])
        self.assertEqual(self.engine.state()['accounts'][0]['status'],'locked')
        with self.assertRaises(ValidationError):
            self.engine.bridge_result({'job_id':job['id'],'success':True,'confirmed':True,
                'snapshot':self.snapshot(position_open=True,entry_price=100)})

    def test_browser_close_uses_observed_cash_not_simulated_pnl(self):
        aid = self.browser_account()
        self.engine.place_orders(self.order([aid],take_profit=100))
        job = self.poll(aid)['job']
        self.engine.bridge_result({'job_id':job['id'],'success':True,'confirmed':True,
            'snapshot':self.snapshot(position_open=True,entry_price=100,price=101,unrealized_pnl=1)})
        pid = self.engine.state()['positions'][0]['id']
        self.engine.close_position(pid)
        close = self.poll(aid,self.snapshot(position_open=True,price=112,entry_price=100,unrealized_pnl=12))['job']
        self.assertEqual(close['type'],'close')
        self.assertEqual(close['position_id'],'vest-position-123')
        self.assertEqual(close['quantity'],1)
        self.assertEqual(close['side'],'buy')
        self.engine.bridge_result({'job_id':close['id'],'success':True,'confirmed':True,
            'snapshot':self.snapshot(position_open=False,balance=1011.75,price=112)})
        self.assertFalse(self.engine.state()['positions'])
        self.assertEqual(self.engine.state()['history'][0]['pnl'],11.75)

    def test_unknown_cancels_other_queued_opens(self):
        a = self.browser_account()
        b = self.account(mode='browser')
        self.poll(b)
        self.engine.place_orders(self.order([a,b]))
        job = self.poll(a)['job']
        self.engine.bridge_result({'job_id':job['id'],'success':False,'confirmed':False,'error':'Warning dialog'})
        self.assertIsNone(self.poll(b)['job'])
        self.assertFalse(self.engine.state()['positions'])

    def test_reconcile_requires_ack_fresh_flat_snapshot_and_no_unexpired_delivery(self):
        aid = self.browser_account()
        with self.assertRaises(ValidationError):
            self.engine.reconcile({'account_id':aid})
        self.engine.place_orders(self.order([aid]))
        job = self.poll(aid)['job']
        with self.assertRaises(ValidationError):
            self.engine.reconcile({'account_id':aid,'acknowledge':True})
        self.now += 31
        self.engine.tick()
        with self.assertRaises(ValidationError):
            self.engine.reconcile({'account_id':aid,'acknowledge':True})
        self.poll(aid,self.snapshot(position_open=True,entry_price=100),ready=False)
        with self.assertRaises(ValidationError):
            self.engine.reconcile({'account_id':aid,'acknowledge':True})
        self.poll(aid,self.snapshot(position_open=False,unrealized_pnl=5),ready=False)
        with self.assertRaises(ValidationError):
            self.engine.reconcile({'account_id':aid,'acknowledge':True})

    def test_reconcile_clears_unknown_without_fake_history_or_strategy_restart(self):
        aid = self.browser_account()
        self.engine.start_strategy(self.strategy([aid],batch_size=1))
        self.engine.tick(price=100)
        job = self.poll(aid)['job']
        self.engine.bridge_result({'job_id':job['id'],'success':True,'confirmed':True,
            'snapshot':self.snapshot(position_open=True,entry_price=100)})
        position = self.engine.state()['positions'][0]
        self.engine.close_position(position['id'])
        close = self.poll(aid,self.snapshot(position_open=True,entry_price=100))['job']
        self.engine.bridge_result({'job_id':close['id'],'success':False,'confirmed':False,'error':'timeout'})
        self.assertEqual(self.engine.state()['strategy']['status'],'paused')
        self.poll(aid,self.snapshot(position_open=False,balance=1012),ready=False)
        result = self.engine.reconcile({'account_id':aid,'acknowledge':True})
        self.assertTrue(result['ok'])
        state = self.engine.state()
        self.assertFalse(state['positions'])
        self.assertFalse(state['history'])
        self.assertEqual(state['accounts'][0]['status'],'active')
        self.assertEqual(state['accounts'][0]['balance'],1012)
        self.assertEqual(state['bridge']['pending_jobs'],0)
        self.assertEqual(state['strategy']['status'],'paused')
        self.assertEqual(len(self.engine.data['audit']),1)

    def test_reconcile_does_not_override_actual_risk_limit(self):
        aid = self.browser_account()
        self.poll(aid,self.snapshot(balance=940),ready=False)
        result = self.engine.reconcile({'account_id':aid,'acknowledge':True})
        self.assertEqual(result['status'],'locked')
        self.assertEqual(self.engine.state()['accounts'][0]['lock_reason'],'已达到配置的账户最大亏损')

    def test_history_csv_filters_mode_and_escapes_formula(self):
        aid = self.account(name='=UNSAFE()')
        self.engine.tick(price=100)
        self.engine.place_orders(self.order([aid]))
        self.engine.tick(price=111)
        exported = self.engine.history_csv()
        self.assertIn("'=UNSAFE()",exported)
        self.engine.settings({'mode':'browser'})
        self.assertNotIn('=UNSAFE',self.engine.history_csv())


class HTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = Engine(':memory:',seed=False)
        cls.server = create_server(cls.engine,port=0)
        cls.port = cls.server.server_port
        # The ephemeral test port differs from the requested zero port, so use
        # same-origin requests (without Origin) for positive API tests.
        cls.thread = threading.Thread(target=cls.server.serve_forever,daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(2)
        cls.engine.close()

    def request(self,method,path,body=None,headers=None):
        connection = http.client.HTTPConnection('127.0.0.1',self.port,timeout=3)
        connection.request(method,path,body=json.dumps(body) if body is not None else None,
                           headers=headers or {})
        response = connection.getresponse()
        result = (response.status,dict(response.getheaders()),response.read())
        connection.close()
        return result

    def test_health_and_json_errors(self):
        self.assertEqual(self.request('GET','/api/health')[0],200)
        self.assertEqual(self.request('POST','/api/accounts',{})[0],415)
        self.assertEqual(self.request('POST','/api/accounts',{}, {'Content-Type':'application/json'})[0],400)

    def test_strategy_lock_http_toggles_persisted_account_preference(self):
        account = self.engine.add_account({'name':'HTTP Lock','initial_balance':1000,'profit_target':100,'max_loss':50})
        path = '/api/accounts/'+account['id']+'/strategy-lock'
        headers = {'Content-Type':'application/json'}
        status,_,body = self.request('POST',path,{'locked':True},headers)
        self.assertEqual(status,200)
        self.assertTrue(json.loads(body)['strategy_locked'])
        self.assertEqual(self.request('POST',path,{'locked':'yes'},headers)[0],400)
        status,_,body = self.request('POST',path,{'locked':False},headers)
        self.assertEqual(status,200)
        self.assertFalse(json.loads(body)['strategy_locked'])

    def test_strategy_form_http_persists_without_starting_and_rejects_extension_origin(self):
        headers={'Content-Type':'application/json'}
        body=dict(stop_loss=177,skip_stop_loss_on_low_drawdown=True,drawdown_stop_loss_threshold=222)
        status,_,raw=self.request('POST','/api/strategy/config',body,headers)
        self.assertEqual(status,200)
        self.assertEqual(json.loads(raw)['drawdown_stop_loss_threshold'],222)
        self.assertEqual(json.loads(self.request('GET','/api/state')[2])['preferences']['strategy_config']['stop_loss'],177)
        self.assertEqual(self.request('POST','/api/strategy/config',{'drawdown_stop_loss_threshold':0},headers)[0],400)
        extension={**headers,'Origin':'chrome-extension://'+'a'*32,'X-Bridge-Token':self.engine.bridge_token}
        self.assertEqual(self.request('POST','/api/strategy/config',body,extension)[0],403)

    def test_host_origin_and_bridge_token_are_enforced(self):
        self.assertEqual(self.request('GET','/api/state',headers={'Host':'evil.example'})[0],403)
        self.assertEqual(self.request('GET','/api/bridge/config',headers={'Origin':'https://evil.example'})[0],403)
        extension = 'chrome-extension://'+'a'*32
        self.assertEqual(self.request('GET','/api/bridge/config',headers={'Origin':extension})[0],403)
        headers = {'Origin':extension,'Content-Type':'application/json'}
        self.assertEqual(self.request('POST','/api/bridge/poll',{},headers)[0],401)
        headers['X-Bridge-Token'] = self.engine.bridge_token
        self.assertEqual(self.request('GET','/api/bridge/accounts',headers=headers)[0],200)
        self.assertEqual(self.request('GET','/api/bridge/accounts',headers={'Origin':extension})[0],401)
        self.assertNotIn('token',json.loads(self.request('GET','/api/bridge/accounts',headers=headers)[2]))
        self.assertEqual(self.request('POST','/api/bridge/poll',{},headers)[0],404)
        self.assertEqual(self.request('OPTIONS','/api/bridge/poll',headers={'Origin':extension})[0],204)
        self.assertEqual(self.request('POST','/api/bridge/reconcile',{},headers)[0],403)
        self.assertEqual(self.request('POST','/api/accounts/anything/archive',{'acknowledge':True},headers)[0],403)

    def test_read_only_observation_origin_token_and_http_contract(self):
        extension = 'chrome-extension://'+'b'*32
        body = {'tab_url':'https://next.vestmarkets.com/trade/NQ-PERP','accounts':[]}
        headers = {'Origin':extension,'Content-Type':'application/json'}
        self.assertEqual(self.request('POST','/api/bridge/observe',body,headers)[0],401)
        headers['X-Bridge-Token'] = 'invalid-token'
        status,_,payload = self.request('POST','/api/bridge/observe',body,headers)
        self.assertEqual(status,401)
        self.assertNotIn('invalid-token',payload.decode())
        headers['X-Bridge-Token'] = self.engine.bridge_token
        self.assertEqual(self.request('POST','/api/bridge/observe',body,headers)[0],200)
        self.assertEqual(self.request('OPTIONS','/api/bridge/observe',headers={'Origin':extension})[0],204)
        self.assertEqual(self.request('GET','/api/account-observation',headers={'Origin':extension})[0],403)
        self.assertEqual(self.request('POST','/api/bridge/observe',body,
            {'Origin':'https://evil.example','Content-Type':'application/json','X-Bridge-Token':self.engine.bridge_token})[0],403)
        status,_,payload = self.request('GET','/api/account-observation')
        self.assertEqual(status,200)
        observation = json.loads(payload)
        self.assertTrue(observation['connected'])
        self.assertEqual(observation['accounts'],[])
        self.assertNotIn('token',observation)

    def test_percentage_size_http_requires_pairing_and_accepts_only_delivered_jobs(self):
        extension='chrome-extension://'+'e'*32
        headers={'Origin':extension,'Content-Type':'application/json'}
        self.assertEqual(self.request('POST','/api/bridge/size',{},headers)[0],401)
        headers['X-Bridge-Token']=self.engine.bridge_token
        self.assertEqual(self.request('OPTIONS','/api/bridge/size',headers={'Origin':extension})[0],204)
        self.assertEqual(self.request('POST','/api/bridge/size',{},headers)[0],409)

    def test_preparation_http_requires_pairing_and_never_grants_permission(self):
        before_sessions = copy.deepcopy(self.engine.data['execution_sessions'])
        before_jobs = copy.deepcopy(self.engine.data['jobs'])
        extension = 'chrome-extension://'+'d'*32
        headers = {'Origin':extension,'Content-Type':'application/json'}
        body = {'session_id':'http-preparation','mode_epoch':0,'tab_url':'https://next.vestmarkets.com/trade/NQ-PERP'}
        self.assertEqual(self.request('POST','/api/bridge/preparation',body,headers)[0],401)
        headers['X-Bridge-Token'] = self.engine.bridge_token
        self.assertEqual(self.request('OPTIONS','/api/bridge/preparation',headers={'Origin':extension})[0],204)
        status,_,payload = self.request('POST','/api/bridge/preparation',body,headers)
        self.assertEqual(status,200)
        self.assertFalse(json.loads(payload)['active'])
        self.assertEqual(self.engine.data['execution_sessions'],before_sessions)
        self.assertEqual(self.engine.data['jobs'],before_jobs)

    def test_readonly_inspection_http_requires_pairing_and_never_returns_a_job(self):
        now=self.engine.clock()
        account=self.engine.bridge_session({'session_id':'inspection-http-session','mode':'readonly','mode_epoch':1,
            'tab_url':'https://next.vestmarkets.com/trade/NDX-USD-PERP','accounts':[
                {'name':'Inspect A','category':'eval','balance':1000,'equity':1000,'unrealized_pnl':0,'current':True,'observed_at':now}]})['accounts'][0]
        body={'account_id':account['id'],'name':account['name'],'session_id':account['session_id'],
            'tab_url':'https://next.vestmarkets.com/trade/NDX-USD-PERP','snapshot':{'balance':1000,'equity':1000,
                'unrealized_pnl':0,'price':100,'symbol':'NQ-PERP','position_open':False,'observed_at':now}}
        headers={'Origin':'chrome-extension://'+'a'*32,'Content-Type':'application/json'}
        self.assertEqual(self.request('POST','/api/bridge/inspect',body,headers)[0],401)
        headers['X-Bridge-Token']=self.engine.bridge_token
        status,_,payload=self.request('POST','/api/bridge/inspect',body,headers)
        self.assertEqual(status,200)
        result=json.loads(payload)
        self.assertTrue(result['read_only'])
        self.assertNotIn('job',result)
        self.assertFalse(self.engine._ready(account['id']))

    def test_plugin_session_http_requires_token_and_does_not_grant_from_an_ordinary_heartbeat(self):
        extension = 'chrome-extension://'+'c'*32
        body = {'session_id':'http-session-123','mode':'readonly','mode_epoch':0,
            'tab_url':'https://next.vestmarkets.com/trade/NQ-PERP','accounts':[]}
        headers = {'Origin':extension,'Content-Type':'application/json'}
        self.assertEqual(self.request('POST','/api/bridge/session',body,headers)[0],401)
        headers['X-Bridge-Token'] = self.engine.bridge_token
        self.assertEqual(self.request('OPTIONS','/api/bridge/session',headers={'Origin':extension})[0],204)
        self.assertEqual(self.request('POST','/api/bridge/session',body,headers)[0],200)
        body['mode'] = 'execute'
        status,_,payload = self.request('POST','/api/bridge/session',body,headers)
        self.assertEqual(status,200)
        self.assertEqual(json.loads(payload)['mode'],'readonly')
        body.update(mode_epoch=1,authorize=True)
        status,_,payload = self.request('POST','/api/bridge/session',body,headers)
        self.assertEqual(status,200)
        self.assertEqual(json.loads(payload)['mode'],'execute')
        body.update(mode='readonly',mode_epoch=2,authorize=False)
        self.assertEqual(self.request('POST','/api/bridge/session',body,headers)[0],200)
        body.update(mode='execute',mode_epoch=1,authorize=True)
        self.assertEqual(self.request('POST','/api/bridge/session',body,headers)[0],409)


if __name__=='__main__':
    unittest.main()
