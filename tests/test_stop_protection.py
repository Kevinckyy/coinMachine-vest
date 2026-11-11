import unittest
import tempfile
import json
from pathlib import Path
from server.engine import Engine, ValidationError
from server.stop_protection import stop_plan


class StopProtectionTests(unittest.TestCase):
    def setUp(self):
        self.now=1790000000.
        self.engine=Engine(':memory:',seed=False,clock=lambda:self.now)
        self.row=dict(name='Fixture stop account',category='eval',equity=5000,balance=5000,
            unrealized_pnl=0,current=True,observed_at=self.now)
        self.session=dict(session_id='fixture-stop-session',mode='execute',mode_epoch=1,
            authorize=True,tab_url='https://next.vestmarkets.com/trade/NQ-PERP',accounts=[self.row])
        self.account=self.engine.bridge_session(self.session)['accounts'][0]
        self.engine.settings({'mode':'browser'})
        self.engine.start_strategy(dict(account_ids=[self.account['id']],batch_size=1,quantity=4,
            side='sell',symbol='NQ-PERP',stop_loss=130,take_profit=None,order_delay_min=0,
            order_delay_max=0,batch_delay_min=0,batch_delay_max=0))
        self.engine.tick(price=28000)
        self.snapshot=dict(balance=5000,equity=5000,unrealized_pnl=0,price=28000,position_open=False,observed_at=self.now)
        self.auth=dict(account_id=self.account['id'],session_id=self.account['session_id'],
            mode_epoch=1,identity=self.account['identity'],tab_url=self.session['tab_url'])
        self.job=self.engine.bridge_poll({**self.auth,'snapshot':self.snapshot,'ready':True})['job']
        self.fill={**self.snapshot,'position_open':True,'position_id':'fixture-real-position',
            'symbol':'NQ-PERP','side':'sell','quantity':4,'entry_price':28001.5,'native_sl_price':28032.5,'native_sl':True}
        self.body={**self.auth,'job_id':self.job['id'],'snapshot':self.fill,'target_price':28034.0}

    def tearDown(self):self.engine.close()

    def result(self,fill):
        return self.engine.bridge_result({**self.auth,'job_id':self.job['id'],'success':True,'confirmed':True,'snapshot':fill})

    def test_calculation_uses_fill_not_mark_or_contract_multiplier(self):
        p=stop_plan(self.fill,130)
        self.assertEqual(p['actual_risk'],124)
        self.assertEqual(p['target_price'],28034)
        self.assertEqual(p['status'],'mismatch')
        p=stop_plan({**self.fill,'side':'buy','native_sl_price':27969},130)
        self.assertEqual(p['status'],'verified')
        self.assertEqual(p['actual_risk'],130)

    def test_nearest_quarter_point_and_midpoint_toward_entry(self):
        for side in ('buy','sell'):
            sign = 1 if side == 'sell' else -1
            for quantity, target, distance, risk in ((3, 130, 43.25, 129.75), (6, 130, 21.75, 130.5), (4, 130.5, 32.5, 130)):
                with self.subTest(side=side, quantity=quantity, target=target):
                    price = self.fill['entry_price'] + sign * distance
                    fill = {**self.fill, 'side': side, 'quantity': quantity, 'native_sl_price': price}
                    checked = stop_plan(fill, target)
                    self.assertEqual(checked['target_price'], price)
                    self.assertEqual(checked['actual_risk'], risk)
                    self.assertEqual(checked['status'], 'verified')
                    self.assertEqual(stop_plan({**fill, 'native_sl_price': price + sign * 0.01}, target)['status'], 'mismatch')

    def test_intent_is_persisted_once_and_corrected_result_records_fill(self):
        fill = {**self.fill, 'entry_price': 28001.56}
        body = {**self.body, 'snapshot': fill}
        self.assertTrue(self.engine.bridge_stop_protection(body)['authorize_click'])
        self.assertFalse(self.engine.bridge_stop_protection(body)['authorize_click'])
        self.assertEqual(self.engine.data['jobs'][self.job['id']]['stop_amendment']['position_id'],fill['position_id'])
        self.result({**fill,'native_sl_price':28034,'stop_check':{'status':'verified','corrected':True}})
        pos=self.engine._position(self.account['id'])
        self.assertEqual(pos['stop_protection']['actual_risk'],129.76)
        self.assertTrue(pos['stop_protection']['corrected'])
        self.assertEqual(self.engine.data['jobs'][self.job['id']]['status'],'confirmed')

    def test_failure_keeps_confirmed_fill_and_pauses_without_retry(self):
        self.engine.bridge_stop_protection(self.body)
        self.result({**self.fill,'stop_check':{'status':'failed','error':'native save result unknown'}})
        self.assertIsNotNone(self.engine._position(self.account['id']))
        self.assertEqual(self.engine.data['jobs'][self.job['id']]['status'],'confirmed')
        self.assertEqual(self.engine.data['strategy']['status'],'paused')
        self.assertEqual(self.engine.data['execution_sessions'][self.account['session_id']]['mode'],'readonly')
        self.assertEqual(self.engine._position(self.account['id'])['stop_protection']['actual_risk'],124)
        self.assertTrue(self.result(self.fill)['duplicate'])

    def test_server_independently_rejects_client_claim_of_correct_stop(self):
        self.result({**self.fill,'stop_check':{'status':'verified','actual_risk':130}})
        self.assertEqual(self.engine._position(self.account['id'])['stop_protection']['status'],'mismatch')
        self.assertEqual(self.engine.data['strategy']['status'],'paused')

    def test_correct_stop_no_amendment_and_omission_never_adds_stop(self):
        b={**self.body,'snapshot':{**self.fill,'native_sl_price':28034}}
        self.assertFalse(self.engine.bridge_stop_protection(b)['authorize_click'])
        self.assertNotIn('stop_amendment',self.engine.data['jobs'][self.job['id']])
        self.engine.data['jobs'][self.job['id']]['stop_loss']=None
        with self.assertRaises(ValidationError):self.engine.bridge_stop_protection(self.body)
        self.result({**self.fill,'native_sl_price':None,'native_sl_absent':True})
        self.assertEqual(self.engine._position(self.account['id'])['stop_protection']['status'],'omitted')

    def test_intent_refuses_stale_wrong_account_size_phase_and_price(self):
        for changes in ({'account_id':'other'},{'mode_epoch':0},{'target_price':28035},{'target_price':28034.01},
            {'snapshot':{**self.fill,'observed_at':self.now-6}},
            {'snapshot':{**self.fill,'quantity':8}},{'snapshot':{**self.fill,'side':'buy'}},
            {'snapshot':{**self.fill,'phase':'live'}},{'snapshot':{**self.fill,'position_open':False}}):
            with self.subTest(changes=changes),self.assertRaises(ValidationError):self.engine.bridge_stop_protection({**self.body,**changes})
        self.engine.bridge_session({**self.session,'mode':'readonly','mode_epoch':2,'authorize':False})
        with self.assertRaises(ValidationError):self.engine.bridge_stop_protection(self.body)

    def test_restart_keeps_intent_and_never_replays_delivered_entry(self):
        self.engine.bridge_stop_protection(self.body)
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'stop.sqlite'
            e=Engine(path,seed=False,clock=lambda:self.now)
            e.data=json.loads(json.dumps(self.engine.data));e._save();e.close()
            restarted=Engine(path,seed=False,clock=lambda:self.now)
            try:
                stored=restarted.data['jobs'][self.job['id']]
                self.assertEqual(stored['status'],'unknown')
                self.assertEqual(stored['stop_amendment']['target_price'],28034)
                self.assertEqual(restarted.data['strategy']['status'],'paused')
                with self.assertRaises(ValidationError):restarted.bridge_stop_protection(self.body)
            finally:restarted.close()


if __name__=='__main__':unittest.main()
