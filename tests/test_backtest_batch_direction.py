import unittest
from collections import defaultdict
from server.backtesting import simulate
from tests.test_backtesting import config,bar,BASE,INSTRUMENT


class BacktestBatchDirectionTests(unittest.TestCase):
    def test_staggered_entries_share_direction_in_each_batch(self):
        for seed in (3,17,101):
            with self.subTest(seed=seed):
                result=simulate(config(account_count=9,batch_size=3,seed=seed,side='random',stop_loss=5,
                    phase1_goal=10000,phase2_goal=10000,order_delay_min=60,order_delay_max=60),
                    [bar(BASE+i*60,high=107,low=93) for i in range(30)],INSTRUMENT)
                groups=defaultdict(list)
                for trade in result['trades']:groups[trade['batch_number']].append(trade)
                self.assertEqual(len(groups),3)
                for trades in groups.values():
                    self.assertEqual(len(trades),3)
                    self.assertEqual(len({t['side'] for t in trades}),1)
                    self.assertEqual(len({t['opened_at'] for t in trades}),3)
                self.assertEqual(result['strategy_version'],3)
                self.assertEqual(result['simulation_version'],10)

    def test_fixed_direction_is_preserved_across_batches(self):
        for side in ('buy','sell'):
            result=simulate(config(account_count=6,batch_size=3,side=side,stop_loss=5),
                [bar(BASE+i*60,high=107,low=93) for i in range(8)],INSTRUMENT)
            self.assertEqual(len(result['trades']),6)
            self.assertEqual({t['side'] for t in result['trades']},{side})
