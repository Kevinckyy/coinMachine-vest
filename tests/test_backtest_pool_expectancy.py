import copy
import random
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from server.backtest_expectancy import account_value,from_accounts,populate_metrics
from server.backtest_service import BacktestService
from server.backtest_statistics import BatchStatistics
from server.backtesting import simulate,validate_config
from server.engine import Engine,ValidationError
from server.market_history import BacktestStore
from server.strategies import choose_live_first
from server.trading_day import entry_day_window,trading_day_window
from test_backtesting import config,bar,BASE,INSTRUMENT


def stamp(value):return datetime.fromisoformat(value.replace('Z','+00:00')).timestamp()


class PoolTests(unittest.TestCase):
    def cfg(self,**changes):
        return config(account_count='infinity',batch_size=2,size_percent=50,repeat_daily=True,
            pool_limits_enabled=True,exam_pool_limit=2,live_pool_limit=1,**changes)

    def test_defaults_and_validation(self):
        c=validate_config(config());self.assertFalse(c['pool_limits_enabled'])
        self.assertEqual(c['exam_pool_limit'],'infinity');self.assertEqual(c['live_pool_limit'],10)
        for key in ('exam_pool_limit','live_pool_limit'):
            for value in (True,0,-1,1.5,None,'10',float('inf'),1000001):
                with self.subTest(key=key,value=value),self.assertRaises(ValidationError):validate_config(config(**{key:value}))
            self.assertEqual(validate_config(config(**{key:'∞'}))[key],'infinity')
        with self.assertRaises(ValidationError):validate_config(config(pool_limits_enabled='true'))

    def test_full_live_pool_preserves_qualified_accounts_and_waiting_value(self):
        r=simulate(self.cfg(),[bar(BASE+i*60,high=106) for i in range(4)],INSTRUMENT)
        m=r['metrics'];self.assertEqual(m['live_pool_peak'],1);self.assertEqual(m['exam_pool_peak'],2)
        waiting=[a for a in r['accounts'] if a['status']=='waiting_live']
        self.assertEqual(len(waiting),2);self.assertEqual(m['waiting_live_accounts'],2)
        self.assertEqual(m['passed_accounts'],3);self.assertEqual(m['funded_accounts'],1)
        for a in waiting:
            self.assertTrue(a['phase_records']['2/2']['passed'])
            self.assertEqual(a['balance'],1000)
            self.assertEqual(account_value(r['config'],a['phase'],a['balance'],a['status']),240)
        # At minute 2 both a Live account and a fresh exam are eligible.
        same_bar=[t for t in r['trades'] if t['opened_at']==BASE+120]
        self.assertEqual([t['phase'] for t in same_bar],['live','1/2'])
        self.assertEqual(m['remaining_expectancy'],720)

    def test_failed_live_releases_slot_and_waiters_enter_fifo(self):
        r=simulate(self.cfg(),[bar(BASE+i*60,high=106) for i in range(4)]+
            [bar(BASE+86400,low=1),bar(BASE+86460)],INSTRUMENT)
        failed=next(a for a in r['accounts'] if a['phase']=='live' and a['status']=='failed')
        promoted=[a for a in r['accounts'] if a['phase']=='live' and a['id']!=failed['id']]
        self.assertEqual(len(promoted),1);self.assertEqual(promoted[0]['id'],2)
        self.assertEqual(r['metrics']['live_pool_peak'],1)
        self.assertEqual(r['metrics']['exam_pool_peak'],2)
        self.assertTrue(any(t['account_id']==2 and t['phase']=='live' and t['opened_at']==BASE+86460 for t in r['trades']))

    def test_finite_lazy_pool_respects_cap_and_partial_batches(self):
        r=simulate(config(account_count=10000,batch_size=20,pool_limits_enabled=True,exam_pool_limit=1,
            live_pool_limit=1),[bar(),bar(BASE+60)],INSTRUMENT)
        self.assertEqual(r['metrics']['started_accounts'],1);self.assertEqual(r['metrics']['unstarted_accounts'],9999)
        self.assertEqual(r['metrics']['exam_pool_peak'],1);self.assertEqual(r['metrics']['total_trades'],1)
        self.assertEqual(r['metrics']['account_purchase_cost'],150000)
        self.assertGreater(r['metrics']['remaining_expectancy'],0)

    def test_initial_live_cap_and_exhausted_survivors_keep_slots(self):
        r=simulate(config(account_count='infinity',batch_size=20,initial_phase='live',live_profit_target=0,
            pool_limits_enabled=True,live_pool_limit=1),[bar(BASE+i*60) for i in range(10)],INSTRUMENT)
        self.assertEqual(r['metrics']['started_accounts'],1);self.assertEqual(r['metrics']['live_pool_peak'],1)
        r=simulate(config(account_count='infinity',batch_size=20,stop_loss=1,pool_limits_enabled=True,exam_pool_limit=1),
            [bar(BASE+i*60,low=98) for i in range(10)],INSTRUMENT)
        self.assertEqual(r['metrics']['started_accounts'],1);self.assertEqual(r['accounts'][0]['status'],'exhausted')

    def test_priority_selects_live_before_exams_and_randomizes_within_group(self):
        for seed in range(20):
            chosen=choose_live_first('random_entry',random.Random(seed),list(range(20)),5,lambda x:x<3)
            self.assertEqual(set(chosen[:3]),{0,1,2});self.assertEqual(len(set(chosen)),5)
        e=Engine(':memory:',seed=False)
        try:
            ids=[e.add_account(dict(name=str(i),initial_balance=1000,profit_target=100,max_loss=300))['id'] for i in range(6)]
            for aid in ids[-2:]:e.data['accounts'][aid]['phase']='live'
            e.start_strategy(dict(account_ids=ids,batch_size=2,side='buy',symbol='NQ-PERP',quantity=1,
                order_delay_min=10,order_delay_max=10,batch_delay_min=0,batch_delay_max=0))
            e.tick(price=100)
            self.assertEqual(set(e.data['strategy']['batch_account_ids']),set(ids[-2:]))
        finally:e.close()


class ExpectancyTests(unittest.TestCase):
    def test_previous_fair_model_49_and_surviving_phase_values(self):
        c=config(initial_capital=5000,phase1_goal=500,phase2_goal=250)
        self.assertAlmostEqual(account_value(c,'1/2',5000),49.09090909)
        self.assertAlmostEqual(account_value(c,'2/2',5000),130.90909091)
        self.assertEqual(account_value(c,'live',5000),240)
        self.assertEqual(account_value(c,'live',4865),132)
        self.assertEqual(account_value(c,'live',5000,'failed'),0)

    def test_cutoff_breakdown_does_not_double_count_settlement_or_estimate(self):
        r=simulate(config(),[bar(BASE+i*60,high=106) for i in range(3)],INSTRUMENT);m=r['metrics']
        self.assertEqual(m['avg_credited_payout_per_account'],0)
        self.assertEqual(m['avg_pending_payout_per_account'],3.2)
        self.assertEqual(m['avg_remaining_expectancy_per_account'],240)
        self.assertEqual(m['avg_total_expectancy_per_account'],243.2)
        self.assertEqual(m['total_payout'],3.2);self.assertEqual(r['curve'][-1]['cash'],3.2-15)
        same=populate_metrics(r);self.assertEqual(same['metrics'],m)

    def test_full_detail_backfill_beyond_500_preserves_original_and_marks_truncation(self):
        details=[]
        r=simulate(config(account_count=600,batch_size=600,stop_loss=1),[bar(low=98),bar(BASE+60)],INSTRUMENT,
            detail_sink=lambda kind,row:details.append((kind,row)))
        legacy=copy.deepcopy(r)
        for key in list(legacy['metrics']):
            if 'expectancy' in key or key.startswith(('avg_credited','avg_pending')):legacy['metrics'].pop(key)
        self.assertIsNone(from_accounts(legacy,legacy['accounts'])['metrics']['remaining_expectancy'])
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]/'runtime') as folder:
            store=BacktestStore(Path(folder)/'test.sqlite')
            try:
                legacy['id']='b'*32;store.save_run(legacy['id'],legacy);store.save_details(legacy['id'],details)
                summary=store.batch(legacy['id'],summary_only=True)['summary']
                self.assertEqual(summary['expectancy']['complete_rounds'],1)
                self.assertAlmostEqual(summary['metrics']['remaining_expectancy']['mean'],r['metrics']['remaining_expectancy'],places=3)
                self.assertEqual(store.run(legacy['id']),legacy)
            finally:store.close()

    def test_component_means_are_round_weighted(self):
        a=simulate(config(),[bar(),bar(BASE+60)],INSTRUMENT)
        b=simulate(config(account_count=10),[bar(),bar(BASE+60)],INSTRUMENT)
        a['settlement'].update(payout_at_cutoff=21,pending_at_cutoff=8)
        b['settlement'].update(payout_at_cutoff=100,pending_at_cutoff=20)
        stats=BatchStatistics();stats.add_result(a);stats.add_result(b);m=stats.result()['metrics']
        self.assertEqual(m['avg_credited_payout_per_account']['mean'],15.5)
        self.assertEqual(m['avg_pending_payout_per_account']['mean'],5)


class EntryDayTests(unittest.TestCase):
    def test_20_window_contains_now_across_utc_midnight_dst_and_fallback(self):
        for fallback in (False,True):
            manager=patch('server.trading_day.NEW_YORK',None) if fallback else patch('server.trading_day.NEW_YORK',__import__('server.trading_day',fromlist=['NEW_YORK']).NEW_YORK)
            with manager:
                for value,hours in [('2026-03-08T23:59:59Z',23),('2026-11-02T00:59:59Z',25),('2026-11-05T00:59:59Z',24)]:
                    ts=stamp(value);w=entry_day_window(ts)
                    self.assertLessEqual(w['start'],ts);self.assertLess(ts,w['end']);self.assertEqual(w['end']-w['start'],hours*3600)
                    self.assertEqual(w['cutoff'],'20:00')

    def test_17_pnl_rollover_does_not_reopen_entry_quota(self):
        now=[stamp('2026-11-04T21:59:00Z')];e=Engine(':memory:',seed=False,clock=lambda:now[0])
        try:
            aid=e.add_account(dict(name='Day',initial_balance=1000,profit_target=100,max_loss=300))['id']
            e.place_orders(dict(account_ids=[aid],side='buy',symbol='NQ-PERP',quantity=1,idempotency_key='entry-window-one'))
            e.close_position(next(iter(e.data['positions'])))
            for t in ('2026-11-04T22:00:00Z','2026-11-05T00:59:59Z'):
                now[0]=stamp(t);s=e.state();self.assertTrue(s['accounts'][0]['daily_entry_used']);self.assertTrue(s['accounts'][0]['strategy_locked'])
                self.assertEqual(s['trading_day']['cutoff'],'17:00');self.assertEqual(s['entry_day']['cutoff'],'20:00')
            now[0]=stamp('2026-11-05T01:00:00Z');self.assertFalse(e.state()['accounts'][0]['daily_entry_used'])
        finally:e.close()

    def test_default_backtest_avoids_17_to_20_double_loss(self):
        times=[stamp(t) for t in ('2026-06-08T11:00:00Z','2026-06-08T22:00:00Z','2026-06-09T00:00:00Z')]
        bars=[bar(t,open=20000,high=20000,low=19970,close=20000) for t in times]
        r=simulate(config(initial_capital=5000,size_percent=50,leverage=50,stop_loss=135,
            daily_loss_percent=3,repeat_daily=True,phase1_goal=500),bars,INSTRUMENT)
        self.assertEqual([t['opened_at'] for t in r['trades']],[times[0],times[2]])
        self.assertTrue(all(t['reason']=='stop_loss' for t in r['trades']))
        custom=simulate({**r['config'],'strategy_reset_hour':17},bars,INSTRUMENT)
        self.assertEqual([t['opened_at'] for t in custom['trades']],[times[0],times[1]])
        self.assertEqual(custom['trades'][1]['reason'],'daily_loss')

    def test_default_20_preserves_both_saved_custom_hours_across_restart(self):
        store=BacktestStore(':memory:');saved=config(strategy_reset_hour=17,risk_reset_hour=19);store.setting('form',saved)
        service=BacktestService(store=store)
        meta=service.metadata()
        self.assertEqual(meta['defaults']['strategy_reset_hour'],20)
        self.assertEqual(meta['defaults']['risk_reset_hour'],20)
        self.assertEqual(meta['saved_form']['strategy_reset_hour'],17)
        self.assertEqual(meta['saved_form']['risk_reset_hour'],19)
        self.assertEqual(store.setting('form'),saved)
        service.executor.shutdown(wait=True)
        second=BacktestService(store=store)
        try:
            self.assertEqual(second.metadata()['saved_form']['strategy_reset_hour'],17)
            self.assertEqual(second.metadata()['saved_form']['risk_reset_hour'],19)
            self.assertEqual(store.setting('form'),saved)
        finally:second.close()
