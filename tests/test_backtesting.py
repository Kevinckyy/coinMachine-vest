import copy
import io
import json
import tempfile
import time
import unittest
import urllib.parse
import http.client
import threading
from pathlib import Path
from datetime import datetime,timezone

from server.backtesting import DEFAULT_CONFIG,simulate,ny_day,validate_config
from server.backtest_service import BacktestService
from server.engine import Engine,ValidationError
from server.market_history import BacktestStore,ReadOnlyHistoryBridge,INSTRUMENTS,sync_history,validated_bar,NoRedirect

BASE=1793880000
INSTRUMENT=dict(root='MNQ',symbol='MNQ 12-26',point_value=2,tick_size=.25)


def bar(t=BASE,**changes):
    return {**dict(time=t,open=100,high=100,low=100,close=100,volume=10),**changes}


def config(**changes):
    return {**DEFAULT_CONFIG,'root':'MNQ','symbol':'MNQ 12-26','from':BASE,'to':BASE+30*86400,'interval':60,
        'account_count':1,'batch_size':1,'initial_capital':1000,'max_drawdown':300,'daily_loss_percent':0,
        'leverage':1,'size_percent':25,'side':'buy','stop_loss':10000,'phase1_goal':10,'phase2_goal':6,
        'live_profit_target':4,'fee_bps':0,'slippage_ticks':0,'order_delay_min':0,'order_delay_max':0,
        'batch_delay_min':0,'batch_delay_max':0,'repeat_daily':False,**changes}


class SimulationTests(unittest.TestCase):
    def test_unlimited_pool_replenishes_until_the_last_bar(self):
        bars=[bar(BASE+i*60,low=98) for i in range(12)]
        finite=simulate(config(account_count=2,stop_loss=1),bars,INSTRUMENT)
        unlimited=simulate(config(account_count='∞',stop_loss=1),bars,INSTRUMENT)
        self.assertEqual(finite['metrics']['total_trades'],2)
        self.assertEqual(unlimited['config']['account_count'],'infinity')
        self.assertEqual(unlimited['metrics']['total_accounts'],12)
        self.assertEqual(unlimited['metrics']['total_trades'],12)
        self.assertEqual(unlimited['trades'][-1]['closed_at'],bars[-1]['time']+60)
        self.assertEqual(unlimited['metrics']['account_purchase_cost'],12*15)
        self.assertEqual(unlimited['stages'][0]['accounts'],12)
        self.assertEqual(unlimited['stages'][0]['avg_trades'],1)

    def test_ten_thousand_pool_uses_lazy_accounts_and_exceeds_old_work_limit(self):
        r=simulate(config(account_count=10000,initial_phase='live',live_profit_target=0),[bar(BASE+i*60) for i in range(2001)],INSTRUMENT)
        self.assertEqual(r['metrics']['total_accounts'],10000)
        self.assertEqual(r['metrics']['started_accounts'],1)
        self.assertEqual(r['metrics']['unstarted_accounts'],9999)
        self.assertEqual(r['metrics']['total_trades'],1)
        self.assertEqual(len(r['accounts']),1)
        self.assertEqual(r['stages'][2]['accounts'],10000)
        self.assertEqual(r['metrics']['funded_accounts'],10000)

    def test_large_trade_stream_keeps_previews_bounded_and_totals_complete(self):
        r=simulate(config(account_count='infinity',batch_size=1000,stop_loss=1),[bar(BASE+i*60,low=98) for i in range(101)],INSTRUMENT)
        self.assertEqual(r['metrics']['total_trades'],101000)
        self.assertEqual(r['metrics']['total_accounts'],101000)
        self.assertEqual(r['detail_counts']['trades'],101000)
        self.assertEqual(len(r['trades']),500)
        self.assertEqual(len(r['accounts']),500)
        self.assertEqual(r['metrics']['total_pnl'],-101000)

    def test_sparse_unused_account_pool_never_reuses_a_removed_id(self):
        from server.backtest_runtime import UnusedAccounts
        pool=UnusedAccounts(10000);used=set()
        import random
        rng=random.Random(42)
        while pool.remaining:
            indices=rng.sample(range(pool.remaining),min(7,pool.remaining))
            ids={pool.value(i) for i in indices}
            self.assertTrue(used.isdisjoint(ids));used.update(ids);pool.discard(indices)
        self.assertEqual(len(used),10000)

    def test_unlimited_payout_denominator_and_credit_survive_account_retirement(self):
        r=simulate(config(account_count='infinity'),[bar(BASE+i*60,high=106) for i in range(3)]+[bar(BASE+25*3600,high=106)],INSTRUMENT)
        self.assertEqual(r['metrics']['total_accounts'],2)
        self.assertEqual(r['metrics']['exam_pass_rate'],50)
        self.assertEqual(r['metrics']['payout_rate'],50)
        self.assertEqual(r['metrics']['total_payout'],3.2)
        self.assertEqual(r['metrics']['avg_payout_per_account'],1.6)
        self.assertEqual(r['claims'][0]['status'],'completed')

    def test_account_count_rejects_invalid_values_with_actionable_range(self):
        for value in (None,True,'50',0,-1,1000001,1.5,float('nan'),float('inf')):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValidationError,'模拟账户数必须为 1–1000000 之间的整数'):
                    validate_config(config(account_count=value))
        for value in (1,50,500,10000,1000000):
            self.assertEqual(validate_config(config(account_count=value))['account_count'],value)

    def test_two_stage_reset_and_24_hour_profit_share_credit(self):
        bars=[bar(BASE+i*60,high=106,close=106) for i in range(3)]+[bar(BASE+25*3600)]
        r=simulate(config(),bars,INSTRUMENT)
        self.assertEqual([t['phase'] for t in r['trades']],['1/2','2/2','live'])
        self.assertEqual([t['pnl'] for t in r['trades']],[10,6,4])
        self.assertEqual(r['metrics']['exam_pass_rate'],100)
        self.assertEqual(r['metrics']['payout_rate'],100)
        self.assertEqual(r['metrics']['total_payout'],3.2)
        self.assertEqual(r['accounts'][0]['balance'],1000)
        self.assertEqual(r['metrics']['total_pnl'],20)
        for s in r['stages']:
            self.assertEqual(s['avg_trades'],1)
            self.assertEqual(s['avg_holding_seconds'],60)

    def test_claim_after_trading_cutoff_is_settled_without_more_trading(self):
        r=simulate(config(),[bar(BASE+i*60,high=106,close=106) for i in range(3)],INSTRUMENT)
        self.assertEqual(r['metrics']['passed_accounts'],1)
        self.assertEqual(r['metrics']['total_payout'],3.2)
        self.assertEqual(r['metrics']['payout_rate'],100)
        self.assertEqual(r['metrics']['pending_payout'],0)
        self.assertEqual(r['metrics']['settled_after_cutoff'],3.2)
        self.assertEqual(r['settlement']['settlement_end'],r['settlement']['trading_end']+86400)
        self.assertEqual(len(r['trades']),3)

    def test_bps_slippage_and_tick_rounding_reserve_enough_net_profit_to_pass(self):
        bars=[bar(BASE+i*60,open=20000,high=20020,low=20000,close=20001) for i in range(3)]
        r=simulate(config(initial_capital=5000,leverage=50,fee_bps=.25,slippage_mode='bps',slippage_bps=.1),bars,INSTRUMENT)
        self.assertTrue(r['stages'][0]['passed'])
        self.assertGreaterEqual(r['trades'][0]['pnl'],10)
        self.assertEqual(r['metrics']['exam_pass_rate'],100)

    def test_fees_and_adverse_slippage_apply_on_both_sides(self):
        r=simulate(config(initial_phase='live',live_profit_target=0,fee_bps=.25,slippage_ticks=1),[bar(),bar(BASE+60)],INSTRUMENT)
        t=r['trades'][0]
        self.assertEqual(t['entry_price'],100.25)
        self.assertEqual(t['exit_price'],99.75)
        self.assertAlmostEqual(t['fees'],.01)
        self.assertAlmostEqual(t['pnl'],-1.01)
        self.assertEqual(r['metrics']['exam_pass_rate'],None)

    def test_ambiguous_ohlc_path_uses_stop_before_profit_target(self):
        r=simulate(config(stop_loss=5),[bar(high=110,low=90),bar(BASE+60)],INSTRUMENT)
        self.assertEqual(r['trades'][0]['reason'],'stop_loss')
        self.assertEqual(r['trades'][0]['pnl'],-5)
        self.assertEqual(r['metrics']['exam_pass_rate'],0)

    def test_gap_through_stop_uses_worse_open_and_never_the_old_stop_price(self):
        r=simulate(config(stop_loss=5),[bar(),bar(BASE+60,open=90,high=91,low=89,close=90)],INSTRUMENT)
        self.assertEqual(r['trades'][0]['exit_price'],90)
        self.assertEqual(r['trades'][0]['pnl'],-20)

    def test_known_opening_gap_profit_precedes_a_later_adverse_extreme(self):
        r=simulate(config(stop_loss=5),[bar(),bar(BASE+60,open=110,high=111,low=90,close=100)],INSTRUMENT)
        self.assertEqual(r['trades'][0]['reason'],'phase_target')
        self.assertEqual(r['trades'][0]['exit_price'],110)
        self.assertEqual(r['trades'][0]['closed_at'],BASE+60)

    def test_static_drawdown_and_daily_loss_equality_do_not_breach(self):
        for changes in [dict(max_drawdown=100),dict(max_drawdown=300,daily_loss_percent=10)]:
            safe=simulate(config(initial_phase='live',live_profit_target=0,**changes),[bar(low=50),bar(BASE+60)],INSTRUMENT)
            self.assertEqual(safe['metrics']['failed_accounts'],0)
            failed=simulate(config(initial_phase='live',live_profit_target=0,**changes),[bar(low=49.75),bar(BASE+60)],INSTRUMENT)
            self.assertEqual(failed['metrics']['failed_accounts'],1)
            self.assertIn(failed['trades'][0]['reason'],('max_drawdown','daily_loss'))

    def test_low_drawdown_stop_rule_is_rechecked_with_strict_boundary(self):
        for remaining,reason in [(199,'end_of_data'),(200,'stop_loss')]:
            r=simulate(config(initial_phase='live',live_profit_target=0,max_drawdown=remaining,stop_loss=5,
                skip_stop_loss_on_low_drawdown=True,drawdown_stop_loss_threshold=200),
                [bar(low=96,close=96),bar(BASE+60,open=96,high=96,low=96,close=96)],INSTRUMENT)
            self.assertEqual(r['trades'][0]['reason'],reason)
            self.assertEqual(r['trades'][0]['stop_loss'],None if remaining<200 else 5)

    def test_risk_20_et_and_strategy_17_et_are_independent_in_winter_and_summer(self):
        t=datetime(2026,11,6,0,59,tzinfo=timezone.utc).timestamp()
        self.assertEqual(ny_day(t,20),'2026-11-04')
        self.assertEqual(ny_day(t,17),'2026-11-05')
        self.assertEqual(ny_day(t+60,20),'2026-11-05')
        t=datetime(2026,7,7,23,59,tzinfo=timezone.utc).timestamp()
        self.assertEqual(ny_day(t,20),'2026-07-06')
        self.assertEqual(ny_day(t+60,20),'2026-07-07')
        self.assertEqual(ny_day(datetime(2026,3,8,4,30,tzinfo=timezone.utc).timestamp(),0),'2026-03-07')
        self.assertEqual(ny_day(datetime(2026,11,1,4,30,tzinfo=timezone.utc).timestamp(),0),'2026-11-01')

    def test_optional_hourly_funding_includes_elapsed_time_across_candle_gaps(self):
        r=simulate(config(initial_phase='live',live_profit_target=0,funding_bps_hourly=1),[bar(),bar(BASE+86400)],INSTRUMENT)
        self.assertAlmostEqual(r['metrics']['total_funding'],200*.0001*(86460/3600),places=4)

    def test_daily_repeat_and_single_run_have_different_explicit_quotas(self):
        bars=[bar(),bar(BASE+60),bar(BASE+86400),bar(BASE+86460)]
        single=simulate(config(initial_phase='live',live_profit_target=0,stop_loss=1),
            [{**x,'low':99} for x in bars],INSTRUMENT)
        repeated=simulate(config(initial_phase='live',live_profit_target=0,stop_loss=1,repeat_daily=True),
            [{**x,'low':99} for x in bars],INSTRUMENT)
        self.assertEqual(len(single['trades']),1)
        self.assertEqual(len(repeated['trades']),2)

    def test_same_seed_data_and_parameters_reproduce_all_trades(self):
        c=config(account_count=5,batch_size=2,side='random',stop_loss=5)
        bars=[bar(BASE+i*60,high=106,low=94,close=101) for i in range(10)]
        first=simulate(c,bars,INSTRUMENT);second=simulate(c,bars,INSTRUMENT)
        self.assertEqual(first,second)
        self.assertEqual(first['dataset']['bars'],10)
        self.assertEqual(len(first['dataset']['sha256']),64)

    def test_insufficient_integer_micro_margin_is_explicit_and_does_not_fake_fills(self):
        c=config(root='MGC',symbol='MGC 12-26',initial_capital=5000,leverage=25)
        bars=[bar(open=5000,high=5000,low=5000,close=5000),bar(BASE+60,open=5000,high=5000,low=5000,close=5000)]
        r=simulate(c,bars,dict(root='MGC',symbol='MGC 12-26',point_value=10,tick_size=.1))
        self.assertEqual(r['metrics']['total_trades'],0)
        self.assertEqual(r['metrics']['skipped_insufficient_margin'],1)
        self.assertIsNone(r['metrics']['win_rate'])

    def test_invalid_strategy_and_unordered_candles_are_rejected(self):
        for value in ['future-unimplemented',{},None]:
            with self.assertRaises(ValidationError):validate_config(config(strategy_id=value))
        with self.assertRaises(ValidationError):simulate(config(),[bar(BASE+60),bar()],INSTRUMENT)


class Response(io.BytesIO):
    def __init__(self,url,data):super().__init__(json.dumps(data).encode());self.url=url
    def geturl(self):return self.url


class FakeOpener:
    def __init__(self):self.requests=[]
    def open(self,request,timeout):
        self.requests.append(request);url=urllib.parse.urlparse(request.full_url);q=urllib.parse.parse_qs(url.query)
        if url.path=='/api/status':data={'connected':True,'historyWindowVersion':1}
        elif url.path=='/api/symbols':
            data={'currentOnly':True,'symbols':[dict(symbol=root+' 12-26',tickSize=s['tick_size'],pointValue=s['point_value']) for root,s in INSTRUMENTS.items()]+[dict(symbol='MNQ',tickSize=.01,pointValue=1)]}
        elif url.path=='/api/resolve':
            symbol=q['symbol'][0];root=symbol.split()[0];s=INSTRUMENTS[root]
            data=dict(symbol=symbol,tickSize=s['tick_size'],pointValue=s['point_value'])
        elif url.path=='/api/history':
            start=int(q['from'][0]);end=int(q['to'][0]);data={'bars':[bar(t) for t in range(start,end+1,60)]}
        else:raise AssertionError('Unexpected upstream path '+url.path)
        return Response(request.full_url,data)


class HistoryTests(unittest.TestCase):
    def test_only_allowlisted_gets_are_possible_and_native_metadata_is_preserved(self):
        opener=FakeOpener();bridge=ReadOnlyHistoryBridge(opener)
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]/'runtime') as tmp:
            path=Path(tmp)/'backtest.sqlite';store=BacktestStore(path)
            cfg={'roots':list(INSTRUMENTS),'from':BASE,'to':BASE+120,'interval':60}
            result=sync_history(store,bridge,cfg,clock=lambda:BASE+3600)
            self.assertEqual(result['status'],'completed')
            self.assertEqual(len(store.inventory()),4)
            self.assertEqual([x['bars'] for x in store.inventory()],[3]*4)
            sync_history(store,bridge,cfg,clock=lambda:BASE+3600)
            self.assertEqual([x['bars'] for x in store.inventory()],[3]*4)
            store.close();store=BacktestStore(path)
            self.assertEqual(store.instrument('MNQ 12-26')['point_value'],2)
            store.close()
        self.assertTrue(all(r.get_method()=='GET' for r in opener.requests))
        self.assertTrue(all(urllib.parse.urlparse(r.full_url).netloc=='127.0.0.1:8090' for r in opener.requests))
        for forbidden in ['/api/order/place','/api/order/cancel','/api/position/close','/api/accounts','https://evil.example/api/history']:
            before=len(opener.requests)
            with self.assertRaises(ValidationError):bridge._get(forbidden)
            self.assertEqual(len(opener.requests),before)
        with self.assertRaises(ValidationError):bridge.resolve('MNQ','MNQ')

    def test_redirects_and_invalid_candles_are_never_accepted(self):
        with self.assertRaises(ValidationError):NoRedirect().redirect_request(None,None,302,'',{},'http://127.0.0.1:8090/api/order/place')
        for raw in [bar(low=101),bar(high=99),bar(close=float('nan')),bar(volume=-1),bar(time=BASE-1)]:
            with self.assertRaises(ValidationError):validated_bar(raw,BASE,BASE+60,60,BASE+3600)
        self.assertIsNone(validated_bar(bar(),BASE,BASE+60,60,BASE+30))

    def test_empty_history_is_an_error_not_a_synthetic_candle_series(self):
        class Empty(ReadOnlyHistoryBridge):
            def history(self,*args):return []
        store=BacktestStore(':memory:')
        try:
            r=sync_history(store,Empty(FakeOpener()),{'roots':['MNQ'],'interval':60,'from':BASE,'to':BASE+120},clock=lambda:BASE+3600)
            self.assertEqual(r['status'],'error')
            self.assertFalse(store.bars('MNQ 12-26',60,BASE,BASE+120))
        finally:store.close()

    def test_reading_a_cache_does_not_mark_another_import_as_interrupted(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]/'runtime') as tmp:
            path=Path(tmp)/'cache.sqlite';a=BacktestStore(path);a.save_job('x','sync','running',{})
            b=BacktestStore(path);self.assertEqual(b.job('x')['status'],'running');b.close();a.close()


class ServiceTests(unittest.TestCase):
    def test_streamed_details_are_complete_paginated_and_persisted(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]/'runtime') as tmp:
            path=Path(tmp)/'cache.sqlite';service=BacktestService(path)
            try:
                service.store.save_chunk(INSTRUMENT,60,[bar(BASE+i*60,low=98) for i in range(601)])
                task=service.backtest(config(account_count='infinity',stop_loss=1));service.active.result(timeout=30)
                result=service.store.run(task['job_id'])
                self.assertEqual(result['metrics']['total_accounts'],601)
                self.assertEqual(len(result['trades']),500)
                first=service.store.details(task['job_id'],'trades',0,100)
                last=service.store.details(task['job_id'],'trades',600,100)
                self.assertEqual(first['total'],601);self.assertEqual(len(first['rows']),100)
                self.assertEqual(last['rows'][0]['id'],1)
                self.assertEqual(service.store.details(task['job_id'],'trades',0,100,'live')['total'],0)
            finally:service.close()
            service=BacktestService(path)
            try:
                rows=service.store.details(task['job_id'],'accounts',500,100)
                self.assertEqual(rows['total'],601);self.assertEqual(len(rows['rows']),100)
                self.assertEqual(service.store.setting('form')['account_count'],'infinity')
            finally:service.close()

    def test_invalid_account_count_does_not_save_parameters_or_create_job(self):
        service=BacktestService(':memory:')
        try:
            service.store.setting('form',config(account_count=50))
            with self.assertRaisesRegex(ValidationError,'模拟账户数'):
                service.backtest(config(account_count=1000001))
            self.assertEqual(service.store.setting('form')['account_count'],50)
            self.assertEqual(service.store.jobs(),[])
        finally:service.close()

    def test_busy_service_does_not_overwrite_last_accepted_parameters(self):
        from concurrent.futures import Future
        service=BacktestService(':memory:')
        try:
            service.store.save_chunk(INSTRUMENT,60,[bar(),bar(BASE+60)])
            service.store.setting('form',config(seed=7))
            service.active=Future()
            with self.assertRaises(ValidationError):service.backtest(config(seed=8))
            self.assertEqual(service.store.setting('form')['seed'],7)
            service.active.set_result(None)
        finally:service.close()

    def test_offline_backtest_persistence_and_results_do_not_touch_live_engine(self):
        engine=Engine(':memory:',seed=False)
        before=copy.deepcopy(engine.data)
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]/'runtime') as tmp:
            path=Path(tmp)/'cache.sqlite';service=BacktestService(path,bridge=ReadOnlyHistoryBridge(FakeOpener()))
            try:
                bars=[bar(BASE+i*60,high=106,close=106) for i in range(3)]
                service.store.save_chunk(INSTRUMENT,60,bars)
                task=service.backtest(config());service.active.result(timeout=10)
                job=service.store.job(task['job_id']);self.assertEqual(job['status'],'completed')
                saved=service.store.run(task['job_id']);self.assertEqual(saved['metrics']['exam_pass_rate'],100)
                self.assertEqual(service.store.setting('form')['strategy_id'],'random_entry')
            finally:service.close()
            service=BacktestService(path)
            try:self.assertEqual(service.store.run(task['job_id'])['dataset']['sha256'],saved['dataset']['sha256'])
            finally:service.close()
        self.assertEqual(engine.data,before)
        engine.close()

    def test_current_profile_defaults_use_verified_5k_rules_and_quote_storage(self):
        service=BacktestService(':memory:')
        try:
            state=dict(accounts=[dict(auto_discovered=True,capital_metrics={'drawdown':{'limit':300},'daily_loss':{'limit':150}})])
            meta=service.metadata(state)
            self.assertEqual(meta['defaults']['initial_capital'],5000)
            self.assertEqual(meta['defaults']['risk_reset_hour'],20)
            self.assertEqual(meta['defaults']['strategy_reset_hour'],17)
            self.assertEqual(meta['defaults']['profit_split_percent'],80)
            q=service.observe_costs(dict(symbol='NQ-PERP',fee_bps=.25,slippage_bps=1.2,observed_at=time.time(),notional=50000,source_text='Fee / Slippage'))
            self.assertEqual(q['quote']['source'],'vest-order-form')
            self.assertEqual(service.metadata()['execution_quotes']['NQ-PERP']['slippage_bps'],1.2)
            with self.assertRaises(ValidationError):service.observe_costs(dict(symbol='BTC-PERP',fee_bps=1,observed_at=time.time()))
        finally:service.close()


class BacktestHTTPTests(unittest.TestCase):
    def setUp(self):
        from server.app import create_server
        self.engine=Engine(':memory:',seed=False)
        self.service=BacktestService(':memory:',bridge=ReadOnlyHistoryBridge(FakeOpener()))
        self.server=create_server(self.engine,port=0,backtests=self.service)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()

    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join(2);self.engine.close()

    def request(self,method,path,body=None,extra=None):
        connection=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=5)
        headers={'Content-Type':'application/json',**(extra or {})}
        connection.request(method,path,body=json.dumps(body) if body is not None else None,headers=headers)
        r=connection.getresponse();status=r.status;payload=json.loads(r.read());connection.close();return status,payload

    def test_import_run_reopen_and_result_endpoints_are_offline_and_isolated(self):
        before=copy.deepcopy(self.engine.data)
        status,meta=self.request('GET','/api/backtests/meta')
        self.assertEqual(status,200);self.assertEqual(meta['strategies'][0]['id'],'random_entry')
        status,task=self.request('POST','/api/backtests/sync',{'roots':list(INSTRUMENTS),'interval':60,'from':BASE,'to':BASE+120})
        self.assertEqual(status,200);self.service.active.result(timeout=5)
        self.assertEqual(self.request('GET','/api/backtests/jobs/'+task['job_id'])[1]['status'],'completed')
        self.assertEqual(len(self.request('GET','/api/backtests/meta')[1]['inventory']),4)
        status,task=self.request('POST','/api/backtests/run',config())
        self.assertEqual(status,200);self.service.active.result(timeout=5)
        result=self.request('GET','/api/backtests/runs/'+task['job_id'])[1]
        self.assertEqual(result['config']['strategy_id'],'random_entry')
        self.assertEqual(result['dataset']['bars'],3)
        self.assertEqual(len(self.request('GET','/api/backtests/runs')[1]['runs']),1)
        status,page=self.request('GET','/api/backtests/runs/'+task['job_id']+'/details?kind=trades&limit=1')
        self.assertEqual(status,200);self.assertEqual(page['total'],result['metrics']['total_trades']);self.assertEqual(len(page['rows']),1)
        self.assertEqual(self.request('GET','/api/backtests/runs/'+task['job_id']+'/details?offset=invalid')[0],400)
        self.assertEqual(self.engine.data,before)

    def test_batch_and_timing_http_are_local_offline_and_return_saved_rounds(self):
        self.service.store.save_chunk(INSTRUMENT,60,[bar(),bar(BASE+60)])
        before=copy.deepcopy(self.engine.data)
        status,task=self.request('POST','/api/backtests/run',config(rounds=3,parallel_workers=1,seed=99))
        self.assertEqual(status,200);self.assertEqual(task['kind'],'batch');self.service.active.result(timeout=10)
        status,batch=self.request('GET','/api/backtests/batches/'+task['job_id'])
        self.assertEqual(status,200)
        seeds=[r['seed'] for r in batch['rows']];self.assertEqual(len(set(seeds)),3)
        for seed in seeds:self.assertRegex(seed,r'^0x[0-9a-f]{32}$')
        self.assertEqual(batch['config']['seed'],seeds[0])
        self.assertEqual(self.request('GET','/api/backtests/batches/'+task['job_id']+'?offset=invalid')[0],400)
        self.assertEqual(self.request('POST','/api/backtests/jobs/'+task['job_id']+'/cancel',{})[0],409)
        status,estimate=self.request('POST','/api/backtests/estimate',{'config':config(rounds=10,parallel_workers=1),'cached_only':True})
        self.assertEqual(status,200);self.assertGreater(estimate['estimate']['total_seconds'],0)
        self.assertEqual(self.engine.data,before)

    def test_extension_can_report_paired_quotes_but_cannot_start_imports_or_backtests(self):
        headers={'Origin':'chrome-extension://'+'a'*32,'X-Bridge-Token':self.engine.bridge_token}
        self.assertEqual(self.request('POST','/api/backtests/run',config(),headers)[0],403)
        self.assertEqual(self.request('POST','/api/backtests/sync',{},headers)[0],403)
        quote=dict(symbol='NQ-PERP',fee_bps=.25,slippage_bps=1,observed_at=time.time())
        unpaired={'Origin':headers['Origin']}
        for _ in range(5):
            self.assertEqual(self.request('POST','/api/backtests/cost-observation',quote,unpaired)[0],401)
        self.assertIsNone(self.service.store.setting('execution_quotes'))
        status,result=self.request('POST','/api/backtests/cost-observation',quote,headers)
        self.assertEqual(status,200);self.assertEqual(result['quote']['source'],'vest-order-form')
        self.assertEqual(self.request('GET','/api/backtests/candles?interval=invalid')[0],400)
