import copy
import tempfile
import unittest
from pathlib import Path

from server.engine import Engine, ValidationError


class AccountDedupTests(unittest.TestCase):
    FIRST='11111111-1111-4111-8111-111111111111:aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
    SECOND='22222222-2222-4222-8222-222222222222:bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'

    def setUp(self):
        self.now=1793800000.0
        self.engine=Engine(':memory:',seed=False,clock=lambda:self.now)

    def tearDown(self):
        self.engine.close()

    def session(self, sid, mode='readonly', epoch=0, authorize=False, balance=1000, names=None):
        rows=[{'name':name,'category':'eval','current':True,'observed_at':self.now,
               'balance':balance,'equity':balance,'unrealized_pnl':0} for name in (names or ['Alpha'])]
        return self.engine.bridge_session({'session_id':sid,'mode':mode,'mode_epoch':epoch,'authorize':authorize,
            'tab_url':'https://next.vestmarkets.com/trade/NQ-PERP','accounts':rows})

    def order(self, aid, key='dedup-order-first'):
        return {'account_ids':[aid],'side':'buy','quantity':1,'stop_loss':130,'take_profit':None,
                'idempotency_key':key,'symbol':'NQ-PERP'}

    def test_repeat_read_and_different_browser_reuse_global_name_and_latest_balance(self):
        first=self.session(self.FIRST)['accounts'][0]
        self.now+=1
        again=self.session(self.FIRST,balance=1010)['accounts'][0]
        self.now+=1
        other=self.session(self.SECOND,balance=1020)['accounts'][0]
        self.assertEqual(first['id'],again['id'])
        self.assertEqual(first['id'],other['id'])
        self.assertEqual(len(self.engine.state()['accounts']),1)
        self.assertEqual(other['balance'],1020)
        self.assertEqual(other['initial_balance'],1000)
        self.assertEqual(other['realized_pnl'],20)

    def test_full_names_remain_distinct(self):
        self.session(self.FIRST,names=['Alpha','Alpha 2','Alpha-2'])
        self.session(self.SECOND,names=['Alpha','Alpha 2','Alpha-2'])
        self.assertEqual(len(self.engine.state()['accounts']),3)

    def test_another_browser_cannot_execute_the_same_name_and_readonly_does_not_revoke_owner(self):
        account=self.session(self.FIRST,'execute',1,True)['accounts'][0]
        self.engine.settings({'mode':'browser'})
        self.engine.place_orders(self.order(account['id']))
        self.session(self.SECOND)
        with self.assertRaises(ValidationError):
            self.session(self.SECOND,'execute',1,True)
        self.assertEqual(self.engine.data['accounts'][account['id']]['session_id'],self.FIRST)
        self.assertEqual(next(iter(self.engine.data['jobs'].values()))['status'],'queued')
        self.session(self.FIRST,'readonly',2)
        claimed=self.session(self.SECOND,'execute',1,True)['accounts'][0]
        self.assertEqual(claimed['id'],account['id'])
        self.assertEqual(claimed['session_id'],self.SECOND)
        self.engine.place_orders(self.order(account['id'],'dedup-second-owner'))
        self.session(self.FIRST,'readonly',2)
        self.assertEqual(self.engine.data['accounts'][account['id']]['session_id'],self.SECOND)
        current=[j for j in self.engine.data['jobs'].values() if j['status']=='queued']
        self.assertEqual(len(current),1)
        self.assertEqual(current[0]['session_id'],self.SECOND)

    def test_legacy_duplicate_merge_preserves_history_daily_usage_and_lock(self):
        runtime=Path(__file__).resolve().parent.parent/'runtime'
        with tempfile.TemporaryDirectory(dir=runtime) as folder:
            self.engine.close()
            database=str(Path(folder)/'duplicates.sqlite')
            self.engine=Engine(database,seed=False,clock=lambda:self.now)
            original=self.session(self.FIRST)['accounts'][0]
            self.engine.strategy_lock_account(original['id'],True)
            self.now+=2
            duplicate=copy.deepcopy(self.engine.data['accounts'][original['id']])
            duplicate.update({'id':'old-tab-copy','session_id':self.SECOND,'created_at':self.now,'observed_at':self.now,
                'balance':1200,'equity':1200,'initial_balance':1100,'strategy_locked':False,
                'strategy_lock_source':None,'strategy_lock_reason':None,'strategy_locked_at':None})
            self.engine.data['accounts'][duplicate['id']]=duplicate
            self.engine.data['execution_sessions'][self.SECOND]={'session_id':self.SECOND,'mode':'readonly','mode_epoch':0,
                'account_ids':[duplicate['id']],'last_seen':self.now,'cursor':0}
            for index,aid,pnl in [(1,original['id'],30),(2,duplicate['id'],15)]:
                self.engine.data['history'].append({'id':'historic-'+str(index),'account_id':aid,'mode':'browser',
                    'opened_at':self.now-2,'closed_at':self.now-1,'pnl':pnl})
            self.engine.close()
            self.engine=Engine(database,seed=False,clock=lambda:self.now)
            try:
                state=self.engine.state()
                self.assertEqual(len(state['accounts']),1)
                account=state['accounts'][0]
                self.assertEqual(account['id'],original['id'])
                self.assertEqual(account['balance'],1200)
                self.assertEqual(account['initial_balance'],1000)
                self.assertTrue(account['strategy_locked'])
                self.assertTrue(account['daily_entry_used'])
                self.assertEqual(account['daily_realized_pnl'],45)
                self.assertEqual(len(state['history']),2)
                self.assertTrue(all(t['account_id']==account['id'] for t in state['history']))
                self.assertEqual(self.engine.data['accounts']['old-tab-copy']['merged_into'],account['id'])
                with self.assertRaises(ValidationError):
                    self.engine._account('old-tab-copy')
                self.assertEqual(self.session(self.SECOND)['accounts'][0]['id'],account['id'])
                self.assertEqual(len(self.engine.state()['accounts']),1)
            finally:
                self.engine.close()
                self.engine=Engine(':memory:',seed=False,clock=lambda:self.now)

    def test_same_name_positions_are_retained_and_blocked_for_review(self):
        original=self.session(self.FIRST)['accounts'][0]
        duplicate=copy.deepcopy(self.engine.data['accounts'][original['id']])
        duplicate.update({'id':'other-browser-copy','session_id':self.SECOND})
        self.engine.data['accounts'][duplicate['id']]=duplicate
        for index,aid in enumerate((original['id'],duplicate['id'])):
            self.engine.data['positions'][str(index)]={'id':str(index),'account_id':aid,'mode':'browser','source':'strategy',
                'opened_at':self.now,'quantity':1,'side':'buy','entry_price':100,'mark_price':100,'unrealized_pnl':0,
                'take_profit':None,'stop_loss':130,'status':'open'}
        self.engine._deduplicate_browser_accounts()
        state=self.engine.state()
        self.assertEqual(len(state['accounts']),1)
        self.assertEqual(len(state['positions']),2)
        self.assertEqual(len({p['account_id'] for p in state['positions']}),1)
        self.assertEqual(state['accounts'][0]['status'],'locked')
        self.assertTrue(state['accounts'][0]['dedup_conflict'])
