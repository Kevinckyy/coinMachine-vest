import copy
import tempfile
import unittest
from pathlib import Path
from server.engine import Engine, ValidationError

TRADE='https://next.vestmarkets.com/trade/NQ-PERP'
PORTFOLIO='https://next.vestmarkets.com/portfolio'


class ProfitClaimTests(unittest.TestCase):
    def setUp(self):
        self.now=1794300000.
        self.e=Engine(':memory:',seed=False,clock=lambda:self.now)
        self.prepare()

    def tearDown(self):self.e.close()

    def prepare(self):
        self.e.settings({'mode':'browser'})
        self.e.save_strategy_config({'live_default_locked':False,'live_side':'buy'})
        self.rows=[dict(name='Payout Live',category='funded',phase='live',current=True,
            balance=5000,equity=5000,unrealized_pnl=0,observed_at=self.now)]
        r=self.session(authorize=True)
        self.a=self.e.data['accounts'][r['accounts'][0]['id']]
        self.e.start_strategy(dict(account_ids=[self.a['id']],batch_size=1,quantity=1,side='buy',stop_loss=130,
            order_delay_min=2,order_delay_max=2,batch_delay_min=0,batch_delay_max=0))
        self.e.tick(price=100)
        job=self.e.bridge_poll({**self.body(), 'snapshot':self.snapshot(5000),'ready':True})['job']
        self.assertIsNotNone(job)
        self.open_job=job
        self.e.bridge_result({**self.body(),'job_id':job['id'],'success':True,'confirmed':True,
            'snapshot':self.snapshot(5000,position_open=True,position_id='native-payout',quantity=1,entry_price=100,side=job['side'])})
        self.now+=1
        self.position=copy.deepcopy(self.e._position(self.a['id']))

    def session(self,**extra):
        return self.e.bridge_session(dict(session_id='profit-claim-session',mode='execute',mode_epoch=1,
            tab_url=TRADE,accounts=self.rows,heartbeat_only=True,**extra))

    def body(self,**extra):
        return dict(account_id=self.a['id'],name=self.a['name'],session_id='profit-claim-session',
            mode_epoch=1,identity=self.a['identity'],phase='live',tab_url=TRADE,**extra)

    def snapshot(self,balance,**extra):
        return {**dict(balance=balance,equity=balance,unrealized_pnl=0,price=100,phase='live',symbol='NQ-PERP',
            position_open=False,observed_at=self.now),**extra}

    def close(self,balance=5040):
        self.e.bridge_poll({**self.body(),'snapshot':self.snapshot(balance),'ready':True})
        return next(iter(self.e.data['profit_claims'].values()),None)

    def reserve(self,claim):
        return self.e.bridge_profit_claim({**self.body(),'claim_id':claim['id'],'action':'reserve','snapshot':self.snapshot(5040)})

    def intent(self,claim,**preview):
        return self.e.bridge_profit_claim({**self.body(),'tab_url':PORTFOLIO,'claim_id':claim['id'],'action':'intent',
            'preview':{**dict(available=40,amount=40,received=32,recipient='main',
                flat=dict(leverage=0,position_margin=0,order_margin=0,observed_at=self.now)),**preview}})

    def verify(self,claim,balance=5000):
        self.now+=1
        return self.e.bridge_profit_claim({**self.body(),'claim_id':claim['id'],'action':'verify','snapshot':self.snapshot(balance)})

    def test_claim_then_same_live_reenters_same_batch_direction_after_jitter(self):
        c=self.close();self.assertEqual(c['target_balance'],5000)
        self.assertTrue(self.e._daily_entry_used(self.a['id']))
        self.assertEqual(len(self.e.data['history']),1)
        self.assertEqual(self.e.data['history'][0]['pnl'],40,'claim deduction must never be booked as a losing trade')
        self.e.tick(price=100)
        self.assertEqual(self.e.data['strategy']['phase'],'monitoring')
        self.reserve(c);self.assertTrue(self.intent(c)['authorize_click'])
        self.assertTrue(self.verify(c)['verified'])
        self.assertEqual(len(self.e.data['history']),1)
        self.assertEqual(self.e.data['history'][0]['pnl'],40,'withdrawing profit does not add a losing trade')
        self.assertFalse(self.e._daily_entry_used(self.a['id']))
        self.assertIsNone(c['credited_at'],'balance reset does not prove funds have been credited')
        self.e.tick(price=100)
        self.assertFalse(any(j['status']=='queued' for j in self.e.data['jobs'].values()),'original order jitter remains in force')
        self.now+=2;self.e.tick(price=100)
        job=self.e.bridge_poll({**self.body(),'snapshot':self.snapshot(5000),'ready':True})['job']
        self.assertIsNotNone(job);self.assertEqual(job['side'],self.open_job['side'])
        self.assertNotEqual(job['id'],self.open_job['id']);self.assertEqual(self.e.data['strategy']['batch_number'],1)
        self.assertTrue(self.e._daily_entry_used(self.a['id']))

    def test_duplicate_intent_and_verification_never_create_another_click_or_reentry(self):
        c=self.close();self.reserve(c)
        self.assertTrue(self.intent(c)['authorize_click']);self.assertFalse(self.intent(c)['authorize_click'])
        self.verify(c);self.assertTrue(self.verify(c)['duplicate'])
        self.assertEqual(self.e.data['strategy']['order_queue'],[self.a['id']])
        self.assertEqual(len(self.e.data['profit_claims']),1)

    def test_partial_claim_does_not_allow_continuation_without_reset(self):
        c=self.close();c['policy'].update(live_payout_mode='amount',live_payout_amount=10)
        self.reserve(c);self.intent(c,amount=10,received=8)
        with self.assertRaisesRegex(ValidationError,'初始规模'):self.verify(c,5030)
        self.assertEqual(c['status'],'balance_mismatch');self.assertEqual(self.e.data['strategy']['status'],'paused')
        self.assertTrue(self.e._daily_entry_used(self.a['id']))
        with self.assertRaises(ValidationError):self.e._open_one(self.a['id'],{'side':'buy'},'strategy')

    def test_zero_and_negative_net_profit_do_not_claim_or_reset_quota(self):
        self.assertIsNone(self.close(4990));self.assertTrue(self.e._daily_entry_used(self.a['id']))
        self.assertTrue(self.a['strategy_locked'])

    def test_readings_from_before_submission_cannot_verify_a_claim(self):
        c=self.close();self.reserve(c);self.intent(c);self.now+=1
        old=self.snapshot(5000);old['observed_at']=c['submitted_at']-1
        with self.assertRaisesRegex(ValidationError,'早于提交'):
            self.e.bridge_profit_claim({**self.body(),'claim_id':c['id'],'action':'verify','snapshot':old})
        self.assertEqual(c['status'],'submitted')

    def test_unknown_submission_blocks_a_new_strategy(self):
        c=self.close();self.reserve(c);self.intent(c)
        self.e.bridge_profit_claim({**self.body(),'claim_id':c['id'],'action':'fail','error':'response lost'})
        self.assertEqual(c['status'],'unknown')
        with self.assertRaises(ValidationError):self.e.start_strategy({})

    def test_manual_lock_survives_successful_balance_verification(self):
        c=self.close();self.reserve(c);self.intent(c)
        self.e.strategy_lock_account(self.a['id'],True)
        self.verify(c)
        self.assertTrue(self.a['strategy_locked']);self.assertEqual(self.a['strategy_lock_source'],'manual')
        self.assertFalse(c.get('reentry_scheduled'))

    def test_manual_lock_before_intent_prevents_submission(self):
        c=self.close();self.reserve(c);self.e.strategy_lock_account(self.a['id'],True)
        with self.assertRaises(ValidationError):self.intent(c)
        self.assertEqual(c['status'],'preparing')

    def test_recipient_amount_and_source_identity_are_validated_before_intent(self):
        c=self.close();self.reserve(c)
        for q in [dict(recipient='external'),dict(amount=41),dict(received=41),dict(available=39),dict(amount=True),
                  dict(flat=None),dict(flat=dict(leverage=1,position_margin=0,order_margin=0,observed_at=self.now))]:
            with self.subTest(q=q),self.assertRaises(ValidationError):self.intent(c,**q)
            self.assertEqual(c['status'],'preparing')
        for patch in [dict(name='Other'),dict(phase='1/2'),dict(mode_epoch=2),dict(tab_url='https://evil.example/portfolio')]:
            with self.subTest(patch=patch),self.assertRaises(ValidationError):
                self.e.bridge_profit_claim({**self.body(),'claim_id':c['id'],'action':'intent',**patch})

    def test_portfolio_heartbeat_renews_only_the_reserved_claim_and_same_epoch(self):
        c=self.close()
        hb={**self.body(),'claim_id':c['id'],'action':'heartbeat','tab_url':PORTFOLIO}
        with self.assertRaises(ValidationError):self.e.bridge_profit_claim(hb)
        self.reserve(c);self.now+=20
        self.assertEqual(self.e.bridge_profit_claim(hb)['mode'],'execute')
        self.now+=20;self.assertTrue(self.e._execution_capable(self.a['id']))
        with self.assertRaises(ValidationError):self.e.bridge_profit_claim({**hb,'mode_epoch':2})
        self.now+=100
        with self.assertRaises(ValidationError):self.e.bridge_profit_claim(hb)

    def test_delivered_submission_cannot_be_cancelled_or_replayed_after_revocation(self):
        c=self.close();self.reserve(c);self.intent(c)
        self.e.bridge_session(dict(session_id='profit-claim-session',mode='readonly',mode_epoch=2,tab_url=TRADE,accounts=self.rows,heartbeat_only=True))
        self.assertEqual(c['status'],'unknown')
        with self.assertRaises(ValidationError):self.e.cancel_profit_claim(c['id'],{'acknowledge':True})
        with self.assertRaises(ValidationError):self.intent(c)

    def test_unsubmitted_claim_can_be_explicitly_cancelled_only_when_paused_readonly(self):
        c=self.close();self.reserve(c)
        with self.assertRaises(ValidationError):self.e.cancel_profit_claim(c['id'],{'acknowledge':True})
        self.e.bridge_session(dict(session_id='profit-claim-session',mode='readonly',mode_epoch=2,tab_url=TRADE,accounts=self.rows,heartbeat_only=True))
        self.e.cancel_profit_claim(c['id'],{'acknowledge':True})
        self.assertEqual(c['status'],'cancelled');self.assertTrue(self.e._daily_entry_used(self.a['id']))

    def test_restart_keeps_submission_unknown_and_readonly_reconciliation_never_resumes_trading(self):
        c=self.close();self.reserve(c);self.intent(c)
        with tempfile.TemporaryDirectory() as folder:
            db=Path(folder)/'claim.sqlite';backup=Engine(db,seed=False,clock=lambda:self.now)
            backup.data=copy.deepcopy(self.e.data);backup._save();backup.close()
            e=Engine(db,seed=False,clock=lambda:self.now)
            try:
                claim=e.data['profit_claims'][c['id']]
                self.assertEqual(claim['status'],'unknown');self.assertEqual(e.data['strategy']['status'],'paused')
                with self.assertRaises(ValidationError):e.bridge_profit_claim({**self.body(),'claim_id':c['id'],'action':'intent','tab_url':PORTFOLIO})
                e.bridge_session(dict(session_id='profit-claim-session',mode='readonly',mode_epoch=2,tab_url=TRADE,accounts=self.rows,heartbeat_only=True))
                e.bridge_inspect({**self.body(),'mode_epoch':2,'snapshot':self.snapshot(5000)})
                self.assertTrue(e.reconcile_profit_claim(c['id'],{'acknowledge':True})['verified'])
                self.assertEqual(e.data['strategy']['status'],'paused');self.assertFalse(claim.get('reentry_scheduled'))
            finally:e.close()
