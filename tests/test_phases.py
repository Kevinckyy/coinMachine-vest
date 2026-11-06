import copy
import tempfile
import unittest
from pathlib import Path
from server.engine import Engine, ValidationError


class PhaseTests(unittest.TestCase):
    def setUp(self):
        self.now=1793808000.0
        self.e=Engine(':memory:',seed=False,clock=lambda:self.now)
        self.sid='phase-browser-session'

    def tearDown(self):
        if self.e:
            self.e.close()

    def snap(self, phase='1/2', **extra):
        return dict(balance=1000,equity=1000,unrealized_pnl=0,price=100,
            position_open=False,position_id='phase-position',symbol='NQ-PERP',side='buy',
            quantity=1,entry_price=100,observed_at=self.now,phase=phase,**extra)

    def session(self, phase='1/2', *, proof=False, metrics=True, **extra):
        row=dict(name='Phase account',category='funded' if phase=='live' else 'eval',
            balance=1000,equity=1000,unrealized_pnl=0,current=True,observed_at=self.now,
            phase=phase,phase_observed_at=self.now if phase else None)
        if metrics:
            row['capital_metrics']={key:dict(text=f'{value} / {limit}',current=value,limit=limit,
                percent=None,observed_at=self.now,category=row['category'],phase=phase)
                for key,value,limit in [('goal',10,500),('daily_loss',20,130),('drawdown',30,300)]}
        if proof:
            row['phase_snapshot']=self.snap(phase)
        row.update(extra)
        return self.e.bridge_session(dict(session_id=self.sid,mode='execute',mode_epoch=1,
            authorize=True,tab_url='https://next.vestmarkets.com/trade/NQ-PERP',accounts=[row]))['accounts'][0]

    def poll(self,a,**extra):
        return self.e.bridge_poll(dict(account_id=a['id'],session_id=a['session_id'],identity=a['identity'],
            mode_epoch=1,phase=a.get('phase'),tab_url='https://next.vestmarkets.com/trade/NQ-PERP',
            ready=True,snapshot={**self.snap(a.get('phase')),**extra}))

    def order(self,a,key='first'):
        self.e.settings({'mode':'browser'})
        return self.e.place_orders(dict(account_ids=[a['id']],side='buy',symbol='NQ-PERP',quantity=1,
            stop_loss=130,idempotency_key="phase-test-"+key))

    def opening(self, phase='1/2', confirmed=True):
        a=self.session(phase)
        self.order(a)
        job=self.poll(a)['job']
        if confirmed:
            self.e.bridge_result(dict(job_id=job['id'],session_id=self.sid,success=True,confirmed=True,
                snapshot={**self.snap(phase), 'position_open':True}))
        return a,job

    def complete(self,phase='1/2',pnl=100):
        a,j=self.opening(phase)
        self.poll(a,balance=1000+pnl,equity=1000+pnl)
        return self.e.state()['accounts'][0]

    def test_same_day_each_phase_has_own_pnl_risk_metrics_baseline_and_daily_entry(self):
        a=self.complete()
        self.assertTrue(a['daily_entry_used'])
        self.assertTrue(a['strategy_locked'])
        self.assertEqual(a['phase_realized_pnl'],100)
        self.now+=1
        b=self.session('2/2',metrics=False,balance=5000,equity=5000)
        self.assertEqual(b['id'],a['id'])
        self.assertFalse(b['daily_entry_used'])
        self.assertFalse(b['strategy_locked'])
        self.assertEqual(b['initial_balance'],5000)
        self.assertEqual(b['realized_pnl'],0)
        self.assertEqual(b['phase_realized_pnl'],0)
        self.assertEqual(b['daily_realized_pnl'],0)
        self.assertTrue(all(v is None for v in b['capital_metrics'].values()))
        self.assertEqual(b['phase_books']['1/2']['capital_metrics']['goal']['limit'],500)
        self.assertEqual(b['phase_books']['1/2']['history_realized_pnl'],100)
        self.assertTrue(b['phase_books']['1/2']['daily_entry_used'])
        self.order(b,'second-phase')
        self.poll(b,balance=5000,equity=5000)
        self.assertTrue(self.e._daily_entry_used(b['id']))
        self.now+=1
        c=self.session('live',proof=True)
        self.assertFalse(c['daily_entry_used'])
        self.assertEqual(c['phase_realized_pnl'],0)
        self.assertEqual(set(c['phase_books']) & {'1/2','2/2','live'}, {'1/2','2/2','live'})

    def test_repeated_same_phase_and_manual_unlock_never_grant_second_entry(self):
        a=self.complete()
        self.e.strategy_lock_account(a['id'],False)
        self.now+=1
        a=self.session()
        with self.assertRaisesRegex(ValidationError,'每日只允许一单'):
            self.order(a,'duplicate')
        self.assertEqual(a['phase_realized_pnl'],100)

    def test_old_phase_and_unknown_observation_never_regress_or_overwrite_money(self):
        self.session()
        self.now+=1
        a=self.session('2/2',balance=2000,equity=2000)
        self.now+=1
        stale=self.session('1/2',balance=9999,equity=9999)
        self.assertEqual(stale['phase'],'2/2')
        self.assertEqual(stale['balance'],2000)
        self.assertFalse(stale['executable'])
        unknown=self.session(None,balance=9999,equity=9999,metrics=False)
        self.assertEqual(unknown['balance'],2000)
        self.assertFalse(unknown['executable'])
        fresh=self.session('2/2')
        self.assertTrue(fresh['executable'])

    def test_existing_unphased_daily_quota_migrates_conservatively_without_tagging_history(self):
        a=self.complete(None)
        self.now+=1
        a=self.session('1/2')
        self.assertTrue(a['daily_entry_used'])
        self.assertEqual(a['phase_realized_pnl'],0)
        self.assertIsNone(self.e.data['history'][0].get('phase'))
        self.assertEqual(a['legacy_phase_scope'],'1/2')
        self.now+=1
        self.assertFalse(self.session('2/2')['daily_entry_used'])

    def test_phase_transition_waits_for_fresh_explicit_new_phase_flat_proof(self):
        a,j=self.opening()
        self.now+=1
        b=self.session('2/2',metrics=False)
        self.assertEqual(b['phase'],'1/2')
        self.assertEqual(b['phase_pending']['phase'],'2/2')
        self.assertTrue(self.e.data['positions'])
        self.assertIsNone(self.poll(a)['job'])
        with self.assertRaises(ValidationError):
            self.order(b,'blocked')
        self.now+=1
        b=self.session('2/2',proof=True,metrics=False)
        self.assertEqual(b['phase'],'2/2')
        self.assertFalse(self.e.data['positions'])
        self.assertFalse(self.e.data['history'])
        self.assertFalse(b['daily_entry_used'])
        self.assertTrue(b['phase_books']['1/2']['settlement_pending'])
        audit=self.e.data['audit'][-1]
        self.assertEqual(len(audit['positions_archived_without_execution']),1)

    def test_readonly_poll_settles_native_first_phase_close_before_same_name_successor(self):
        a,j=self.opening()
        self.now+=1
        result=self.e.bridge_poll(dict(account_id=a['id'],session_id=self.sid,identity=a['identity'],
            mode_epoch=1,phase='1/2',tab_url='https://next.vestmarkets.com/trade/NQ-PERP',ready=False,
            snapshot={**self.snap('1/2'),'balance':1100,'equity':1100}))
        self.assertIsNone(result['job'])
        self.assertFalse(self.e.data['positions'])
        self.assertEqual(self.e.data['history'][-1]['phase'],'1/2')
        self.assertEqual(self.e.data['history'][-1]['pnl'],100)
        self.now+=1
        b=self.session('2/2',proof=True)
        self.assertEqual(b['id'],a['id'])
        self.assertEqual(b['phase'],'2/2')
        self.assertEqual(b['phase_books']['1/2']['history_realized_pnl'],100)
        self.assertEqual(b['phase_realized_pnl'],0)
        self.assertFalse(b['daily_entry_used'])

    def test_unknown_delivered_job_is_archived_on_verified_phase_change_and_late_receipt_cannot_reopen(self):
        a,j=self.opening(confirmed=False)
        self.now+=1
        b=self.session('2/2',proof=True)
        self.assertEqual(self.e.data['jobs'][j['id']]['status'],'archived_phase')
        self.assertFalse(self.e._pending(a['id']))
        result=self.e.bridge_result(dict(job_id=j['id'],session_id=self.sid,success=True,confirmed=True,
            snapshot={**self.snap('1/2'),'position_open':True}))
        self.assertTrue(result['duplicate'])
        self.assertFalse(self.e.data['positions'])
        self.assertFalse(self.e.data['history'])
        self.assertFalse(b['daily_entry_used'])

    def test_phase_snapshot_must_really_be_flat_fresh_and_of_new_phase(self):
        a,j=self.opening()
        for patch in [dict(position_open=None),dict(position_open=True),dict(unrealized_pnl=1),
                      dict(phase='1/2'),dict(observed_at=self.now-10),dict(price=None)]:
            self.now+=.1
            row=self.session('2/2',phase_snapshot={**self.snap('2/2'),**patch})
            self.assertEqual(row['phase'],'1/2',str(patch))
            self.assertTrue(self.e.data['positions'])

    def test_phase_guards_reject_mismatched_poll_and_result_without_changing_money_or_position(self):
        a,j=self.opening(confirmed=False)
        with self.assertRaisesRegex(ValidationError,'阶段'):
            self.poll(a,phase='2/2',balance=9999,equity=9999)
        self.assertEqual(self.e.data['accounts'][a['id']]['balance'],1000)
        with self.assertRaisesRegex(ValidationError,'阶段'):
            self.e.bridge_result(dict(job_id=j['id'],session_id=self.sid,success=True,confirmed=True,
                snapshot={**self.snap('2/2'),'position_open':True}))
        self.assertEqual(self.e.data['jobs'][j['id']]['status'],'delivered')
        self.assertFalse(self.e.data['positions'])

    def test_manual_lock_archive_and_primary_exclusion_survive_phase_changes(self):
        a=self.session()
        self.e.strategy_lock_account(a['id'],True)
        self.now+=1
        self.assertTrue(self.session('2/2')['strategy_locked'])
        self.e.archive_account(a['id'],{'acknowledge':True})
        self.now+=1
        b=self.session('live')
        self.assertTrue(b['archived'])
        self.assertFalse(b['enabled'])
        primary=self.session('1/2',name='Main Account',category='default')
        self.now+=1
        primary=self.session('2/2',name='Main Account',category='default')
        self.assertTrue(primary['execution_excluded'])
        self.assertFalse(primary['executable'])

    def test_next_day_resets_current_phase_quota_but_keeps_earlier_phase_finances(self):
        a=self.complete()
        self.now+=86400
        a=self.session()
        self.assertFalse(a['daily_entry_used'])
        self.assertEqual(a['phase_realized_pnl'],100)
        self.assertEqual(a['phase_books']['1/2']['daily_realized_pnl'],0)

    def test_fresh_explicit_phase_and_valid_timestamp_required(self):
        a=self.session()
        self.now+=1
        for changes in [dict(phase='3/3'),dict(phase='2/2',phase_observed_at=self.now+10)]:
            with self.assertRaises(ValidationError):
                self.session(**changes)
        self.assertEqual(self.session('2/2',phase_observed_at=self.now-20)['phase'],'1/2')

    def test_phase_books_and_scoped_quota_survive_restart(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]/'runtime') as folder:
            db=str(Path(folder)/'phase.sqlite')
            self.e.close(); self.e=Engine(db,seed=False,clock=lambda:self.now)
            a=self.complete()
            self.now+=1
            self.session('2/2')
            self.e.close(); self.e=Engine(db,seed=False,clock=lambda:self.now)
            a=self.e.state()['accounts'][0]
            self.assertEqual(a['phase'],'2/2')
            self.assertFalse(a['daily_entry_used'])
            self.assertEqual(a['phase_books']['1/2']['history_realized_pnl'],100)
            self.e.close()
            self.e=None

    def test_strategy_continues_existing_run_on_new_phase_without_automatic_resume(self):
        a,j=self.opening()
        config=dict(account_ids=[a['id']],batch_size=1,side='buy',symbol='NQ-PERP',quantity=1,
            order_delay_min=0,order_delay_max=0,batch_delay_min=2,batch_delay_max=2)
        strategy=self.e.data['strategy']
        strategy.update(status='running',phase='monitoring',config=config,
            batch_account_ids=[a['id']],remaining_accounts=[],completed_accounts=[])
        self.now+=1
        self.session('2/2',proof=True)
        self.assertIn(a['id'],strategy['remaining_accounts'])
        self.e.tick(price=100)
        self.assertEqual(strategy['status'],'waiting')
        self.assertFalse(self.e._pending(a['id']))
        self.now+=2
        self.session('2/2')
        self.e.tick(price=100)
        self.e.tick(price=100)
        self.assertTrue(self.e._pending(a['id']))
        self.e.stop_strategy()
        self.now+=1
        self.session('live',proof=True)
        self.assertEqual(strategy['status'],'stopped')

    def test_merging_different_phase_caches_keeps_ledgers_and_does_not_inherit_old_automatic_lock(self):
        a=self.complete()
        original=self.e.data['accounts'][a['id']]
        duplicate=copy.deepcopy(original)
        duplicate.update(id='phase-duplicate',phase='2/2',phase_observed_at=self.now+1,
            initial_balance=5000,balance=5000,equity=5000,realized_pnl=0,status='active',lock_reason=None,
            strategy_locked=False,strategy_lock_source=None,strategy_lock_reason=None,strategy_locked_at=None,
            capital_metrics={},phase_books={'2/2':{'phase':'2/2','balance':5000}},created_at=self.now+1)
        self.e.data['accounts'][duplicate['id']]=duplicate
        self.e._deduplicate_browser_accounts()
        account=self.e.state()['accounts'][0]
        self.assertEqual(account['phase'],'2/2')
        self.assertEqual(account['initial_balance'],5000)
        self.assertFalse(account['strategy_locked'])
        self.assertFalse(account['daily_entry_used'])
        self.assertEqual(account['phase_books']['1/2']['history_realized_pnl'],100)
        self.assertTrue(all(v is None for v in account['capital_metrics'].values()) or not account['capital_metrics'])

    def test_merge_cannot_settle_an_old_phase_position_from_a_new_phase_cached_record(self):
        a,j=self.opening()
        duplicate=copy.deepcopy(self.e.data['accounts'][a['id']])
        duplicate.update(id='phase-duplicate',phase='2/2',phase_observed_at=self.now+1,created_at=self.now+1)
        self.e.data['accounts'][duplicate['id']]=duplicate
        self.e._deduplicate_browser_accounts()
        account=self.e.state()['accounts'][0]
        self.assertEqual(account['phase'],'1/2')
        self.assertEqual(account['phase_pending']['phase'],'2/2')
        self.assertTrue(self.e.data['positions'])
        self.now+=1
        self.assertEqual(self.session('2/2',proof=True)['phase'],'2/2')
        self.assertFalse(self.e.data['positions'])
        self.assertFalse(self.e.data['history'])
