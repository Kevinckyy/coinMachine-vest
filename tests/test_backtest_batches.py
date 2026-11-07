import copy
import math
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from server.backtesting import simulate,validate_config
from server.backtest_batches import MAX_PARALLEL
from server.backtest_service import BacktestService
from server.engine import ValidationError
from test_backtesting import config,bar,BASE,INSTRUMENT


class BatchTests(unittest.TestCase):
    def test_round_count_and_parallelism_validation(self):
        for value in (0,-1,1.5,True,None,2**32+1):
            with self.assertRaisesRegex(ValidationError,'测试轮数'):validate_config(config(rounds=value))
        for value in (1,10000,2**32):self.assertEqual(validate_config(config(rounds=value))['rounds'],value)
        for value in (0,MAX_PARALLEL+1,True):
            with self.assertRaisesRegex(ValidationError,'并行任务数'):validate_config(config(parallel_workers=value))

    def test_batch_seeds_are_independently_random_and_results_persist_with_unweighted_summary(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]/'runtime') as tmp:
            path=Path(tmp)/'cache.sqlite';service=BacktestService(path)
            bars=[bar(BASE+i*60,high=106) for i in range(4)]
            try:
                service.store.save_chunk(INSTRUMENT,60,bars)
                cfg=config(rounds=3,seed=2**32-1,parallel_workers=1,side='random')
                seeds=['0x123456789abcdef00123456789abcdef','0xfedcba9876543210123456789abcdef0','0x0123456789abcdeffedcba9876543210']
                with patch('server.backtest_random.fresh_seed',side_effect=seeds):
                    task=service.backtest(cfg);service.active.result(timeout=20)
                job=service.store.job(task['job_id']);self.assertEqual(job['status'],'completed')
                batch=service.store.batch(task['job_id']);self.assertEqual(batch['status'],'completed')
                self.assertEqual([r['seed'] for r in batch['rows']],seeds)
                self.assertEqual(batch['config']['seed'],seeds[0])
                actual=[]
                for row in batch['rows']:
                    run=service.store.run(row['run_id']);actual.append(run['metrics']['total_pnl'])
                    self.assertEqual(run['batch_id'],batch['id'])
                    self.assertEqual(run['config']['rounds'],1)
                    expected=simulate(run['config'],bars,INSTRUMENT)
                    self.assertEqual(run['metrics'],expected['metrics'])
                    self.assertEqual(run['time_statistics'],expected['time_statistics'])
                summary=batch['summary']['metrics']['total_pnl']
                self.assertAlmostEqual(summary['mean'],sum(actual)/3)
                self.assertAlmostEqual(summary['stddev'],math.sqrt(sum((x-sum(actual)/3)**2 for x in actual)/3))
                self.assertEqual(summary['min'],min(actual));self.assertEqual(summary['max'],max(actual))
                self.assertEqual(service.store.setting('form')['rounds'],3)
                page=service.store.batch(batch['id'],1,1);self.assertEqual(page['total'],3);self.assertEqual(page['rows'][0]['seed'],seeds[1])
            finally:service.close()
            service=BacktestService(path)
            try:self.assertEqual(service.store.batch(task['job_id'])['summary']['completed_rounds'],3)
            finally:service.close()

    @unittest.skipIf(MAX_PARALLEL<2,'Requires two CPU workers')
    def test_parallel_processes_match_serial_rounds_and_import_all_details(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]/'runtime') as tmp:
            service=BacktestService(Path(tmp)/'cache.sqlite');bars=[bar(BASE+i*60,high=106) for i in range(4)]
            try:
                service.store.save_chunk(INSTRUMENT,60,bars)
                cfg=config(rounds=4,parallel_workers=2,side='random',seed=41)
                task=service.backtest(cfg);service.active.result(timeout=40)
                self.assertEqual(service.store.job(task['job_id'])['status'],'completed',service.store.job(task['job_id']))
                batch=service.store.batch(task['job_id']);self.assertEqual(batch['summary']['parallel_workers'],2)
                self.assertEqual(batch['total'],4)
                fingerprints=set()
                for row in batch['rows']:
                    run=service.store.run(row['run_id'])
                    fingerprints.add(run['dataset']['sha256'])
                    expected=simulate(run['config'],bars,INSTRUMENT)
                    self.assertEqual(run['metrics'],expected['metrics'])
                    self.assertEqual(service.store.details(run['id'],'trades')['total'],run['metrics']['total_trades'])
                self.assertEqual(len(fingerprints),1)
                self.assertEqual(batch['summary']['time_statistics']['complete_rounds'],4)
                self.assertEqual(batch['summary']['time_statistics']['incomplete_rounds'],0)
                self.assertIn(batch['summary']['dataset_sha256'],fingerprints)
                self.assertEqual(list(Path(tmp).glob('batch-workers-*')),[],'temporary worker databases cleaned up')
            finally:service.close()

    def test_calibration_is_not_a_run_and_timing_cache_rescales_rounds(self):
        service=BacktestService(':memory:')
        try:
            service.store.save_chunk(INSTRUMENT,60,[bar(),bar(BASE+60)])
            cfg=config(rounds=9,parallel_workers=1)
            self.assertTrue(service.estimate({'config':cfg,'cached_only':True})['needs_calibration'])
            task=service.estimate({'config':cfg});service.active.result(timeout=10)
            estimate=service.store.job(task['job_id'])['detail']['estimate']
            self.assertGreater(estimate['per_round_seconds'],0)
            self.assertAlmostEqual(estimate['total_seconds'],estimate['per_round_seconds']*9)
            self.assertEqual(service.store.runs(),[]);self.assertIsNone(service.store.setting('form'))
            cached=service.estimate({'config':{**cfg,'rounds':100,'seed':123},'cached_only':True})
            self.assertIn('estimate',cached)
            self.assertAlmostEqual(cached['estimate']['total_seconds'],estimate['per_round_seconds']*100)
            service.store.save_chunk(INSTRUMENT,60,[bar(),bar(BASE+60,volume=20)])
            self.assertTrue(service.estimate({'config':cfg,'cached_only':True})['needs_calibration'])
        finally:service.close()

    @unittest.skipIf(MAX_PARALLEL<2,'Requires two CPU workers')
    def test_parallel_cancellation_stops_children_and_cleans_worker_files(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]/'runtime') as tmp:
            service=BacktestService(Path(tmp)/'cache.sqlite')
            try:
                service.store.save_chunk(INSTRUMENT,60,[bar(BASE+i*60,low=98) for i in range(2000)])
                task=service.backtest(config(rounds=20,parallel_workers=2,account_count='infinity',batch_size=50,stop_loss=1))
                deadline=time.monotonic()+5
                while not list(Path(tmp).glob('batch-workers-*')) and time.monotonic()<deadline:time.sleep(.01)
                service.cancel(task['job_id']);service.active.result(timeout=20)
                self.assertEqual(service.store.job(task['job_id'])['status'],'cancelled')
                self.assertEqual(service.store.batch(task['job_id'])['status'],'cancelled')
                self.assertEqual(list(Path(tmp).glob('batch-workers-*')),[])
            finally:service.close()

    def test_cancellation_preserves_completed_rounds(self):
        service=BacktestService(':memory:');second=threading.Event()
        service.store.save_chunk(INSTRUMENT,60,[bar(),bar(BASE+60)])
        real=simulate;calls=0
        def controlled(cfg,bars,instrument,progress,**kwargs):
            nonlocal calls
            calls+=1
            if calls==2:
                second.set();deadline=time.monotonic()+5
                while time.monotonic()<deadline:
                    progress({'completed':0,'total':2});time.sleep(.01)
                raise AssertionError('Cancellation was not delivered')
            return real(cfg,bars,instrument,progress,**kwargs)
        try:
            with patch('server.backtest_service.simulate',side_effect=controlled):
                task=service.backtest(config(rounds=4,seed=7,parallel_workers=1))
                self.assertTrue(second.wait(5));service.cancel(task['job_id']);service.active.result(timeout=10)
            self.assertEqual(service.store.job(task['job_id'])['status'],'cancelled')
            batch=service.store.batch(task['job_id']);self.assertEqual(batch['status'],'cancelled')
            self.assertEqual(batch['total'],1);self.assertEqual(batch['summary']['completed_rounds'],1)
            self.assertEqual(len(service.store.runs()),1)
        finally:service.close()
