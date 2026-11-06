import copy
import random
import tempfile
import unittest
from pathlib import Path

from server.engine import Engine, ValidationError
from server.trading_day import trading_day_window


class FailedAccountArchiveTests(unittest.TestCase):
    def setUp(self):
        self.now=1793860000.0
        self.engine=Engine(':memory:',seed=False,clock=lambda:self.now,rng=random.Random(7))
        self.sid='failed-archive-session'
        self.register()
        self.engine.settings({'mode':'browser'})

    def tearDown(self):
        self.engine.close()

    def register(self,failed=None,stamp=None,duplicates=False,mode='execute',epoch=1,category='failed'):
        rows=[{'name':name,'category':'default' if name=='主账户' else category if name==failed else 'eval',
               'current':i==0,'balance':5000,'equity':5000,'unrealized_pnl':0,'observed_at':self.now,
               'status_observed_at':self.now if stamp is None else stamp}
              for i,name in enumerate(('A','B','C','主账户'))]
        if failed=='主账户':rows[-1]['category']='failed'
        if duplicates:rows.append(copy.deepcopy(rows[0]))
        return self.engine.bridge_session({'session_id':self.sid,'mode':mode,'mode_epoch':epoch,
            'authorize':True,'tab_url':'https://next.vestmarkets.com/trade/NQ-PERP','accounts':rows})

    def account(self,name):
        return next(a for a in self.engine.data['accounts'].values() if a['name']==name)

    def start(self,batch_size=1):
        self.engine.start_strategy({'account_ids':[self.account(name)['id'] for name in ('A','B','C')],
            'batch_size':batch_size,'quantity':4,'side':'buy','stop_loss':130,
            'order_delay_min':0,'order_delay_max':0,'batch_delay_min':15,'batch_delay_max':15})
        self.engine.tick(100)
        job=next(j for j in self.engine.data['jobs'].values() if j['status']=='queued')
        account=self.engine._account(job['account_id'])
        delivered=self.engine.bridge_poll({'account_id':account['id'],'session_id':self.sid,'mode_epoch':1,
            'identity':account['identity'],'ready':True,'tab_url':'https://next.vestmarkets.com/trade/NQ-PERP',
            'snapshot':self.snapshot()})['job']
        return account,delivered

    def snapshot(self,**changes):
        return {'balance':5000,'equity':5000,'unrealized_pnl':0,'price':100,'symbol':'NQ-PERP',
                'position_open':False,'observed_at':self.now,**changes}

    def result(self,account,job,**changes):
        return self.engine.bridge_result({'account_id':account['id'],'job_id':job['id'],'session_id':self.sid,
            'success':True,'confirmed':True,'snapshot':self.snapshot(position_open=True,position_id='native-position',
                entry_price=100,side='buy',quantity=4),**changes})

    def test_native_failure_with_managed_position_advances_after_jitter_without_revoke_or_fake_history(self):
        account,job=self.start()
        self.result(account,job)
        original=copy.deepcopy(self.engine._position(account['id']))
        self.now+=1
        reply=self.register(failed=account['name'])
        self.assertEqual(reply['mode'],'execute')
        self.assertTrue(account['retired_failed'])
        self.assertTrue(account['archived'])
        self.assertEqual(account['archive_source'],'automatic')
        self.assertFalse(self.engine._position(account['id']))
        audit=next(a for a in reversed(self.engine.data['audit']) if a.get('event')=='platform_evaluation_failure')
        self.assertEqual(audit['positions_archived_without_execution'],[original])
        self.assertFalse(self.engine.data['history'])
        self.assertEqual(self.engine.data['strategy']['status'],'running')
        self.engine.tick(100)
        strategy=self.engine.data['strategy']
        self.assertEqual(strategy['status'],'waiting')
        self.assertEqual(strategy['next_action_at'],self.now+15)
        self.now+=15
        self.register()
        self.engine.tick(100)
        self.assertEqual(strategy['batch_number'],2)
        self.assertNotIn(account['id'],strategy['batch_account_ids'])
        self.assertIsNotNone(self.register()['next_account'])

    def test_failure_during_delivered_open_archives_once_and_late_result_cannot_recreate_position(self):
        account,job=self.start()
        original=copy.deepcopy(self.engine.data['jobs'][job['id']])
        self.register(failed=account['name'])
        self.assertEqual(self.engine.data['jobs'][job['id']]['status'],'archived_failed')
        self.assertTrue(self.engine._daily_entry_used(account['id']))
        audit_count=len(self.engine.data['audit'])
        self.register(failed=account['name'])
        self.assertEqual(len(self.engine.data['audit']),audit_count)
        audit=next(a for a in self.engine.data['audit'] if a.get('event')=='platform_evaluation_failure')
        self.assertEqual(audit['original_jobs'],[original])
        receipt=self.result(account,job)
        self.assertTrue(receipt['ok'])
        self.assertEqual(receipt['status'],'archived_failed')
        self.assertFalse(self.engine.data['positions'])
        self.assertFalse(self.engine.data['history'])
        self.assertEqual(self.engine.state()['bridge']['pending_jobs'],0)
        self.assertEqual(self.engine.data['strategy']['status'],'running')

    def test_one_failed_member_does_not_advance_while_another_batch_member_is_still_open(self):
        account,job=self.start(batch_size=2)
        self.result(account,job)
        self.engine.tick(100)
        other_job=next(j for j in self.engine.data['jobs'].values() if j['status']=='queued')
        other=self.engine._account(other_job['account_id'])
        poll={'account_id':other['id'],'session_id':self.sid,'mode_epoch':1,'identity':other['identity'],
              'ready':True,'tab_url':'https://next.vestmarkets.com/trade/NQ-PERP','snapshot':self.snapshot()}
        second=self.engine.bridge_poll(poll)['job']
        self.result(other,second)
        self.register(failed=account['name'])
        self.engine.tick(100)
        strategy=self.engine.data['strategy']
        self.assertEqual(strategy['phase'],'monitoring')
        self.assertEqual(strategy['batch_number'],1)
        self.assertIsNotNone(self.engine._position(other['id']))
        self.engine.bridge_poll({**poll,'snapshot':self.snapshot(balance=4800,equity=4800)})
        self.engine.tick(100)
        self.assertEqual(strategy['phase'],'cooldown')
        self.assertEqual(len(self.engine.data['history']),1,'only the observed healthy-account close enters trade history')

    def test_auto_archive_off_still_settles_failure_and_manual_archive_works_during_remaining_strategy(self):
        self.engine.settings({'auto_archive_failed':False})
        account,job=self.start()
        self.register(failed=account['name'])
        self.assertTrue(account['retired_failed'])
        self.assertFalse(account['archived'])
        self.assertFalse(account['enabled'])
        self.assertEqual(self.engine.state()['bridge']['pending_jobs'],0)
        with self.assertRaises(ValidationError):self.engine.archive_account(account['id'],{})
        self.assertTrue(self.engine.archive_account(account['id'],{'acknowledge':True})['archived'])
        self.assertEqual(account['archive_source'],'user')
        self.assertTrue(self.engine.archive_account(account['id'],{'acknowledge':True})['duplicate'])
        self.assertEqual(self.engine.data['strategy']['status'],'running')

    def test_enabling_auto_archive_catches_previously_failed_records_without_trading_or_changing_money(self):
        self.engine.settings({'auto_archive_failed':False})
        self.register(failed='A')
        account=self.account('A')
        before=copy.deepcopy(account)
        self.engine.settings({'auto_archive_failed':True})
        self.assertTrue(account['archived'])
        for key in ('balance','equity','unrealized_pnl','observed_at'):
            self.assertEqual(account[key],before[key])
        self.assertFalse(self.engine.data['jobs'])
        self.assertFalse(self.engine.data['positions'])

    def test_stale_ambiguous_or_primary_failure_is_not_automatically_finalized(self):
        self.register(failed='A',stamp=self.now-6)
        self.assertFalse(self.account('A').get('retired_failed'))
        self.register(failed='A',duplicates=True)
        self.assertFalse(self.account('A').get('retired_failed'))
        self.register(failed='主账户')
        self.assertFalse(self.account('主账户').get('retired_failed'))
        self.assertTrue(self.account('主账户')['execution_excluded'])
        self.assertFalse(self.account('主账户')['enabled'])

    def test_new_failure_badge_can_be_recorded_without_fresh_money_but_missing_account_is_not_failure(self):
        account=self.account('A')
        old_balance=account['balance'];old_observed=account['observed_at']
        self.now+=10
        body={'session_id':self.sid,'mode':'execute','mode_epoch':1,'tab_url':'https://next.vestmarkets.com/trade/NQ-PERP',
              'accounts':[{'name':'A','category':'failed','current':True,'balance':None,'equity':None,'unrealized_pnl':None,
                           'observed_at':None,'status_observed_at':self.now}]}
        self.engine.bridge_session(body)
        self.assertTrue(account['archived'])
        self.assertEqual(account['balance'],old_balance)
        self.assertEqual(account['observed_at'],old_observed)
        self.assertFalse(self.account('B').get('retired_failed'),'absence is not evidence of failure')

    def test_unrelated_unknown_still_blocks_and_paused_strategy_is_never_automatically_resumed(self):
        account,job=self.start()
        self.result(account,job,success=False,confirmed=False,error='ambiguous execution')
        self.assertEqual(self.engine.data['strategy']['status'],'paused')
        self.register(failed='B',mode='readonly',epoch=2)
        self.assertEqual(self.engine.data['jobs'][job['id']]['status'],'unknown')
        with self.assertRaises(ValidationError):self.engine._assert_settled_browser_actions()
        self.register(failed=account['name'],mode='readonly',epoch=2)
        self.engine._assert_settled_browser_actions()
        self.assertEqual(self.engine.data['strategy']['status'],'paused')
        self.assertEqual(self.engine.data['execution_sessions'][self.sid]['mode'],'readonly')

    def test_manual_archive_rejects_primary_and_implicit_confirmation_and_preferences_are_atomic(self):
        before=copy.deepcopy(self.engine.data)
        for name in ('主账户',):
            with self.assertRaises(ValidationError):self.engine.archive_account(self.account(name)['id'],{'acknowledge':True})
        with self.assertRaises(ValidationError):self.engine.archive_account(self.account('A')['id'],{})
        with self.assertRaises(ValidationError):self.engine.settings({'auto_archive_failed':'true'})
        self.assertEqual(self.engine.data,before)

    def test_closed_badge_with_live_position_ends_account_without_identity_freeze_and_preserves_other_execution(self):
        account,job=self.start()
        self.result(account,job)
        reply=self.register(failed=account['name'],category='closed')
        self.assertEqual(reply['mode'],'execute')
        self.assertEqual(account['category'],'closed')
        self.assertEqual(account['terminal_status'],'closed')
        self.assertTrue(account['archived'])
        self.assertFalse(self.engine._position(account['id']))
        self.assertFalse(self.engine.data['history'])
        self.assertEqual(self.engine.data['strategy']['status'],'running')
        self.engine.tick(100)
        self.assertEqual(self.engine.data['strategy']['phase'],'cooldown')
        # A cached evaluation row cannot undo an observed closure.
        self.register()
        self.assertEqual(account['category'],'closed')
        self.assertFalse(account['enabled'])
        self.assertFalse(self.engine._execution_capable(account['id']))
        audit=next(a for a in self.engine.data['audit'] if a.get('event')=='platform_evaluation_failure')
        self.assertEqual(audit['evidence']['category'],'closed')

    def test_closed_current_account_resolves_old_unknown_with_hidden_money_and_readonly_permission(self):
        account,job=self.start()
        self.result(account,job,success=False,confirmed=False,error='old-link-error')
        original=copy.deepcopy(self.engine.data['jobs'][job['id']])
        self.engine.bridge_session({'session_id':self.sid,'mode':'readonly','mode_epoch':2,
            'tab_url':'https://next.vestmarkets.com/trade/NQ-PERP','accounts':[
                {'name':account['name'],'category':'closed','current':True,'observed_at':None,
                 'balance':None,'equity':None,'unrealized_pnl':None,'status_observed_at':self.now}]})
        self.assertEqual(self.engine.state()['bridge']['pending_jobs'],0)
        self.assertEqual(self.engine.data['jobs'][job['id']]['status'],'archived_failed')
        self.assertEqual(self.engine.data['strategy']['status'],'paused')
        self.assertEqual(self.engine.data['execution_sessions'][self.sid]['mode'],'readonly')
        self.assertEqual(account['category'],'closed')
        self.assertEqual(account['balance'],5000)
        self.assertEqual(self.engine.data['audit'][-1]['original_jobs'],[original])
        self.assertFalse(self.engine.data['history'])

    def test_stale_closed_and_missing_account_do_not_become_terminal(self):
        self.register(failed='A',category='closed',stamp=self.now-6)
        self.assertFalse(self.account('A').get('retired_failed'))
        self.register(failed='A',category='closed',duplicates=True)
        self.assertFalse(self.account('A').get('retired_failed'))

    def test_user_can_archive_legacy_unknown_locked_state_but_not_an_active_unknown_account(self):
        account,job=self.start()
        self.result(account,job,success=False,confirmed=False,error='old-link-error')
        self.engine.stop_strategy()
        self.now+=31
        account.update({'category':'unknown','identity':'unknown:'+account['name'],'available':True})
        with self.assertRaises(ValidationError):self.engine.archive_account(account['id'],{})
        result=self.engine.archive_account(account['id'],{'acknowledge':True,'terminal_status':'failed'})
        self.assertTrue(result['archived'])
        self.assertEqual(account['failure_confirmation_source'],'user')
        self.assertEqual(self.engine.state()['bridge']['pending_jobs'],0)
        self.assertFalse(self.engine.data['history'])
        other=self.account('B')
        other['category']='unknown'
        with self.assertRaises(ValidationError):self.engine.archive_account(other['id'],{'acknowledge':True,'terminal_status':'failed'})

    def test_manual_archive_healthy_preserves_platform_state_cash_and_permissions(self):
        account=self.account('A')
        original=copy.deepcopy(account)
        sessions=copy.deepcopy(self.engine.data['execution_sessions'])
        result=self.engine.archive_account(account['id'],{'acknowledge':True})
        self.assertEqual(result['archive_kind'],'manual')
        self.assertTrue(account['archived'])
        self.assertFalse(account['enabled'])
        self.assertFalse(account.get('retired_failed'))
        for key in ('category','status','lock_reason','balance','equity','unrealized_pnl','observed_at'):
            self.assertEqual(account[key],original[key])
        self.assertEqual(self.engine.data['execution_sessions'],sessions)
        self.assertFalse(self.engine.data['history'])
        self.assertFalse(self.engine.data['jobs'])
        saved=next(a for a in self.engine.state()['accounts'] if a['id']==account['id'])
        self.assertFalse(saved['executable'])
        self.assertEqual(saved['execution_mode'],'readonly')
        self.assertEqual(self.engine.data['audit'][-1]['account_before'],original)
        before=copy.deepcopy(self.engine.data)
        self.assertTrue(self.engine.archive_account(account['id'],{'acknowledge':True})['duplicate'])
        self.assertEqual(self.engine.data,before)

    def test_manual_archive_cancels_queued_open_without_pausing_other_batch_members(self):
        first,job=self.start(batch_size=2)
        self.result(first,job)
        self.engine.tick(100)
        queued=next(j for j in self.engine.data['jobs'].values() if j['status']=='queued')
        account=self.engine._account(queued['account_id'])
        positions=copy.deepcopy(self.engine.data['positions'])
        sessions=copy.deepcopy(self.engine.data['execution_sessions'])
        self.engine.archive_account(account['id'],{'acknowledge':True})
        self.assertEqual(queued['status'],'cancelled')
        self.assertFalse(self.engine._daily_entry_used(account['id']))
        self.assertEqual(self.engine.data['positions'],positions)
        self.assertEqual(self.engine.data['execution_sessions'],sessions)
        self.assertEqual(self.engine.data['strategy']['status'],'running')
        self.engine.tick(100)
        self.assertEqual(self.engine.data['strategy']['phase'],'monitoring')
        self.assertEqual(self.engine.data['strategy']['batch_number'],1)
        self.assertNotIn(account['id'],self.engine.data['strategy']['order_queue'])
        self.assertFalse(self.engine.data['history'])
        self.register()
        self.assertTrue(account['archived'])
        self.assertFalse(account['enabled'])
        self.assertFalse(self.engine._execution_capable(account['id']))

    def test_manual_archive_refuses_managed_native_external_and_uncertain_trades(self):
        account=self.account('A')
        self.engine.data['positions']['known']={'id':'known','account_id':account['id']}
        with self.assertRaisesRegex(ValidationError,'持仓'):self.engine.archive_account(account['id'],{'acknowledge':True})
        self.engine.data['positions'].clear()
        self.engine.data['bridge_sessions'][account['id']]={'last_seen':self.now,'snapshot':self.snapshot(position_open=True)}
        with self.assertRaisesRegex(ValidationError,'持仓'):self.engine.archive_account(account['id'],{'acknowledge':True})
        self.engine.data['bridge_sessions'].clear()
        account['external_position']=True
        with self.assertRaisesRegex(ValidationError,'持仓'):self.engine.archive_account(account['id'],{'acknowledge':True})
        account['external_position']=False
        first,job=self.start()
        before=copy.deepcopy(self.engine.data)
        with self.assertRaisesRegex(ValidationError,'未确认交易'):self.engine.archive_account(first['id'],{'acknowledge':True})
        self.assertEqual(self.engine.data,before)
        self.result(first,job,success=False,confirmed=False,error='unknown result')
        before=copy.deepcopy(self.engine.data)
        with self.assertRaisesRegex(ValidationError,'未确认交易'):self.engine.archive_account(first['id'],{'acknowledge':True})
        self.assertEqual(self.engine.data,before)

    def test_manual_archive_unknown_without_trades_does_not_claim_evaluation_failure(self):
        account=self.account('A')
        account.update({'category':'unknown','status':'locked','lock_reason':'读数待更新'})
        self.engine.archive_account(account['id'],{'acknowledge':True})
        self.assertEqual(account['category'],'unknown')
        self.assertEqual(account['lock_reason'],'读数待更新')
        self.assertFalse(account.get('retired_failed'))
        self.assertTrue(account['archived'])

    def test_manual_archive_remains_archived_if_platform_later_fails_with_auto_archive_off(self):
        self.engine.settings({'auto_archive_failed':False})
        account=self.account('A')
        self.engine.archive_account(account['id'],{'acknowledge':True})
        archived_at=account['archived_at']
        self.now+=1
        self.register(failed='A')
        self.assertTrue(account['archived'])
        self.assertTrue(account['retired_failed'])
        self.assertEqual(account['archive_source'],'user')
        self.assertEqual(account['archived_at'],archived_at)

    def test_manual_archive_survives_merge_restart_next_day_and_another_browser(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent.parent/'runtime') as folder:
            self.engine.close()
            database=str(Path(folder)/'manual-archive.sqlite')
            self.engine=Engine(database,seed=False,clock=lambda:self.now)
            self.register()
            self.engine.settings({'mode':'browser'})
            account=self.account('A')
            self.engine.archive_account(account['id'],{'acknowledge':True})
            duplicate={**copy.deepcopy(account),'id':'older-manual-archive-copy','created_at':account['created_at']-1,
                       'archived':False,'enabled':True}
            self.engine.data['accounts'][duplicate['id']]=duplicate
            self.now=trading_day_window(self.now)['end']+1
            self.engine.close()
            self.engine=Engine(database,seed=False,clock=lambda:self.now)
            try:
                self.sid='another-manual-browser'
                saved=next(a for a in self.register()['accounts'] if a['name']=='A')
                self.assertTrue(saved['archived'])
                self.assertEqual(saved['archive_kind'],'manual')
                self.assertFalse(saved['enabled'])
                self.assertFalse(saved['executable'])
                self.assertFalse(saved.get('retired_failed'))
                self.assertEqual(saved['category'],'eval')
                self.assertEqual(len([a for a in self.engine.state()['accounts'] if a['name']=='A']),1)
                for action in (lambda:self.engine.toggle_account(saved['id'],True),
                               lambda:self.engine.strategy_lock_account(saved['id'],False),
                               lambda:self.engine._new_job('open',saved['id']),
                               lambda:self.engine.delete_account(saved['id'])):
                    with self.assertRaises(ValidationError):action()
            finally:
                self.engine.close()
                self.engine=Engine(':memory:',seed=False,clock=lambda:self.now)
