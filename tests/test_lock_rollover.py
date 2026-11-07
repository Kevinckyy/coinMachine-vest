import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from server.engine import Engine, ValidationError


def stamp(value):
    return datetime.fromisoformat(value.replace('Z','+00:00')).timestamp()


class LockRolloverTests(unittest.TestCase):
    def setUp(self):
        self.now=stamp('2026-11-05T00:59:00Z')
        self.engine=Engine(':memory:',seed=False,clock=lambda:self.now)

    def tearDown(self):
        self.engine.close()

    def account(self, name='Rollover'):
        aid=self.engine.add_account({'name':name,'initial_balance':1000,'profit_target':100,'max_loss':50})['id']
        return self.engine.data['accounts'][aid]

    def test_all_strategy_lock_sources_clear_at_next_new_york_close(self):
        for source in ('manual','external_close','daily_completed','take_profit','stop_loss','target_reached','evaluation_passed'):
            account=self.account(source)
            if source=='evaluation_passed':
                account['status']='passed'
            self.engine._lock_strategy(account,source,source)
        self.now=stamp('2026-11-05T01:00:00Z')
        self.engine.tick(price=100)
        for account in self.engine.state()['accounts']:
            with self.subTest(account=account['name']):
                self.assertFalse(account['strategy_locked'])
                self.assertIsNone(account['strategy_lock_source'])
                self.assertIsNone(account['strategy_locked_at'])
                self.assertIsNone(account['strategy_unlock_at'])
        self.assertEqual(self.engine.data['strategy']['status'],'idle')
        self.assertFalse(self.engine.data['jobs'])

    def test_beijing_midnight_does_not_unlock_before_20_new_york(self):
        self.now=stamp('2026-11-04T15:59:59Z')
        account=self.account()
        self.engine.strategy_lock_account(account['id'],True)
        self.now+=2
        self.assertTrue(self.engine.state()['accounts'][0]['strategy_locked'])

    def test_dst_cutoffs_and_expiry_metadata(self):
        for start,end in [('2026-03-08T01:00:00Z','2026-03-09T00:00:00Z'),
                          ('2026-11-01T00:00:00Z','2026-11-02T01:00:00Z')]:
            with self.subTest(start=start):
                self.now=stamp(start)
                account=self.account(start)
                self.engine.strategy_lock_account(account['id'],True)
                shown=next(a for a in self.engine.state()['accounts'] if a['id']==account['id'])
                self.assertEqual(shown['strategy_unlock_at'],stamp(end))
                self.now=stamp(end)-1
                self.assertTrue(next(a for a in self.engine.state()['accounts'] if a['id']==account['id'])['strategy_locked'])
                self.now+=1
                self.assertFalse(next(a for a in self.engine.state()['accounts'] if a['id']==account['id'])['strategy_locked'])

    def test_offline_rollover_persists_and_preserves_platform_risk_and_unknown_jobs(self):
        runtime=Path(__file__).resolve().parent.parent/'runtime'
        with tempfile.TemporaryDirectory(dir=runtime) as folder:
            self.engine.close()
            database=str(Path(folder)/'rollover.sqlite')
            self.engine=Engine(database,seed=False,clock=lambda:self.now)
            passed=self.account('Passed')
            passed['status']='passed'
            self.engine._lock_strategy(passed,'Passed','evaluation_passed')
            risk=self.account('Unknown result')
            risk.update({'status':'locked','lock_reason':'操作结果未知'})
            self.engine.strategy_lock_account(risk['id'],True)
            self.engine.data['jobs']['unknown-entry']={'id':'unknown-entry','account_id':risk['id'],
                'type':'open','status':'unknown','created_at':self.now,'delivered_at':self.now,'source':'strategy'}
            self.engine.close()
            self.now+=2*86400
            self.engine=Engine(database,seed=False,clock=lambda:self.now)
            try:
                accounts={a['id']:a for a in self.engine.state()['accounts']}
                self.assertFalse(accounts[passed['id']]['strategy_locked'])
                self.assertEqual(accounts[passed['id']]['status'],'passed')
                self.assertFalse(accounts[risk['id']]['strategy_locked'])
                self.assertEqual(accounts[risk['id']]['status'],'locked')
                self.assertEqual(accounts[risk['id']]['lock_reason'],'操作结果未知')
                self.assertEqual(self.engine.data['jobs']['unknown-entry']['status'],'unknown')
                self.engine._account_status(self.engine.data['accounts'][passed['id']])
                self.assertFalse(self.engine.data['accounts'][passed['id']]['strategy_locked'])
                with self.assertRaises(ValidationError):
                    self.engine._eligible(risk['id'],for_strategy=True)
            finally:
                self.engine.close()
                self.engine=Engine(':memory:',seed=False,clock=lambda:self.now)

    def test_repeated_passed_observation_does_not_recreate_yesterday_strategy_lock(self):
        def observe():
            return self.engine.bridge_session({'session_id':'passed-rollover','mode':'readonly','mode_epoch':0,
                'tab_url':'https://next.vestmarkets.com/trade/NQ-PERP','accounts':[
                    {'name':'Passed browser','category':'passed','current':True,'observed_at':self.now,
                     'balance':1100,'equity':1100,'unrealized_pnl':0}]})['accounts'][0]
        self.assertTrue(observe()['strategy_locked'])
        self.now=stamp('2026-11-05T01:00:00Z')
        account=observe()
        self.assertFalse(account['strategy_locked'])
        self.assertEqual(account['status'],'passed')
        self.assertFalse(account['executable'])
        self.assertFalse(observe()['strategy_locked'])

    def test_explicit_new_day_manual_lock_stays_until_following_close(self):
        account=self.account()
        self.engine.strategy_lock_account(account['id'],True)
        self.now=stamp('2026-11-05T01:00:00Z')
        self.engine.strategy_lock_account(account['id'],True)
        shown=self.engine.state()['accounts'][0]
        self.assertTrue(shown['strategy_locked'])
        self.assertEqual(shown['strategy_locked_at'],self.now)
        self.assertEqual(shown['strategy_unlock_at'],stamp('2026-11-06T01:00:00Z'))
