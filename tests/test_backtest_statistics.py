import copy
import tempfile
import unittest
from pathlib import Path
from server.backtest_statistics import BatchStatistics
from server.backtesting import simulate
from server.market_history import BacktestStore
from test_backtesting import config,bar,BASE,INSTRUMENT


def sample(index,accounts,trades,held,cash,free):
    return dict(id=str(index),round_index=index,config=dict(account_count=accounts,seed=10+index,account_fee=15,fee_bps=.25),
        metrics=dict(total_trades=trades,total_accounts=accounts,net_cash=cash[-1],total_payout=cash[-1]+15*accounts),
        stages=[dict(phase='1/2',accounts=accounts,trades=trades,closed_trades=trades,total_holding_seconds=held)],
        curve=[dict(time=BASE+i*60,cash=v) for i,v in enumerate(cash)],cash_curve_without_fees=[dict(time=BASE+i*60,cash=v) for i,v in enumerate(free)])


class StatisticsTests(unittest.TestCase):
    def test_pooled_trade_statistics_use_accounts_and_closed_trades_as_weights(self):
        stats=BatchStatistics();stats.add_result(sample(0,1,2,120,[-10,100],[-10,150]));stats.add_result(sample(1,9,3,360,[0,-20],[0,200]))
        summary=stats.result();trading=summary['trading']
        self.assertEqual(trading['total_accounts'],10);self.assertEqual(trading['total_trades'],5)
        self.assertEqual(trading['avg_trades_per_account'],.5)
        self.assertEqual(trading['avg_holding_seconds'],96)
        self.assertEqual(trading['stages'][0]['avg_trades'],.5)
        self.assertEqual(trading['stages'][0]['avg_holding_seconds'],96)
        self.assertIsNone(trading['stages'][1]['avg_trades'])
        self.assertAlmostEqual(summary['metrics']['avg_trades_per_account']['mean'],(2+3/9)/2)

    def test_best_and_worst_are_whole_round_curves_and_fee_modes_rank_independently(self):
        stats=BatchStatistics();stats.add_result(sample(1,9,3,360,[0,-20],[0,200]));stats.add_result(sample(0,1,2,120,[-10,100],[-10,150]))
        curves=stats.result()['cash_curves'];paid=curves['with_fees'];free=curves['without_fees']
        self.assertEqual(paid['best']['seed'],10);self.assertEqual(paid['worst']['seed'],11)
        self.assertEqual([p['value'] for p in paid['best']['points']],[-10,100])
        self.assertEqual([p['value'] for p in paid['average']],[-5,40])
        self.assertEqual(free['best']['seed'],11);self.assertEqual(free['worst']['seed'],10)
        self.assertEqual(free['average'][-1]['value'],175)

    def test_finite_and_infinite_purchase_cost_is_charged_at_the_right_time(self):
        bars=[bar(BASE+i*60,low=98) for i in range(2)]
        finite=simulate(config(account_count=5,stop_loss=1),bars,INSTRUMENT)
        unlimited=simulate(config(account_count='infinity',stop_loss=1),bars,INSTRUMENT)
        self.assertTrue(all(p['cost']==75 for p in finite['curve']))
        self.assertEqual([p['cost'] for p in unlimited['curve']],[15,30,30])
        self.assertEqual(unlimited['curve'][-1]['cash'],unlimited['metrics']['net_cash'])

    def test_without_fee_curve_recomputes_evaluation_and_payout_path(self):
        bars=[bar(BASE+i*60,high=106) for i in range(3)]
        cfg=config(fee_bps=50,payout_delay_hours=0)
        result=simulate(cfg,bars,INSTRUMENT);free=simulate({**cfg,'fee_bps':0},bars,INSTRUMENT)
        self.assertEqual(result['cash_curve_without_fees'],free['curve'])
        self.assertEqual(result['metrics']['total_payout'],0)
        self.assertGreater(result['cash_curve_without_fees'][-1]['cash'],result['curve'][-1]['cash'])
        self.assertEqual(free['cash_curve_without_fees'],free['curve'])

    def test_legacy_single_statistics_upgrade_preserves_original_and_flags_missing_comparison(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]/'runtime') as tmp:
            store=BacktestStore(Path(tmp)/'cache.sqlite')
            try:
                result=simulate(config(account_count='infinity',stop_loss=1,fee_bps=.25),[bar(BASE+i*60,low=98) for i in range(3)],INSTRUMENT)
                result['id']='a'*32;result.pop('cash_curve_without_fees')
                for point in result['curve']:point.pop('cash');point.pop('cost')
                original=copy.deepcopy(result);store.save_run(result['id'],result)
                record=store.batch(result['id'],summary_only=True)
                self.assertEqual(record['rows'],[]);self.assertEqual(record['summary']['completed_rounds'],1)
                self.assertTrue(record['summary']['cash_curves']['with_fees']['available'])
                self.assertFalse(record['summary']['cash_curves']['without_fees']['available'])
                self.assertEqual(record['summary']['cash_curves']['with_fees']['average'][-1]['value'],result['metrics']['net_cash'])
                self.assertEqual(store.run(result['id']),original)
            finally:store.close()
