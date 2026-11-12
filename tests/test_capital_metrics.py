import copy
import tempfile
import unittest
from pathlib import Path

from server.engine import Engine, ValidationError


class CapitalMetricsTests(unittest.TestCase):
    def setUp(self):
        self.now = 1793800000.0
        self.engine = Engine(':memory:',seed=False,clock=lambda:self.now)

    def tearDown(self):
        self.engine.close()

    def metric(self, current=12.5, limit=130, **extra):
        return {'text':f'${current} / ${limit}','current':current,'limit':limit,'percent':None,'observed_at':self.now,**extra}

    def read(self, metrics=None, *, name='Alpha', sid='capital-session-1', category='eval', scanning=False, position_open=False):
        row = {'name':name,'category':category,'current':True,'balance':1000,'equity':1000,
               'unrealized_pnl':0,'observed_at':self.now}
        if metrics is not None:
            row['capital_metrics'] = metrics
        if metrics and metrics.get('health'):
            health = metrics['health']
            row['phase_snapshot'] = dict(account_health=health['percent'],observed_at=health['observed_at'],position_open=position_open)
        if scanning:
            row['scan_status'] = 'success'
        return self.engine.bridge_session({'session_id':sid,'mode':'readonly','mode_epoch':0,
            'progress':{'completed':1,'total':1} if scanning else None,
            'tab_url':'https://next.vestmarkets.com/trade/NQ-PERP','accounts':[row]})['accounts'][0]

    def test_all_three_metrics_roundtrip_with_independent_timestamps_and_no_trading_limit_substitution(self):
        account = self.read({'daily_loss':self.metric(),'drawdown':self.metric(30,500),'goal':self.metric(60,300)})
        self.assertEqual(account['capital_metrics']['daily_loss']['limit'],130)
        self.assertEqual(account['capital_metrics']['drawdown']['current'],30)
        self.assertEqual(account['capital_metrics']['goal']['limit'],300)
        self.assertIsNone(account['profit_target'])
        self.assertIsNone(account['max_loss'])
        self.assertFalse(self.engine.data['jobs'])
        self.assertFalse(self.engine.data['positions'])
        self.assertEqual(self.engine.account_observation()['accounts'][0]['capital_metrics'],account['capital_metrics'])
        self.now += 31
        cached = self.engine.state()['accounts'][0]['capital_metrics']
        self.assertEqual(cached['goal']['observed_at'],self.now-31)
        self.assertEqual(cached['goal']['limit'],300)

    def test_missing_fields_keep_last_value_pending_and_never_refresh_its_timestamp(self):
        first = self.read({'daily_loss':self.metric(),'drawdown':self.metric(0,400),'goal':self.metric(5,300)})
        self.now += 2
        second = self.read({'daily_loss':None,'drawdown':self.metric(2,400),'goal':None})
        self.assertEqual(second['capital_metrics']['daily_loss']['observed_at'],first['capital_metrics']['daily_loss']['observed_at'])
        self.assertTrue(second['capital_metrics']['daily_loss']['update_pending'])
        self.assertFalse(second['capital_metrics']['drawdown']['update_pending'])
        self.assertEqual(second['capital_metrics']['drawdown']['observed_at'],self.now)
        self.now += 1
        older_plugin = self.read()
        self.assertTrue(all(m['update_pending'] for m in older_plugin['capital_metrics'].values() if m is not None))
        self.now += 1
        updated = self.read({'daily_loss':self.metric(0,120)})
        self.assertEqual(updated['capital_metrics']['daily_loss']['current'],0)
        self.assertFalse(updated['capital_metrics']['daily_loss']['update_pending'])

    def test_percentage_only_and_absent_metrics_remain_distinct_from_zero_or_unlimited(self):
        account = self.read({'daily_loss':self.metric(None,None,text='3%',percent=3)})
        self.assertEqual(account['capital_metrics']['daily_loss']['percent'],3)
        self.assertIsNone(account['capital_metrics']['daily_loss']['limit'])
        self.assertIsNone(account['capital_metrics']['goal'])
        self.assertIsNone(account['capital_metrics']['drawdown'])

    def test_health_below_25_locks_through_trading_day_even_after_recovery(self):
        health = lambda percent, **extra: self.metric(None, None, text=f'{percent}%', percent=percent, **extra)
        self.assertFalse(self.read({'health':health(1)})['health_locked'])
        self.assertFalse(self.read({'health':health(1)},scanning=True,position_open=True)['health_locked'])
        account = self.read({'health':health(25)},scanning=True)
        self.assertFalse(account['health_locked'])
        stale = self.read({'health':health(1, observed_at=self.now-6)},scanning=True)
        self.assertFalse(stale['health_locked'])
        self.now += 1
        locked = self.read({'health':health(24.99)},scanning=True)
        self.assertTrue(locked['health_locked'])
        self.assertEqual(locked['capital_metrics']['health']['percent'],24.99)
        self.assertEqual(locked['health_unlock_at'],self.engine.state()['entry_day']['end'])
        self.now += 1
        self.assertTrue(self.read({'health':health(100)},scanning=True)['health_locked'])
        self.now = locked['health_unlock_at']
        self.assertFalse(self.read()['health_locked'])
        self.assertTrue(self.read({'health':health(0)},scanning=True)['health_locked'])

    def test_only_scan_and_confirmed_close_lock_and_monitor_never_delivers(self):
        tab_url = 'https://next.vestmarkets.com/trade/NQ-PERP'
        rows = [dict(name=name,category='eval',current=False,balance=5000,equity=5000,
                     unrealized_pnl=0,observed_at=self.now) for name in ('Alpha','Beta')]
        session = dict(session_id='health-test-session',mode='execute',mode_epoch=1,authorize=True,tab_url=tab_url,accounts=rows)
        accounts = self.engine.bridge_session(session)['accounts']
        self.engine.settings({'mode':'browser'})
        self.engine.place_orders(dict(account_ids=[a['id'] for a in accounts],side='buy',symbol='NQ-PERP',
            quantity=4,take_profit=200,stop_loss=130,idempotency_key='test-health-lock'))
        snapshot = dict(balance=5000,equity=5000,unrealized_pnl=0,price=28000,
                        position_open=False,observed_at=self.now,account_health=24)
        rows[0].update(scan_status='success',phase_snapshot=snapshot)
        self.engine.bridge_session({**session,'authorize':False,'progress':{'completed':1,'total':2}})
        alpha, beta = (self.engine.data['accounts'][a['id']] for a in accounts)
        with self.assertRaisesRegex(ValidationError,'健康度锁定'):
            self.engine._eligible(alpha['id'])
        self.assertFalse(self.engine._daily_entry_used(alpha['id']))
        self.assertFalse(self.engine._pending(alpha['id']))

        request = dict(account_id=beta['id'],session_id=session['session_id'],mode_epoch=1,
                       identity=beta['identity'],tab_url=tab_url,ready=True)
        self.assertIsNone(self.engine.bridge_poll({**request,'snapshot':snapshot},dispatch=False)['job'])
        self.assertEqual(self.engine._pending(beta['id'])[0]['status'],'queued')
        job = self.engine.bridge_poll({**request,'snapshot':snapshot})['job']
        self.assertEqual(job['status'],'delivered')
        opened = {**snapshot,'position_open':True,'symbol':'NQ-PERP','position_id':'health-position',
                  'side':'buy','quantity':4,'entry_price':28000}
        self.assertEqual(self.engine.bridge_result({**request,'job_id':job['id'],'success':True,
            'confirmed':True,'snapshot':opened})['status'],'confirmed')
        self.assertFalse(self.engine._health_locked(beta))

        protected = {**opened,'unrealized_pnl':-130,'equity':4870}
        self.assertIsNone(self.engine.bridge_poll({**request,'snapshot':protected},dispatch=False)['job'])
        self.assertFalse(self.engine._health_locked(beta))
        closing = self.engine._pending(beta['id'])[0]
        self.assertEqual((closing['type'],closing['status']),('close','queued'))
        routed = self.engine.bridge_session({**session,'authorize':False})['next_account']
        self.assertEqual((routed['id'],routed['operation']),(beta['id'],'close'))

        # The native order closed before the queued fallback was dispatched.
        closed = {**snapshot,'balance':4870,'equity':4870,'account_health':20}
        for _ in range(2):
            self.assertIsNone(self.engine.bridge_poll({**request,'snapshot':closed},dispatch=False)['job'])
        self.assertEqual(closing['status'],'cancelled')
        self.assertEqual(len(self.engine.data['history']),1)
        self.assertEqual(beta['capital_metrics']['health']['percent'],20)
        self.assertEqual(self.engine.data['execution_sessions'][session['session_id']]['mode'],'execute')
        self.assertFalse(self.engine.data['positions'])
        for account in self.engine.state()['accounts']:
            self.assertTrue(account['health_locked'])
            self.assertTrue(account['executable'])  # The same execution permission is needed for closing.
            self.assertFalse(account['strategy_executable'])

    def test_out_of_order_and_other_accounts_cannot_overwrite_current_metric_values(self):
        original = self.read({'goal':self.metric(20,300)})
        self.now += 5
        fresh = self.read({'goal':self.metric(30,350)},sid='capital-session-2')
        old = self.read({'goal':original['capital_metrics']['goal']})
        self.assertEqual(old['capital_metrics']['goal']['limit'],350)
        self.assertEqual(old['capital_metrics']['goal']['observed_at'],fresh['capital_metrics']['goal']['observed_at'])
        other = self.read(name='Beta')
        self.assertTrue(all(value is None for value in other['capital_metrics'].values()))
        self.now += 1
        transitioned = self.read(category='funded')
        self.assertTrue(transitioned['capital_metrics']['goal']['update_pending'])
        self.assertEqual(transitioned['capital_metrics']['goal']['category'],'eval')

    def test_invalid_metrics_reject_without_overwriting_existing_observations(self):
        self.read({'goal':self.metric(0,300)})
        previous = copy.deepcopy(self.engine.data['read_only_observation'])
        for metric in [True,{},self.metric(limit=-1),self.metric(current=True),self.metric(percent=10),
                       self.metric(observed_at=self.now+10),self.metric(current=float('nan')),
                       self.metric(text='x'*161),self.metric(limit='130')]:
            with self.subTest(metric=metric):
                with self.assertRaises(ValidationError):
                    self.read({'daily_loss':metric})
                self.assertEqual(self.engine.data['read_only_observation'],previous)

    def test_restart_and_legacy_name_merge_retain_latest_reading_for_each_metric(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent.parent/'runtime') as folder:
            self.engine.close()
            database = str(Path(folder)/'capital.sqlite')
            self.engine = Engine(database,seed=False,clock=lambda:self.now)
            account = self.read({'daily_loss':self.metric(0,130),'goal':self.metric(20,300)})
            duplicate = copy.deepcopy(self.engine.data['accounts'][account['id']])
            duplicate['id'] = 'legacy-capital-copy'
            duplicate['capital_metrics']['daily_loss'] = self.metric(2,140,observed_at=self.now+1)
            duplicate['capital_metrics']['goal'] = None
            self.engine.data['accounts'][duplicate['id']] = duplicate
            self.now += 2
            self.engine.close()
            self.engine = Engine(database,seed=False,clock=lambda:self.now)
            try:
                saved = self.engine.state()['accounts']
                self.assertEqual(len(saved),1)
                self.assertEqual(saved[0]['capital_metrics']['daily_loss']['limit'],140)
                self.assertEqual(saved[0]['capital_metrics']['goal']['limit'],300)
                self.assertEqual(saved[0]['capital_metrics']['goal']['observed_at'],self.now-2)
            finally:
                self.engine.close()
                self.engine = Engine(':memory:',seed=False,clock=lambda:self.now)
