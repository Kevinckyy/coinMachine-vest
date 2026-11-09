import copy
import random
import unittest

from server.engine import Engine, ValidationError


class ObservedPositionAdoptionTests(unittest.TestCase):
    def setUp(self):
        self.now=1794230000.0
        self.e=Engine(':memory:',seed=False,clock=lambda:self.now,rng=random.Random(4))
        self.sid='observed-position-session'
        self.rows=[dict(name=name,category='eval',phase='1/2',phase_observed_at=self.now,
                       current=i==0,balance=5000,equity=5000,unrealized_pnl=0,observed_at=self.now)
                   for i,name in enumerate(('A','B'))]
        self.grant(1)
        self.e.settings({'mode':'browser'})
        ids=list(self.e.data['accounts'])
        self.e.start_strategy(dict(account_ids=ids,batch_size=1,quantity=4,side='sell',stop_loss=130,
            order_delay_min=0,order_delay_max=0,batch_delay_min=5,batch_delay_max=5))
        self.e.tick(100)
        self.job=next(j for j in self.e.data['jobs'].values() if j['status']=='queued')
        self.a=self.e._account(self.job['account_id'])
        self.poll(1,self.snap(False))
        self.e.bridge_result(dict(job_id=self.job['id'],session_id=self.sid,mode_epoch=1,
            success=False,confirmed=False,error='virtual position rows not rendered',snapshot={'observed_at':self.now}))
        self.e.bridge_session(dict(session_id=self.sid,mode='readonly',mode_epoch=2,
            tab_url='https://next.vestmarkets.com/trade/NQ-PERP',accounts=copy.deepcopy(self.rows)))
        self.e.bridge_inspect(dict(account_id=self.a['id'],session_id=self.sid,name=self.a['name'],
            tab_url='https://next.vestmarkets.com/trade/NQ-PERP',snapshot=self.snap(True)))
        self.original_run=copy.deepcopy(self.e.data['strategy'])

    def tearDown(self):
        self.e.close()

    def grant(self,epoch):
        return self.e.bridge_session(dict(session_id=self.sid,mode='execute',mode_epoch=epoch,authorize=True,
            tab_url='https://next.vestmarkets.com/trade/NQ-PERP',accounts=copy.deepcopy(self.rows)))

    def snap(self,opened):
        return dict(phase='1/2',balance=5000,equity=5012 if opened else 5000,unrealized_pnl=12 if opened else 0,
            price=100,position_open=opened,position_id='verified-native-position' if opened else None,
            symbol='NQ-PERP',side='sell' if opened else None,quantity=4 if opened else None,
            entry_price=103 if opened else None,observed_at=self.now,native_sl=True,native_sl_price=135.5,
            native_sl_absent=False,native_tp=False,native_tp_price=None)

    def poll(self,epoch,snapshot):
        return self.e.bridge_poll(dict(account_id=self.a['id'],session_id=self.sid,mode_epoch=epoch,
            identity=self.a['identity'],phase='1/2',ready=True,tab_url='https://next.vestmarkets.com/trade/NQ-PERP',snapshot=snapshot))

    def adopt(self,**changes):
        return self.e.reconcile(dict(account_id=self.a['id'],acknowledge=True,adopt_position=True,
            job_id=self.job['id'],position_id='verified-native-position',**changes))

    def test_adopts_exact_observed_exposure_once_without_fill_or_history_and_preserves_daily_use(self):
        result=self.adopt()
        position=self.e._position(self.a['id'])
        self.assertTrue(result['adopted'])
        self.assertEqual(position['browser_position_id'],'verified-native-position')
        self.assertEqual((position['side'],position['quantity'],position['entry_price']),('sell',4,103))
        self.assertEqual(position['opened_at_source'],'verified_existing_position_observation')
        self.assertEqual(self.job['status'],'reconciled')
        self.assertNotIn('confirmed_at',self.job)
        self.assertFalse(self.e.data['history'])
        self.assertTrue(self.e._daily_entry_used(self.a['id']))
        self.assertEqual(self.e.data['strategy']['status'],'paused')
        self.assertTrue(self.adopt()['duplicate'])
        self.assertEqual(len(self.e.data['positions']),1)
        self.assertEqual(sum(a.get('event')=='manual_position_adoption' for a in self.e.data['audit']),1)

    def test_mismatched_incomplete_or_stale_observation_cannot_be_adopted(self):
        snapshot=self.e.data['bridge_sessions'][self.a['id']]['snapshot']
        for changes in [dict(quantity=2),dict(side='buy'),dict(symbol='ES-PERP'),dict(position_id='other'),
                        dict(position_open=False),dict(entry_price=None),dict(observed_at=self.now-6),
                        dict(phase='2/2'),dict(native_sl=False),dict(equity=5100)]:
            with self.subTest(changes=changes):
                old=copy.deepcopy(snapshot);snapshot.update(changes)
                with self.assertRaises(ValidationError):self.adopt()
                snapshot.clear();snapshot.update(old)
                self.assertFalse(self.e.data['positions'])
                self.assertEqual(self.job['status'],'unknown')

    def test_requires_explicit_ack_unknown_delivered_open_and_same_readonly_owner(self):
        with self.assertRaises(ValidationError):
            self.e.reconcile(dict(account_id=self.a['id'],adopt_position=True,job_id=self.job['id'],position_id='verified-native-position'))
        session=self.e.data['bridge_sessions'][self.a['id']]
        for changes in [dict(source='poll'),dict(session_id='another-session')]:
            original=copy.deepcopy(session);session.update(changes)
            with self.assertRaises(ValidationError):self.adopt()
            session.clear();session.update(original)
        self.job['type']='close'
        with self.assertRaises(ValidationError):self.adopt()
        self.job['type']='open';self.job['delivered_at']=None
        with self.assertRaises(ValidationError):self.adopt()

    def test_no_stop_loss_adoption_requires_explicit_native_absence(self):
        self.job['stop_loss']=None
        snapshot=self.e.data['bridge_sessions'][self.a['id']]['snapshot']
        with self.assertRaises(ValidationError):self.adopt()
        snapshot.update(native_sl_absent=True,native_sl=False,native_sl_price=None)
        self.assertTrue(self.adopt()['adopted'])

    def test_resume_preserves_original_batch_and_parameters_and_waits_for_held_position(self):
        body=dict(acknowledge=True,run_id=self.original_run['run_id'])
        with self.assertRaises(ValidationError):self.e.resume_strategy(body)
        self.adopt()
        with self.assertRaises(ValidationError):self.e.resume_strategy(body)
        self.grant(3);self.poll(3,self.snap(True))
        result=self.e.resume_strategy(body)
        self.assertEqual(result['status'],'running')
        for key in ('run_id','config','batch_account_ids','remaining_accounts','batch_number'):
            self.assertEqual(result[key],self.original_run[key])
        self.e.tick(100)
        self.assertEqual(self.e.data['strategy']['phase'],'monitoring')
        self.assertEqual(len(self.e.data['jobs']),1,'resuming must not replay the old open or start another batch')

    def test_resume_rejects_stale_run_or_unverified_lease(self):
        self.adopt()
        for body in [dict(acknowledge=True,run_id='other-run'),dict(run_id=self.original_run['run_id'])]:
            with self.assertRaises(ValidationError):self.e.resume_strategy(body)
        self.grant(3);self.now+=6
        with self.assertRaises(ValidationError):
            self.e.resume_strategy(dict(acknowledge=True,run_id=self.original_run['run_id']))

    def test_resume_cooldown_waits_original_delay_and_never_reopens_finished_account(self):
        self.adopt();self.grant(3);self.poll(3,self.snap(True))
        self.poll(3,{**self.snap(False),'balance':5012,'equity':5012})
        s=self.e.data['strategy'];s.update(phase='cooldown',next_action_at=None)
        result=self.e.resume_strategy(dict(acknowledge=True,run_id=s['run_id']))
        self.assertEqual(result['status'],'waiting')
        self.assertEqual(result['next_action_at'],self.now+5)
        self.e.tick(100);self.assertEqual(s['batch_number'],1)
        self.now+=5;self.grant(3);self.e.tick(100)
        self.assertEqual(s['batch_number'],2)
        self.assertNotIn(self.a['id'],s['batch_account_ids'])
        self.assertEqual(self.job['status'],'reconciled')
