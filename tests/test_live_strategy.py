import copy
import tempfile
import unittest
from pathlib import Path
from server.engine import Engine, ValidationError


class LiveStrategyTests(unittest.TestCase):
    def setUp(self):
        self.now=1793955000.0
        self.e=Engine(':memory:',seed=False,clock=lambda:self.now)

    def tearDown(self):
        self.e.close()

    def account(self, name='Live', phase='live'):
        a=self.e.add_account(dict(name=name,initial_balance=1000,profit_target=10000,max_loss=500,mode='paper'))
        self.e.data['accounts'][a['id']].update(phase=phase,category='funded' if phase=='live' else 'eval')
        self.e._roll_strategy_locks()
        return self.e.data['accounts'][a['id']]

    def config(self, ids, **extra):
        return dict(account_ids=ids,batch_size=1,size_percent=25,side='buy',symbol='NQ-PERP',stop_loss=130,
            order_delay_min=0,order_delay_max=0,batch_delay_min=0,batch_delay_max=0,**extra)

    def test_default_lock_excludes_live_only_and_survives_daily_reset(self):
        live=self.account();exam=self.account('Exam','2/2')
        self.assertEqual(live['strategy_lock_source'],'live_default')
        observed=next(a for a in self.e.state()['accounts'] if a['id']==live['id'])
        self.assertIsNone(observed['strategy_unlock_at'])
        self.assertFalse(exam['strategy_locked'])
        self.now+=86400;self.e._roll_strategy_locks()
        self.assertTrue(live['strategy_locked'])
        with self.assertRaisesRegex(ValidationError,'已锁定'):
            self.e.start_strategy(self.config([live['id']]))

    def test_manual_unlock_survives_heartbeat_daily_reset_and_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            e=Engine(Path(folder)/'test.sqlite',seed=False,clock=lambda:self.now)
            try:
                a=e.add_account(dict(name='Persistent Live',initial_balance=1000,profit_target=10000,max_loss=500))
                e.data['accounts'][a['id']].update(phase='live',category='funded');e._roll_strategy_locks()
                e.strategy_lock_account(a['id'],False)
                self.now+=86400;e._roll_strategy_locks();e._save();e.close()
                e=Engine(Path(folder)/'test.sqlite',seed=False,clock=lambda:self.now)
                self.assertFalse(e.data['accounts'][a['id']]['strategy_locked'])
                self.assertTrue(e.data['accounts'][a['id']]['live_default_lock_exempt'])
            finally:e.close()

    def test_switch_off_clears_default_only_and_does_not_enqueue_accounts(self):
        default=self.account();manual=self.account('Manual');risk=self.account('Risk')
        self.e.strategy_lock_account(manual['id'],True)
        self.e._lock_strategy(risk,'Stop reached','stop_loss')
        self.e.save_strategy_config({'live_default_locked':False})
        self.assertFalse(default['strategy_locked'])
        self.assertEqual(manual['strategy_lock_source'],'manual')
        self.assertEqual(risk['strategy_lock_source'],'stop_loss')
        self.assertFalse(self.e.data['strategy']['remaining_accounts'])

    def test_live_and_exam_open_with_independent_parameters(self):
        live=self.account();exam=self.account('Exam','1/2')
        cfg=self.config([live['id'],exam['id']],live_default_locked=False,live_size_percent=75,
            live_side='sell',live_take_profit=90,live_stop_loss=41)
        self.e.start_strategy(cfg);self.e.tick(price=100)
        lp=self.e._position(live['id'])
        self.assertEqual((lp['side'],lp['take_profit'],lp['stop_loss'],lp['size_percent']),('sell',90,41,75))
        self.assertIsNone(self.e._position(exam['id']))
        self.e.close_position(lp['id'])
        for _ in range(4):self.e.tick(price=100)
        ep=self.e._position(exam['id'])
        self.assertEqual((ep['side'],ep['take_profit'],ep['stop_loss'],ep['size_percent']),('buy',None,130,25))

    def test_live_drawdown_policy_uses_live_threshold_and_can_omit_stop(self):
        live=self.account()
        self.e.start_strategy(self.config([live['id']],live_default_locked=False,
            live_skip_stop_loss_on_low_drawdown=True,live_drawdown_stop_loss_threshold=600,live_stop_loss=44))
        self.e.tick(price=100);p=self.e._position(live['id'])
        self.assertIsNone(p['stop_loss']);self.assertEqual(p['configured_stop_loss'],44)
        self.assertEqual(p['remaining_drawdown_at_entry'],500)

    def test_saved_profile_does_not_change_existing_position_or_run(self):
        live=self.account();self.e.strategy_lock_account(live['id'],False)
        self.e.start_strategy(self.config([live['id']],live_stop_loss=42,live_take_profit=81))
        self.e.tick(price=100);position=copy.deepcopy(self.e._position(live['id']));run=copy.deepcopy(self.e.data['strategy']['config'])
        self.e.save_strategy_config({'live_stop_loss':99,'live_take_profit':200,'live_side':'sell'})
        self.assertEqual(self.e._position(live['id']),position);self.assertEqual(self.e.data['strategy']['config'],run)

    def test_start_without_live_fields_uses_saved_live_profile(self):
        a=self.account();self.e.save_strategy_config({'live_default_locked':False,'live_take_profit':88,'live_stop_loss':33,'live_side':'sell'})
        self.e.start_strategy(self.config([a['id']]))
        self.e.tick(price=100);p=self.e._position(a['id'])
        self.assertEqual((p['side'],p['take_profit'],p['stop_loss']),('sell',88,33))

    def test_transition_with_default_lock_off_rejoins_original_pool(self):
        self.e.save_strategy_config({'live_default_locked':False})
        a=self.account('Unlocked transition','2/2');s=self.e.data['strategy']
        s.update(status='running',phase='monitoring',config=self.config([a['id']]),remaining_accounts=[],order_queue=[])
        self.assertTrue(self.e._apply_phase_observation(a,dict(phase='live',phase_observed_at=self.now)))
        self.assertFalse(a['strategy_locked']);self.assertIn(a['id'],s['remaining_accounts'])

    def test_config_validation_and_optional_exits(self):
        saved=self.e.save_strategy_config({'live_stop_loss':None,'live_take_profit':None})
        self.assertIsNone(saved['live_stop_loss'])
        for patch in ({'live_default_locked':1},{'live_skip_stop_loss_on_low_drawdown':'true'},
                      {'live_side':'both'},{'live_size_percent':True},{'live_size_percent':30},
                      {'live_stop_loss':0},{'live_take_profit':-1},{'live_take_profit':True},
                      {'live_drawdown_stop_loss_threshold':0},{'live_skip_stop_loss_on_low_drawdown':True}):
            with self.subTest(patch=patch),self.assertRaises(ValidationError):self.e.save_strategy_config(patch)
            self.assertEqual(self.e.data['preferences']['strategy_config'],saved)

    def test_transition_locks_new_live_before_it_can_rejoin_running_pool(self):
        a=self.account('Transition','2/2');s=self.e.data['strategy']
        s.update(status='running',phase='monitoring',config=self.config([a['id']]),remaining_accounts=[],order_queue=[])
        row=dict(phase='live',phase_observed_at=self.now,current=True,category='funded',balance=1000,
            unrealized_pnl=0,phase_snapshot=dict(phase='live',balance=1000,unrealized_pnl=0,price=100,
                position_open=False,observed_at=self.now))
        self.assertTrue(self.e._apply_phase_observation(a,row))
        self.assertEqual(a['strategy_lock_source'],'live_default');self.assertNotIn(a['id'],s['remaining_accounts'])
        self.e.save_strategy_config({'live_default_locked':False})
        self.assertFalse(a['strategy_locked'])

    def test_live_take_profit_and_stop_loss_close_at_live_limits(self):
        for price,reason in ((111,'take_profit'),(94,'stop_loss')):
            with self.subTest(reason=reason):
                self.e.close();self.e=Engine(':memory:',seed=False,clock=lambda:self.now)
                a=self.account();self.e.start_strategy(self.config([a['id']],live_default_locked=False,
                    live_size_percent=100,live_side='buy',live_take_profit=100,live_stop_loss=50))
                self.e.tick(price=100);self.e.tick(price=price)
                self.assertIsNone(self.e._position(a['id']))
                self.assertEqual(self.e.data['history'][-1]['reason'],reason)

    def test_browser_live_job_and_sizing_use_live_drawdown_and_protection(self):
        self.e.settings({'mode':'browser'})
        metric=dict(text='960 / 1000',current=960,limit=1000,percent=None,phase='live',observed_at=self.now)
        row=dict(name='Browser Live',phase='live',phase_observed_at=self.now,category='funded',current=True,
            balance=1000,equity=1000,unrealized_pnl=0,observed_at=self.now,capital_metrics={'drawdown':metric})
        a=self.e.bridge_session(dict(session_id='live-browser',mode='execute',mode_epoch=1,authorize=True,
            tab_url='https://next.vestmarkets.com/trade/NQ-PERP',accounts=[row]))['accounts'][0]
        self.e.start_strategy(self.config([a['id']],live_default_locked=False,live_size_percent=75,
            live_side='sell',live_take_profit=100,live_stop_loss=44,
            live_skip_stop_loss_on_low_drawdown=True,live_drawdown_stop_loss_threshold=50))
        self.e.tick(price=100)
        snap=dict(phase='live',balance=1000,equity=1000,unrealized_pnl=0,price=100,
            position_open=False,symbol='NQ-PERP',observed_at=self.now)
        request=dict(account_id=a['id'],session_id='live-browser',identity=a['identity'],mode_epoch=1,
            phase='live',tab_url='https://next.vestmarkets.com/trade/NQ-PERP',snapshot=snap)
        job=self.e.bridge_poll({**request,'ready':True})['job']
        self.assertEqual((job['size_percent'],job['side'],job['take_profit'],job['configured_stop_loss']),(75,'sell',100,44))
        sized=self.e.bridge_size({**request,'job_id':job['id'],'size_percent':75,'quantity':2,'stop_loss':None,'drawdown':metric})
        self.assertIsNone(sized['stop_loss']);self.assertEqual(sized['remaining_drawdown_at_entry'],40)
