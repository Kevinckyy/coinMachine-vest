import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from server import backtest_random as randoms
from server.backtesting import simulate,validate_config
from server.backtest_service import BacktestService
from server.market_history import BacktestStore
from test_backtesting import config,bar,BASE,INSTRUMENT


class RandomStreamTests(unittest.TestCase):
    def test_pcg64dxsm_reference_integer_stream_is_preserved_across_buffer_refills(self):
        stream=randoms.PCG64DXSMStream(42)
        self.assertEqual([stream.word() for _ in range(5)],
            [12329818062196000797,125530269004142706,12137922674892001441,6848431486601849532,3812337789277959813])
        from numpy.random import PCG64DXSM
        expected=PCG64DXSM(42).random_raw(2051).tolist()
        stream=randoms.PCG64DXSMStream(42)
        self.assertEqual([stream.word() for _ in range(2051)],expected)

    def test_integer_sampling_rejects_the_modulo_bias_tail(self):
        stream=randoms.PCG64DXSMStream(1)
        with patch.object(stream,'word',side_effect=[2**64-1,5]) as word:
            self.assertEqual(stream.below(3),2)
            self.assertEqual(word.call_count,2)

    def test_sparse_fisher_yates_sampling_handles_large_ranges_without_replacement(self):
        stream=randoms.PCG64DXSMStream(42)
        self.assertEqual(stream.sample(range(1_000_000_000),8),
            [196000797,129672975,167846839,147144053,527311029,115247593,359232753,163162252])
        self.assertEqual(sorted(stream.sample(range(20),20)),list(range(20)))
        self.assertEqual(stream.sample(range(10),0),[])
        with self.assertRaises(ValueError):stream.sample(range(3),4)

    def test_128_bit_seeds_roundtrip_json_and_sqlite_without_numeric_rounding(self):
        seed='0xffffffffffffffffffffffffffffffff'
        cfg=validate_config(config(seed=seed,rng_algorithm=randoms.ALGORITHM))
        self.assertEqual(json.loads(json.dumps(cfg))['seed'],seed)
        store=BacktestStore(':memory:')
        try:
            self.assertTrue(store.reserve_round_seed('batch',0,seed))
            self.assertEqual(store.db.execute('SELECT seed FROM bt_round_seeds').fetchone()[0],seed)
        finally:store.close()

    def test_duplicate_seed_is_redrawn_and_allocations_survive_reopening(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]/'runtime') as tmp:
            path=Path(tmp)/'cache.sqlite';store=BacktestStore(path)
            first='0x'+'1'*32;second='0x'+'f'*32
            try:
                self.assertEqual(randoms.reserve_seed(store,'batch',0,first),first)
                with patch.object(randoms,'fresh_seed',side_effect=[first,second]) as fresh:
                    self.assertEqual(randoms.reserve_seed(store,'batch',1,first),second)
                    self.assertEqual(fresh.call_count,2)
            finally:store.close()
            store=BacktestStore(path)
            try:self.assertEqual([r[0] for r in store.db.execute('SELECT seed FROM bt_round_seeds ORDER BY round_index')],[first,second])
            finally:store.close()

    def test_new_submissions_draw_fresh_seeds_and_record_the_selected_algorithm(self):
        service=BacktestService(':memory:');seeds=['0x'+'1'*32,'0x'+'e'*32]
        try:
            service.store.save_chunk(INSTRUMENT,60,[bar(),bar(BASE+60)])
            with patch.object(randoms,'fresh_seed',side_effect=seeds):
                for seed in seeds:
                    task=service.backtest(config(seed=123));service.active.result(timeout=10)
                    r=service.store.run(task['job_id'])
                    self.assertEqual(r['config']['seed'],seed)
                    self.assertEqual(r['randomness']['algorithm'],randoms.ALGORITHM)
                    self.assertEqual(r['randomness']['seed_bits'],128)
            self.assertEqual(service.metadata()['defaults']['rng_algorithm'],randoms.ALGORITHM)
        finally:service.close()

    def test_saved_round_reproduces_both_fee_modes_and_old_configs_keep_mt19937(self):
        cfg=config(seed='0x'+'a'*32,rng_algorithm=randoms.ALGORITHM,side='random',fee_bps=.25)
        bars=[bar(BASE+i*60,high=110,low=99) for i in range(5)]
        a=simulate(cfg,bars,INSTRUMENT);b=simulate(cfg,bars,INSTRUMENT)
        self.assertEqual(a['trades'],b['trades']);self.assertEqual(a['cash_curve_without_fees'],b['cash_curve_without_fees'])
        old=config();old.pop('rng_algorithm');old.pop('seed_policy')
        self.assertEqual(validate_config(old)['rng_algorithm'],randoms.LEGACY_ALGORITHM)
