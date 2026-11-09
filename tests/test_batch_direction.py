import copy
import random
import tempfile
import unittest
from pathlib import Path
from server.engine import Engine, ValidationError
from server.strategies import choose_batch_side


class AlternatingSides(random.Random):
    def __init__(self):super().__init__(17);self.side_calls=0
    def choice(self, values):
        if tuple(values)==('buy','sell'):
            self.side_calls+=1
            return 'buy' if self.side_calls%2 else 'sell'
        return super().choice(values)


class BatchDirectionTests(unittest.TestCase):
    def setUp(self):
        self.now=1793955000.;self.rng=AlternatingSides()
        self.e=Engine(':memory:',seed=False,clock=lambda:self.now,rng=self.rng)

    def tearDown(self):self.e.close()

    def accounts(self, count, live=0):
        ids=[self.e.add_account(dict(name=f'Batch {i}',initial_balance=1000,profit_target=10000,max_loss=900))['id'] for i in range(count)]
        for aid in ids[:live]:self.e.data['accounts'][aid].update(phase='live',category='funded')
        return ids

    def config(self, ids, **extra):
        return dict(account_ids=ids,batch_size=3,side='random',symbol='NQ-PERP',quantity=1,stop_loss=500,
            order_delay_min=1,order_delay_max=1,batch_delay_min=0,batch_delay_max=0,**extra)

    def tick(self):self.now+=1;self.e.tick(price=100)

    def test_random_direction_is_drawn_once_for_entire_batch(self):
        ids=self.accounts(3);self.e.start_strategy(self.config(ids))
        for _ in ids:self.tick()
        self.assertEqual({p['side'] for p in self.e.data['positions'].values()},{'buy'})
        self.assertEqual(self.rng.side_calls,1)
        self.assertEqual(self.e.data['strategy']['batch_directions'],{'NQ-PERP':'buy'})

    def test_closed_first_trade_does_not_reroll_remaining_entries(self):
        self.e.start_strategy(self.config(self.accounts(3)));self.tick()
        first=next(iter(self.e.data['positions'].values()));self.e.close_position(first['id'])
        for _ in range(2):self.tick()
        self.assertEqual({p['side'] for p in self.e.data['positions'].values()},{'buy'})
        self.assertEqual(self.rng.side_calls,1)

    def test_next_batch_gets_its_own_single_draw(self):
        self.e.start_strategy(self.config(self.accounts(6)))
        for _ in range(3):self.tick()
        for p in list(self.e.data['positions'].values()):self.e.close_position(p['id'])
        for _ in range(4):self.tick()
        self.assertEqual({p['side'] for p in self.e.data['positions'].values()},{'sell'})
        self.assertEqual(self.rng.side_calls,2)

    def test_fixed_member_controls_random_members_of_mixed_live_batch(self):
        ids=self.accounts(3,live=1)
        self.e.start_strategy(self.config(ids,live_default_locked=False,live_side='sell'))
        for _ in ids:self.tick()
        self.assertEqual({p['side'] for p in self.e.data['positions'].values()},{'sell'})
        self.assertEqual(self.rng.side_calls,0)

    def test_conflicting_fixed_preferences_reject_whole_batch_before_any_open(self):
        ids=self.accounts(3,live=1);cfg=self.config(ids,live_default_locked=False,live_side='sell');cfg['side']='buy'
        self.e.start_strategy(cfg);self.tick()
        self.assertFalse(self.e.data['positions']);self.assertFalse(self.e.data['jobs'])
        self.assertEqual(self.e.data['strategy']['status'],'paused')
        self.assertEqual(set(self.e.data['strategy']['remaining_accounts']),set(ids))
        self.assertIn('整批未开仓',self.e.data['logs'][-1]['message'])

    def test_pure_batches_may_use_different_stage_directions(self):
        ids=self.accounts(4,live=2);cfg=self.config(ids,live_default_locked=False,live_side='sell');cfg.update(batch_size=2,side='buy')
        self.e.start_strategy(cfg);self.tick();self.tick()
        self.assertEqual({p['side'] for p in self.e.data['positions'].values()},{'sell'})
        for p in list(self.e.data['positions'].values()):self.e.close_position(p['id'])
        for _ in range(3):self.tick()
        self.assertEqual({p['side'] for p in self.e.data['positions'].values()},{'buy'})

    def test_legacy_batch_recovers_direction_from_position_or_closed_trade(self):
        for closed in (False,True):
            with self.subTest(closed=closed):
                self.e.close();self.rng=AlternatingSides();self.e=Engine(':memory:',seed=False,clock=lambda:self.now,rng=self.rng)
                self.e.start_strategy(self.config(self.accounts(3)));self.tick()
                if closed:self.e.close_position(next(iter(self.e.data['positions'])))
                del self.e.data['strategy']['batch_directions']
                for _ in range(2):self.tick()
                self.assertEqual({p['side'] for p in self.e.data['positions'].values()},{'buy'})
                self.assertEqual(self.rng.side_calls,1)

    def test_batch_direction_survives_restart_and_original_run_is_retained(self):
        with tempfile.TemporaryDirectory() as folder:
            self.e.close();self.e=Engine(Path(folder)/'batch.sqlite',seed=False,clock=lambda:self.now,rng=self.rng)
            self.e.start_strategy(self.config(self.accounts(3)));self.tick();run=self.e.data['strategy']['run_id'];self.e.close()
            self.e=Engine(Path(folder)/'batch.sqlite',seed=False,clock=lambda:self.now,rng=self.rng)
            self.assertEqual(self.e.data['strategy']['status'],'paused')
            self.assertEqual(self.e.data['strategy']['batch_directions'],{'NQ-PERP':'buy'})
            self.e.data['strategy']['status']='running'  # isolated verification of the persisted submission queue
            self.tick();self.tick()
            self.assertEqual({p['side'] for p in self.e.data['positions'].values()},{'buy'})
            self.assertEqual(self.e.data['strategy']['run_id'],run)
            self.e.close();self.e=Engine(':memory:',seed=False,clock=lambda:self.now,rng=self.rng)

    def browser_batch(self):
        self.e.settings({'mode':'browser'})
        rows=[dict(name=f'Browser {i}',category='eval',phase='1/2',phase_observed_at=self.now,
            balance=1000,equity=1000,unrealized_pnl=0,observed_at=self.now,current=i==0) for i in range(2)]
        accounts=self.e.bridge_session(dict(session_id='batch-browser',mode='execute',mode_epoch=1,authorize=True,
            tab_url='https://next.vestmarkets.com/trade/NQ-PERP',accounts=rows))['accounts']
        cfg=self.config([a['id'] for a in accounts]);cfg['batch_size']=2;self.e.start_strategy(cfg);self.tick()
        return {a['id']:a for a in accounts}

    def poll(self,a,**extra):
        return self.e.bridge_poll(dict(account_id=a['id'],session_id='batch-browser',mode_epoch=1,identity=a['identity'],
            phase='1/2',tab_url='https://next.vestmarkets.com/trade/NQ-PERP',ready=True,
            snapshot=dict(phase='1/2',balance=1000,equity=1000,unrealized_pnl=0,price=100,
                position_open=False,symbol='NQ-PERP',observed_at=self.now,**extra)))

    def test_queued_opposite_job_is_cancelled_before_browser_delivery(self):
        accounts=self.browser_batch();job=next(iter(self.e.data['jobs'].values()));job['side']='sell'
        self.assertIsNone(self.poll(accounts[job['account_id']])['job'])
        self.assertEqual(job['status'],'cancelled');self.assertIsNone(job['delivered_at'])
        self.assertEqual(self.e.data['strategy']['status'],'paused')
        self.assertIn('同向',job['cancel_reason'])

    def test_stale_batch_job_cannot_be_delivered_even_if_direction_matches(self):
        accounts=self.browser_batch();job=next(iter(self.e.data['jobs'].values()));job['strategy_run_id']='another-run'
        self.assertIsNone(self.poll(accounts[job['account_id']])['job'])
        self.assertEqual(job['status'],'cancelled');self.assertIsNone(job['delivered_at'])

    def test_corrupt_batch_map_cannot_override_an_already_delivered_side(self):
        accounts=self.browser_batch();first=next(iter(self.e.data['jobs'].values()));a=accounts[first['account_id']]
        delivered=self.poll(a)['job'];self.assertEqual(delivered['side'],'buy')
        snap=dict(phase='1/2',balance=1000,equity=1000,unrealized_pnl=0,price=100,
            position_open=True,position_id='native-first',symbol='NQ-PERP',side='buy',quantity=1,entry_price=100,observed_at=self.now)
        self.e.bridge_result(dict(job_id=delivered['id'],session_id='batch-browser',success=True,confirmed=True,snapshot=snap))
        self.tick();second=next(j for j in self.e.data['jobs'].values() if j['status']=='queued')
        self.e.data['strategy']['batch_directions']['NQ-PERP']='sell';second['side']='sell'
        self.assertIsNone(self.poll(accounts[second['account_id']])['job'])
        self.assertEqual(second['status'],'cancelled')
        self.assertEqual(self.e._position(a['id'])['side'],'buy')

    def test_batch_resolver_rejects_bad_preferences_without_random_draw(self):
        for prefs in ([],['buy','sell'],['invalid']):
            with self.subTest(prefs=prefs),self.assertRaises(ValueError):choose_batch_side('random_entry',self.rng,prefs)
        self.assertEqual(self.rng.side_calls,0)
