import copy
import tempfile
import unittest
from pathlib import Path

from server.backtesting import simulate
from server.backtest_statistics import CurveSummary
from server.market_history import BacktestStore
from test_backtesting import BASE,INSTRUMENT,bar,config


class SettlementTests(unittest.TestCase):
    def test_final_close_claim_settles_at_its_real_credit_time_and_freezes_trading(self):
        r=simulate(config(),[bar(BASE+i*60,high=106) for i in range(3)],INSTRUMENT)
        end=BASE+180;s=r['settlement'];m=r['metrics']
        self.assertEqual(s['trading_end'],end)
        self.assertEqual(s['settlement_end'],end+86400)
        self.assertEqual(m['pending_payout_at_cutoff'],3.2)
        self.assertEqual(m['total_payout'],3.2)
        self.assertEqual(m['pending_payout'],0)
        cutoff=next(p for p in r['curve'] if p['time']==end)
        self.assertEqual(cutoff['payout'],0)
        self.assertEqual(r['curve'][-1]['time'],end+86400)
        self.assertEqual(r['curve'][-1]['cash'],m['net_cash'])
        self.assertEqual(m['total_accounts'],1)
        self.assertEqual(m['total_trades'],3)
        self.assertTrue(all(t['closed_at']<=end for t in r['trades']))
        self.assertTrue(all(c['status']=='completed' for c in r['claims']))
        self.assertEqual(r['accounts'][0]['pending_payout'],0)
        self.assertEqual(r['curve'][-1]['pnl'],cutoff['pnl'])
        self.assertEqual(r['curve'][-1]['cost'],cutoff['cost'])

    def test_repeated_claims_do_not_count_one_account_as_multiple_paid_accounts(self):
        r=simulate(config(initial_phase='live',repeat_daily=True,payout_delay_hours=48),
            [bar(BASE+i*86400,high=103) for i in range(3)],INSTRUMENT)
        self.assertEqual(r['metrics']['total_trades'],3)
        self.assertEqual(r['metrics']['total_payout'],9.6)
        self.assertEqual(r['metrics']['settled_after_cutoff'],6.4)
        self.assertEqual(r['metrics']['paid_accounts'],1)
        self.assertEqual(r['metrics']['payout_rate'],100)
        self.assertEqual(r['settlement']['paid_accounts_at_cutoff'],1)
        self.assertEqual(r['curve'][-1]['time'],BASE+4*86400+60)

    def test_zero_delay_and_no_claims_do_not_extend_the_trading_horizon(self):
        for cfg,bars in [(config(payout_delay_hours=0),[bar(BASE+i*60,high=106) for i in range(3)]),
                         (config(),[bar(),bar(BASE+60)])]:
            r=simulate(cfg,bars,INSTRUMENT)
            self.assertEqual(r['settlement']['settlement_end'],r['settlement']['trading_end'])
            self.assertEqual(r['metrics']['settled_after_cutoff'],0)
            times=[p['time'] for p in r['curve']]
            self.assertTrue(all(a<b for a,b in zip(times,times[1:])))

    def test_fees_only_reduce_evaluation_and_live_balances_and_are_not_deducted_from_payout_again(self):
        r=simulate(config(fee_bps=25),[bar(BASE+i*60,high=120) for i in range(3)],INSTRUMENT)
        trades=r['trades'];self.assertEqual([t['phase'] for t in trades],['1/2','2/2','live'])
        for t in trades:self.assertAlmostEqual(t['pnl'],t['gross_pnl']-t['fees'])
        live_profit=trades[-1]['pnl'];payout=round(live_profit*.8,8)
        self.assertGreater(r['metrics']['total_fees'],0)
        self.assertEqual(r['claims'][0]['gross_profit'],live_profit)
        self.assertEqual(r['claims'][0]['amount'],payout)
        self.assertAlmostEqual(r['metrics']['total_payout'],payout,places=4)
        self.assertEqual(r['curve'][-1]['cash'],round(payout-15,4))
        self.assertEqual(r['accounts'][0]['balance'],1000)

    def test_rounds_with_different_settlement_dates_share_a_stepwise_mean_axis(self):
        stats=CurveSummary()
        for i,points in enumerate([[(0,-15),(10,-15),(20,85)],[(0,-15),(10,-15),(30,185)]]):
            stats.add(dict(config=dict(seed=i),round_index=i),[dict(time=BASE+t,value=v) for t,v in points])
        s=stats.result(2);self.assertTrue(s['available'])
        self.assertEqual([p['value'] for p in s['average']],[-15,-15,35,135])
        self.assertEqual([p['value'] for p in s['best']['points']],[-15,-15,-15,185])
        self.assertEqual([p['value'] for p in s['worst']['points']],[-15,-15,85,85])
        self.assertEqual(s['average'][-1]['time'],BASE+30)

    def test_archived_full_claims_upgrade_statistics_and_preserve_original_records(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]/'runtime') as tmp:
            store=BacktestStore(Path(tmp)/'cache.sqlite');end=BASE+120
            claims=[dict(id=i+1,account_id=aid,credit_at=end+dt,amount=amount,status='pending' if dt>0 else 'completed')
                for i,(aid,dt,amount) in enumerate([(1,-60,200),(3,-30,100),(1,60,100),(2,120,100),(4,120,100)])]
            original=dict(id='legacy',config=config(account_count=4,fee_bps=.25),simulation_version=4,
                dataset=dict(last_time=end-60),metrics=dict(total_trades=0,total_accounts=4,total_payout=300,paid_accounts=2,
                payout_rate=50,pending_payout=300,account_purchase_cost=60,net_cash=240,avg_payout_per_account=75,total_pnl=0),
                stages=[],curve=[dict(time=BASE,payout=0,cash=-60,cost=60),dict(time=end,payout=300,cash=240,cost=60)],
                claims=claims[:1],detail_counts=dict(claims=5))
            try:
                store.save_run('legacy',copy.deepcopy(original));store.save_details('legacy',[('claims',c) for c in claims])
                s=store.batch('legacy',summary_only=True)['summary']
                self.assertEqual(s['metrics']['total_payout']['mean'],600)
                self.assertEqual(s['metrics']['paid_accounts']['mean'],4)
                self.assertEqual(s['metrics']['payout_rate']['mean'],100)
                self.assertEqual(s['metrics']['net_cash']['mean'],540)
                self.assertEqual(s['metrics']['settled_after_cutoff']['mean'],300)
                self.assertEqual(s['settlement']['settlement_end'],end+120)
                self.assertFalse(s['cash_curves']['without_fees']['available'])
                self.assertEqual(s['cash_curves']['with_fees']['average'][-1]['value'],540)
                self.assertEqual(store.run('legacy'),original)
                self.assertEqual(store.details('legacy','claims')['rows'][0]['status'],'pending')
            finally:store.close()

    def test_incomplete_archived_claims_are_flagged_instead_of_guessing_payout_accounts(self):
        from server.backtest_settlement import settle_archived
        r=dict(config=config(),metrics=dict(pending_payout=100),curve=[dict(time=BASE,payout=0,cash=-15)],detail_counts=dict(claims=2))
        result=settle_archived(r,[])
        self.assertEqual(result['metrics']['pending_payout'],100)
        self.assertEqual(result['settlement']['policy'],'trading_cutoff_only')
