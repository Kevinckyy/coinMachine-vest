import copy
import tempfile
import unittest
from pathlib import Path

from server.engine import Engine, ValidationError


class StrategySettingsTests(unittest.TestCase):
    def setUp(self):
        self.now = 1793955000.0
        self.e = Engine(':memory:', seed=False, clock=lambda:self.now)

    def tearDown(self):
        self.e.close()

    def config(self, **extra):
        return dict(batch_size=1,size_percent=25,side='buy',symbol='NQ-PERP',take_profit=None,stop_loss=130,
            order_delay_min=0,order_delay_max=0,batch_delay_min=1,batch_delay_max=2,
            skip_stop_loss_on_low_drawdown=True,drawdown_stop_loss_threshold=200,**extra)

    def metric(self, remaining=199, **extra):
        return {**dict(text=f'${1000-remaining} / $1000',current=1000-remaining,limit=1000,
            percent=None,phase='1/2',observed_at=self.now),**extra}

    def session(self, metric):
        return self.e.bridge_session(dict(session_id='rule-session',mode='execute',mode_epoch=1,authorize=True,
            tab_url='https://next.vestmarkets.com/trade/NQ-PERP',accounts=[dict(name='Rule account',category='eval',
                phase='1/2',phase_observed_at=self.now,current=True,balance=1000,equity=1000,unrealized_pnl=0,
                observed_at=self.now,capital_metrics={'drawdown':metric})]))['accounts'][0]

    def snap(self, **extra):
        return {**dict(phase='1/2',balance=1000,equity=1000,unrealized_pnl=0,price=100,
            position_open=False,symbol='NQ-PERP',observed_at=self.now),**extra}

    def poll(self, a, **extra):
        return self.e.bridge_poll(dict(account_id=a['id'],session_id=a['session_id'],mode_epoch=1,identity=a['identity'],
            phase='1/2',tab_url='https://next.vestmarkets.com/trade/NQ-PERP',ready=True,snapshot=self.snap(**extra)))

    def queued(self, metric):
        a=self.session(metric)
        self.e.settings({'mode':'browser'})
        self.e.start_strategy({**self.config(),'account_ids':[a['id']]})
        self.e.tick(price=100)
        return a

    def size(self, a, job, metric, stop):
        return self.e.bridge_size(dict(job_id=job['id'],account_id=a['id'],session_id=a['session_id'],mode_epoch=1,
            identity=a['identity'],phase='1/2',tab_url='https://next.vestmarkets.com/trade/NQ-PERP',
            size_percent=25,quantity=2,snapshot=self.snap(),drawdown=metric,stop_loss=stop))

    def assert_decision(self, remaining, expected):
        a=self.queued(self.metric(remaining))
        job=self.poll(a)['job']
        self.assertEqual(job['configured_stop_loss'],130)
        result=self.size(a,job,self.metric(remaining),expected)
        self.assertEqual(result['stop_loss'],expected)
        self.assertEqual(result['stop_loss_omitted'],expected is None)
        self.e.bridge_result(dict(job_id=job['id'],session_id=a['session_id'],success=True,confirmed=True,
            snapshot=self.snap(position_open=True,position_id='actual-position',side='buy',quantity=2,entry_price=100)))
        position=self.e.state()['positions'][0]
        self.assertEqual(position['stop_loss'],expected)
        self.assertEqual(position['remaining_drawdown_at_entry'],remaining)
        if expected is None:
            self.assertIsNone(self.poll(a,position_open=True,position_id='actual-position',side='buy',quantity=2,
                entry_price=100,balance=1000,equity=850,unrealized_pnl=-150)['job'])
            self.assertIsNone(self.e.state()['positions'][0]['stop_loss'])

    def test_below_threshold_omits_stop_in_job_and_position_without_local_stop_exit(self):
        self.assert_decision(199.99,None)

    def test_equal_threshold_keeps_configured_stop(self):
        self.assert_decision(200,130)

    def test_above_threshold_keeps_configured_stop(self):
        self.assert_decision(200.01,130)

    def test_missing_or_stale_metric_waits_without_delivery_or_daily_quota(self):
        a=self.queued(None)
        self.assertIsNone(self.poll(a)['job'])
        self.assertEqual(next(iter(self.e.data['jobs'].values()))['status'],'queued')
        self.assertFalse(self.e.state()['accounts'][0]['daily_entry_used'])
        a=self.session(self.metric(observed_at=self.now-6))
        self.assertIsNone(self.poll(a)['job'])
        a=self.session(self.metric())
        job=self.poll(a)['job']
        self.assertEqual(job['status'],'delivered')
        for metric in [None,self.metric(observed_at=self.now-6),self.metric(observed_at=self.now+1),
                       self.metric(phase='2/2'),self.metric(current=None,limit=None,percent=80),self.metric(update_pending=True)]:
            with self.subTest(metric=metric),self.assertRaises(ValidationError):
                self.size(a,job,metric,None)
        self.assertNotIn('sized_at',self.e.data['jobs'][job['id']])

    def test_incorrect_stop_and_changed_decision_cannot_pin_or_rewrite_job(self):
        a=self.queued(self.metric(201));job=self.poll(a)['job']
        with self.assertRaises(ValidationError):
            self.size(a,job,self.metric(201),None)
        self.assertNotIn('quantity',self.e.data['jobs'][job['id']])
        self.size(a,job,self.metric(201),130)
        with self.assertRaises(ValidationError):
            self.size(a,job,self.metric(199),None)
        self.assertEqual(self.e.data['jobs'][job['id']]['stop_loss'],130)

    def test_disabled_rule_needs_no_drawdown_and_keeps_stop(self):
        a=self.session(None);self.e.settings({'mode':'browser'})
        cfg={**self.config(),'skip_stop_loss_on_low_drawdown':False,'account_ids':[a['id']]}
        self.e.start_strategy(cfg);self.e.tick(price=100)
        job=self.poll(a)['job']
        self.assertEqual(job['stop_loss'],130)
        self.size(a,job,None,130)
        self.assertEqual(self.e.data['jobs'][job['id']]['stop_loss'],130)

    def test_paper_rule_omits_only_trade_stop_and_preserves_account_loss_limit(self):
        a=self.e.add_account(dict(name='Paper rule',initial_balance=1000,profit_target=1000,max_loss=150))
        self.e.start_strategy({**self.config(),'stop_loss':10,'account_ids':[a['id']]})
        self.e.tick(price=100)
        self.assertIsNone(self.e.state()['positions'][0]['stop_loss'])
        self.e.tick(price=92)
        self.assertTrue(self.e.state()['positions'])
        self.e.tick(price=40)
        self.assertFalse(self.e.state()['positions'])
        self.assertEqual(self.e.data['history'][-1]['reason'],'max_loss')

    def test_unstarted_form_persists_every_parameter_across_service_restart(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]/'runtime') as folder:
            path=str(Path(folder)/'settings.sqlite')
            e=Engine(path,seed=False)
            cfg={**self.config(),'batch_size':7,'size_percent':75,'side':'sell','stop_loss':177.5,
                'order_delay_min':3.2,'order_delay_max':7.9,'batch_delay_min':21,'batch_delay_max':43,
                'drawdown_stop_loss_threshold':222.5}
            saved=e.save_strategy_config(cfg);e.close()
            e=Engine(path,seed=False)
            try:
                self.assertEqual(e.state()['preferences']['strategy_config'],saved)
                self.assertEqual(e.data['strategy']['status'],'idle')
                self.assertFalse(e.data['jobs'])
            finally:e.close()

    def test_saving_form_does_not_change_running_config_or_schedule(self):
        a=self.e.add_account(dict(name='Paper',initial_balance=1000,profit_target=1000,max_loss=150))
        self.e.start_strategy({**self.config(),'account_ids':[a['id']]})
        running=copy.deepcopy(self.e.data['strategy'])
        self.e.save_strategy_config({**self.config(),'batch_size':9,'stop_loss':180})
        self.assertEqual(self.e.data['strategy'],running)
        self.e.stop_strategy()
        self.assertEqual(self.e.state()['preferences']['strategy_config']['stop_loss'],180)

    def test_invalid_settings_leave_the_saved_form_unchanged(self):
        before=copy.deepcopy(self.e.state()['preferences']['strategy_config'])
        for patch in [dict(batch_size=True),dict(batch_size=0),dict(batch_size=1.5),dict(stop_loss=None),
                      dict(drawdown_stop_loss_threshold=0),dict(drawdown_stop_loss_threshold=True),
                      dict(skip_stop_loss_on_low_drawdown='false'),dict(order_delay_min=9,order_delay_max=8)]:
            with self.subTest(patch=patch),self.assertRaises(ValidationError):self.e.save_strategy_config(patch)
            self.assertEqual(self.e.state()['preferences']['strategy_config'],before)

    def test_upgrade_restores_last_run_parameters_without_resuming_strategy(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]/'runtime') as folder:
            path=str(Path(folder)/'legacy.sqlite');e=Engine(path,seed=False)
            a=e.add_account(dict(name='Legacy',initial_balance=1000,profit_target=1000,max_loss=150))
            e.start_strategy({**self.config(),'stop_loss':177,'account_ids':[a['id']]})
            del e.data['preferences']['strategy_config'];e._save();e.close()
            e=Engine(path,seed=False)
            try:
                self.assertEqual(e.state()['preferences']['strategy_config']['stop_loss'],177)
                self.assertEqual(e.data['strategy']['status'],'paused')
                self.assertFalse(e.data['jobs'])
            finally:e.close()
