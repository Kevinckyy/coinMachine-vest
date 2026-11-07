import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from server.engine import Engine, ValidationError


class DailyEntryTests(unittest.TestCase):
    def setUp(self):
        self.now=datetime.fromisoformat('2026-11-04T20:00:00+00:00').timestamp()
        self.engine=Engine(':memory:',seed=False,clock=lambda:self.now)

    def tearDown(self):
        self.engine.close()

    def account(self, **extra):
        return self.engine.add_account({'name':'Daily','initial_balance':1000,'profit_target':100,'max_loss':500,**extra})['id']

    def order(self, aid, **extra):
        return {'account_ids':[aid],'side':'buy','symbol':'NQ-PERP','quantity':1,
                'idempotency_key':'daily-first-entry',**extra}

    def strategy(self, aid):
        return {'account_ids':[aid],'batch_size':1,'side':'buy','symbol':'NQ-PERP','quantity':1,
                'order_delay_min':0,'order_delay_max':0,'batch_delay_min':0,'batch_delay_max':0}

    def snapshot(self, **extra):
        return {'balance':1000,'equity':1000,'unrealized_pnl':0,'price':100,
                'position_open':False,'position_id':'daily-position','symbol':'NQ-PERP','side':'buy',
                'quantity':1,'entry_price':100,'observed_at':self.now,**extra}

    def session(self, category='eval', **extra):
        return self.engine.bridge_session({'session_id':'daily-browser-session','mode':'execute','mode_epoch':1,
            'authorize':True,'tab_url':'https://next.vestmarkets.com/trade/NQ-PERP','accounts':[
                {'name':'Daily browser','category':category,'balance':1000,'equity':1000,'unrealized_pnl':0,
                 'current':True,'observed_at':self.now,**extra}]})['accounts'][0]

    def poll(self, account, **extra):
        return self.engine.bridge_poll({'account_id':account['id'],'session_id':account['session_id'],
            'mode_epoch':1,'identity':account['identity'],'tab_url':'https://next.vestmarkets.com/trade/NQ-PERP',
            'ready':True,'snapshot':self.snapshot(**extra)})

    def browser_position(self):
        account=self.session()
        self.engine.settings({'mode':'browser'})
        self.engine.start_strategy(self.strategy(account['id']))
        self.engine.tick(price=100)
        job=self.poll(account)['job']
        self.engine.bridge_result({'job_id':job['id'],'session_id':account['session_id'],'success':True,'confirmed':True,
            'snapshot':self.snapshot(position_open=True)})
        return account,next(iter(self.engine.data['positions'].values()))

    def test_default_stop_130_no_take_profit_and_target_closes_then_locks(self):
        aid=self.account()
        config=self.engine.start_strategy(self.strategy(aid))['config']
        self.assertEqual(config['stop_loss'],130)
        self.assertIsNone(config['take_profit'])
        self.engine.tick(price=100)
        self.engine.tick(price=150)
        self.assertTrue(self.engine.data['positions'],'no fixed TP closes a profitable position early')
        self.engine.tick(price=200)
        account=self.engine.state()['accounts'][0]
        self.assertFalse(self.engine.data['positions'])
        self.assertEqual(self.engine.data['history'][0]['reason'],'target')
        self.assertEqual(account['status'],'passed')
        self.assertTrue(account['strategy_locked'])
        self.assertTrue(account['daily_entry_used'])

    def test_no_take_profit_still_stops_at_130_and_locks(self):
        aid=self.account(profit_target=1000)
        self.engine.tick(price=200)
        self.engine.start_strategy(self.strategy(aid))
        self.engine.tick(price=200)
        self.engine.tick(price=70)
        self.assertFalse(self.engine.data['positions'])
        self.assertEqual(self.engine.data['history'][0]['pnl'],-130)
        self.assertEqual(self.engine.data['history'][0]['reason'],'stop_loss')
        self.assertTrue(self.engine.state()['accounts'][0]['strategy_locked'])

    def test_manual_completion_locks_and_unlock_or_new_run_cannot_bypass_daily_limit(self):
        aid=self.account()
        self.engine.place_orders(self.order(aid))
        self.engine.close_position(next(iter(self.engine.data['positions'])))
        self.assertEqual(self.engine.data['accounts'][aid]['strategy_lock_source'],'daily_completed')
        self.engine.strategy_lock_account(aid,False)
        with self.assertRaisesRegex(ValidationError,'每日只允许一单'):
            self.engine.place_orders(self.order(aid,idempotency_key='daily-second-entry'))
        with self.assertRaises(ValidationError):
            self.engine.start_strategy(self.strategy(aid))

    def test_20_et_rollover_resets_entry_quota_and_automatically_unlocks(self):
        self.now=datetime.fromisoformat('2026-11-05T00:59:59+00:00').timestamp()
        aid=self.account()
        self.engine.place_orders(self.order(aid))
        self.engine.close_position(next(iter(self.engine.data['positions'])))
        self.now+=1
        account=self.engine.state()['accounts'][0]
        self.assertFalse(account['daily_entry_used'])
        self.assertFalse(account['strategy_locked'])
        self.engine.start_strategy(self.strategy(aid))
        self.engine.tick(price=100)
        self.assertTrue(self.engine.data['positions'])

    def test_cancelled_undelivered_request_does_not_consume_daily_quota(self):
        account=self.session()
        self.engine.settings({'mode':'browser'})
        self.engine.start_strategy(self.strategy(account['id']))
        self.engine.tick(price=100)
        self.engine.stop_strategy()
        self.assertFalse(self.engine._daily_entry_used(account['id']))
        self.engine.start_strategy(self.strategy(account['id']))

    def test_passed_browser_account_can_close_without_fixed_tp_and_locks_after_confirmation(self):
        account,position=self.browser_position()
        passed=self.session('passed',equity=1100,unrealized_pnl=100)
        close=self.poll(passed,position_open=True,equity=1100,unrealized_pnl=100,price=200)['job']
        self.assertEqual(close['type'],'close')
        self.assertEqual(close['reason'],'target')
        self.assertIsNone(close['take_profit'])
        self.assertIn(position['id'],self.engine.data['positions'])
        self.engine.bridge_result({'job_id':close['id'],'session_id':account['session_id'],'success':True,'confirmed':True,
            'snapshot':self.snapshot(balance=1100,equity=1100,price=200)})
        self.assertFalse(self.engine.data['positions'])
        self.assertEqual(self.engine.state()['accounts'][0]['strategy_lock_source'],'evaluation_passed')

    def test_native_target_close_cancels_unsent_close_instead_of_reporting_unknown(self):
        account,position=self.browser_position()
        queued=self.engine.close_position(position['id'],reason='target')
        passed=self.session('passed',balance=1100,equity=1100)
        self.assertIsNone(self.poll(passed,balance=1100,equity=1100,price=200)['job'])
        self.assertFalse(self.engine.data['positions'])
        self.assertTrue(all(j['status']!='unknown' for j in self.engine.data['jobs'].values()))
        self.assertEqual(self.engine.data['history'][0]['reason'],'target')
        self.assertTrue(any(j['type']=='close' and j['status']=='cancelled' for j in self.engine.data['jobs'].values()))

    def test_delivered_unknown_entry_keeps_daily_quota_used_after_restart(self):
        runtime=Path(__file__).resolve().parent.parent/'runtime'
        with tempfile.TemporaryDirectory(dir=runtime) as folder:
            self.engine.close()
            self.engine=Engine(str(Path(folder)/'daily.sqlite'),seed=False,clock=lambda:self.now)
            account=self.session()
            self.engine.settings({'mode':'browser'})
            self.engine.start_strategy(self.strategy(account['id']))
            self.engine.tick(price=100)
            self.poll(account)
            self.assertTrue(self.engine._daily_entry_used(account['id']))
            self.engine.close()
            self.engine=Engine(str(Path(folder)/'daily.sqlite'),seed=False,clock=lambda:self.now)
            try:
                self.assertTrue(self.engine.state()['accounts'][0]['daily_entry_used'])
                self.assertEqual(next(iter(self.engine.data['jobs'].values()))['status'],'unknown')
            finally:
                self.engine.close()
                self.engine=Engine(':memory:',seed=False,clock=lambda:self.now)
