import copy
import http.client
import json
import threading
import unittest
from datetime import datetime
from unittest.mock import patch

from server.app import create_server
from server.backtest_service import BacktestService
from server.backtest_statistics import BatchStatistics,STATISTICS_VERSION
from server.backtest_time_statistics import RoundTimeStatistics,TimeSummary,from_trades,local_time,time_keys,normalize_zone,packet_from_grid
from server.backtesting import simulate
from server.engine import Engine,ValidationError
from server.market_history import BacktestStore
from test_backtesting import config,bar,BASE,INSTRUMENT


NY='America/New_York';SH='Asia/Shanghai'
def stamp(text):return datetime.fromisoformat(text.replace('Z','+00:00')).timestamp()
def trade(opened,closed=None,pnl=1,**changes):return dict(id=1,account_id=1,phase='live',side='buy',opened_at=opened,closed_at=closed if closed is not None else opened+60,pnl=pnl,**changes)
def rows(summary,zone=NY,axis='hour'):return {r['key']:r for r in summary['zones'][zone][axis]}


class TimeStatisticsTests(unittest.TestCase):
    def test_fixed_utc_offsets_validate_quarter_hours_and_do_not_follow_dst(self):
        self.assertEqual(normalize_zone('UTC+5:30'),'UTC+05:30')
        self.assertEqual(normalize_zone('UTC-00:00'),'UTC+00:00')
        for zone in ('UTC-12:00','UTC+14:00','UTC+05:45','UTC-03:30','UTC+00:00'):self.assertEqual(normalize_zone(zone),zone)
        for zone in ('UTC+14:15','UTC-12:15','UTC+05:10','UTC+99:00','UTC','GMT+8',None):
            with self.subTest(zone=zone),self.assertRaises(ValidationError):normalize_zone(zone)
        winter=stamp('2026-01-01T12:00:00Z');summer=stamp('2026-07-01T12:00:00Z')
        self.assertEqual(local_time(winter,'UTC-05:00').hour,7);self.assertEqual(local_time(summer,'UTC-05:00').hour,7)
        self.assertEqual(local_time(summer,NY).hour,8)
        self.assertEqual(local_time(winter,'UTC+05:45').minute,45)

    def test_universal_grid_matches_direct_grouping_across_dates_and_dst(self):
        ts=[stamp(t) for t in ('2026-03-08T06:59:59Z','2026-03-08T07:00:00Z','2026-06-01T03:59:59Z','2026-11-01T05:30:00Z','2026-11-01T06:30:00Z','2026-12-31T23:59:59Z')]
        trades=[trade(t,t+3601,pnl=10 if i%2 else -3) for i,t in enumerate(ts)]
        base=RoundTimeStatistics()
        for t in trades:base.add(t)
        packet=base.result(len(trades),ts[0],ts[-1]+4000)
        for zone in (NY,SH,'UTC-12:00','UTC+14:00','UTC+05:45','UTC-03:30','UTC+00:00'):
            direct=RoundTimeStatistics((zone,))
            for t in trades:direct.add(t)
            expected=direct.result(len(trades),packet['start'],packet['end'])
            converted=packet_from_grid(packet,zone)
            self.assertEqual(converted['zones'][zone],expected['zones'][zone])

    def test_fixed_zone_cache_from_full_sql_details_and_invalidates_on_new_rounds(self):
        store=BacktestStore(':memory:');jid='d'*32;cfg=config(rounds=2)
        try:
            store.save_batch(jid,cfg,{})
            result=simulate(config(account_count=600,batch_size=600,stop_loss=1),[bar(low=98),bar(BASE+60)],INSTRUMENT)
            result.pop('time_statistics');result['id']='e'*32;result['duration_seconds']=1
            store.save_run(result['id'],result)
            # The embedded preview has only 500 trades; SQL must use all 600.
            actual=[{**trade(BASE,BASE+60,-1),'id':i+1} for i in range(600)]
            store.save_details(result['id'],[('trades',t) for t in actual]);store.save_batch_round(jid,0,result)
            before=copy.deepcopy(store.run(result['id']));stats=store.zone_statistics(jid,'UTC+05:30')
            self.assertEqual(stats['recorded_trades'],600);self.assertEqual(stats['complete_rounds'],1)
            self.assertEqual(sum(r['total_pnl'] for r in stats['zones']['UTC+05:30']['hour']),-600)
            self.assertEqual(store.run(result['id']),before)
            with patch.object(store,'_utc_grid',side_effect=AssertionError('must use cached result')):self.assertEqual(store.zone_statistics(jid,'UTC+5:30'),stats)
            # Other offsets reuse the universal grid, not the raw trade payloads.
            with patch.object(store,'run',side_effect=AssertionError('must use cached grid')):
                self.assertEqual(store.zone_statistics(jid,'UTC-12:00')['recorded_trades'],600)
            second={**result,'id':'f'*32};store.save_run(second['id'],second);store.save_details(second['id'],[('trades',t) for t in actual]);store.save_batch_round(jid,1,second)
            self.assertEqual(store.zone_statistics(jid,'UTC+05:30')['complete_rounds'],2)
            self.assertEqual(store.zone_statistics(jid,'UTC+05:30')['recorded_trades'],1200)
        finally:store.close()

    def test_grid_sql_preserves_saved_win_flags_and_marks_invalid_records(self):
        store=BacktestStore(':memory:');jid='1'*32;rid='2'*32
        try:
            result=dict(id=rid,duration_seconds=1,config=config(),metrics=dict(total_trades=3),dataset=dict(first_time=BASE,last_time=BASE+60),trades=[])
            store.save_run(rid,result);store.save_batch(jid,config(),{});store.save_batch_round(jid,0,result)
            trades=[{**trade(BASE,pnl=0),'is_win':True},{**trade(BASE),'id':2,'closed_at':BASE-1},{**trade(BASE),'id':3,'opened_at':True}]
            store.save_details(rid,[('trades',t) for t in trades]);packet=store._utc_grid(rid)
            self.assertEqual(packet['recorded_trades'],1);self.assertFalse(packet['complete'])
            self.assertEqual(sum(row[2] for row in packet['utc_grid']),1)
            self.assertEqual(store.zone_statistics(jid,'UTC+00:00')['incomplete_rounds'],1)
        finally:store.close()
    def test_cross_midnight_week_and_month_use_open_time_but_closes_use_exit_hour(self):
        opened=stamp('2026-06-01T03:30:00Z');closed=stamp('2026-06-01T05:00:00Z')
        r=RoundTimeStatistics();r.add(trade(opened,closed,pnl=-15));s=TimeSummary();s.add(r.result(1,opened-60,closed));s=s.result()
        self.assertEqual(rows(s)['23']['trades'],1);self.assertEqual(rows(s)['1']['closes'],1)
        self.assertEqual(rows(s)['23']['avg_holding_seconds'],5400);self.assertEqual(rows(s)['1']['trades'],0)
        self.assertEqual(rows(s,NY,'weekday')['6']['total_pnl'],-15)
        self.assertEqual(rows(s,NY,'month')['2026-05']['trades'],1)
        self.assertEqual(rows(s,NY,'week')['2026-05-25']['trades'],1)
        self.assertEqual(rows(s,SH,'month')['2026-06']['trades'],1)
        self.assertEqual(rows(s,SH,'weekday')['0']['trades'],1)
        self.assertEqual(rows(s,SH,'hour')['11']['avg_holding_seconds'],5400)

    def test_dst_fallback_matches_utc_transition_hours(self):
        stamps=[stamp(t) for t in ('2026-11-01T05:30:00Z','2026-11-01T06:30:00Z','2026-03-08T06:30:00Z','2026-03-08T07:30:00Z')]
        with patch('server.backtest_time_statistics.NEW_YORK',None):
            self.assertEqual([local_time(t,NY).hour for t in stamps],[1,1,1,3])
            self.assertEqual([local_time(t,NY).utcoffset().total_seconds()/3600 for t in stamps],[-4,-5,-5,-4])
            time_keys.cache_clear();r=RoundTimeStatistics()
            for t in stamps[:2]:r.add(trade(t,t+120))
            s=TimeSummary();s.add(r.result(2,stamps[0],stamps[1]+120));h=rows(s.result())
            self.assertEqual(h['1']['trades'],2);self.assertEqual(h['1']['closes'],2);self.assertEqual(h['1']['avg_holding_seconds'],120)
        time_keys.cache_clear()

    def test_equal_round_win_rates_differ_from_trade_weighting_and_daily_counts(self):
        t=stamp('2026-06-01T14:00:00Z');a=RoundTimeStatistics();a.add(trade(t,t+7200,90))
        b=RoundTimeStatistics()
        for i in range(9):b.add(trade(t,t+1800,10 if i==0 else -10))
        s=TimeSummary();s.add(a.result(1,t,t+2*86400));s.add(b.result(9,t,t+4*86400));r=rows(s.result())['10']
        self.assertAlmostEqual(r['mean_round_win_rate'],(100+100/9)/2)
        self.assertEqual(r['pooled_win_rate'],20);self.assertEqual(r['active_rounds'],2)
        self.assertEqual(r['total_pnl'],20);self.assertEqual(r['mean_round_pnl'],10);self.assertEqual(r['avg_trade_pnl'],2)
        self.assertEqual(r['avg_holding_seconds'],2340);self.assertEqual(r['mean_opens'],5)
        self.assertEqual(r['daily_opens'],1.375)
        self.assertIsNone(rows(s.result())['0']['pooled_win_rate']);self.assertEqual(rows(s.result())['0']['mean_round_pnl'],0)

    def test_empty_rounds_count_as_zero_pnl_and_counts_but_not_zero_win_rate(self):
        t=stamp('2026-06-01T14:00:00Z');r=RoundTimeStatistics();r.add(trade(t,pnl=8))
        s=TimeSummary();s.add(r.result(1,t,t+86400));s.add(RoundTimeStatistics().result(0,t,t+86400));s.add(r.result(2,t,t+86400))
        h=rows(s.result())['10'];self.assertEqual(h['mean_round_pnl'],4);self.assertEqual(h['mean_opens'],.5)
        self.assertEqual(h['mean_round_win_rate'],100);self.assertEqual(s.result()['incomplete_rounds'],1)
        self.assertEqual(s.result()['expected_trades'],3);self.assertEqual(s.result()['recorded_trades'],2)

    def test_calendar_axes_include_empty_weeks_months_and_seven_weekdays(self):
        t=stamp('2026-01-01T00:00:00Z');end=stamp('2026-04-01T00:00:00Z');s=TimeSummary()
        s.add(RoundTimeStatistics().result(0,t,end));result=s.result()
        self.assertEqual(len(result['zones'][SH]['hour']),24);self.assertEqual(len(result['zones'][SH]['weekday']),7)
        self.assertEqual([r['key'] for r in result['zones'][SH]['month']],['2026-01','2026-02','2026-03','2026-04'])
        self.assertEqual(result['zones'][SH]['week'][0]['key'],'2025-12-29')
        self.assertTrue(all(r['mean_round_pnl']==0 for r in result['zones'][SH]['week']))

    def test_invalid_trade_timestamps_are_incomplete_and_never_guessed(self):
        r=RoundTimeStatistics()
        for bad in (trade(BASE,BASE-1),dict(opened_at=None,closed_at=BASE,pnl=1),trade(BASE,pnl=float('nan'))):r.add(bad)
        self.assertEqual(r.result(3)['recorded_trades'],0);self.assertFalse(r.result(3)['complete'])

    def test_saved_win_flag_preserves_profit_smaller_than_display_rounding(self):
        r=RoundTimeStatistics();r.add({**trade(BASE,pnl=0),'is_win':True})
        self.assertEqual(sum(b['wins'] for b in r.result(1)['zones'][NY]['hour'].values()),1)

    def test_simulation_aggregates_full_stream_beyond_preview_and_preserves_totals(self):
        journal=[];r=simulate(config(account_count='infinity',batch_size=100,stop_loss=1),[bar(BASE+i*60,low=98) for i in range(12)],INSTRUMENT,
            detail_sink=lambda kind,row:journal.append((kind,row)))
        self.assertEqual(len(r['trades']),500);self.assertEqual(r['time_statistics']['recorded_trades'],1200)
        rebuilt=from_trades(r,(row for kind,row in journal if kind=='trades'))
        self.assertEqual(r['time_statistics'],rebuilt)
        s=BatchStatistics();s.add_result(r);s=s.result()['time_statistics']
        for zone in (NY,SH):
            for axis in ('hour','weekday','week','month'):
                axis_rows=s['zones'][zone][axis]
                self.assertEqual(sum(row['trades'] for row in axis_rows),1200)
                self.assertAlmostEqual(sum(row['total_pnl'] for row in axis_rows),r['metrics']['total_pnl'])
                self.assertAlmostEqual(sum(row['total_holding_seconds'] for row in axis_rows),r['metrics']['total_holding_seconds'])
            self.assertEqual(sum(row['closes'] for row in s['zones'][zone]['hour']),1200)

    def test_archived_backfill_uses_full_details_caches_and_keeps_original_results(self):
        journal=[];r=simulate(config(account_count=600,batch_size=600,stop_loss=1),[bar(low=98),bar(BASE+60)],INSTRUMENT,detail_sink=lambda kind,row:journal.append((kind,row)))
        r.pop('time_statistics');r['id']='a'*32;original=copy.deepcopy(r);store=BacktestStore(':memory:')
        try:
            store.save_run(r['id'],r);store.save_details(r['id'],journal)
            record=store.batch(r['id'],summary_only=True)
            self.assertEqual(record['summary']['statistics_version'],STATISTICS_VERSION)
            self.assertEqual(record['summary']['time_statistics']['recorded_trades'],600)
            self.assertEqual(store.run(r['id']),original)
            with patch('server.backtest_time_statistics.from_trades',side_effect=AssertionError('must use derived cache')):
                self.assertTrue(store._time_result(original)['time_statistics']['complete'])
            self.assertNotIn('time_statistics',store.batches()[0]['summary'])
        finally:store.close()

    def test_truncated_legacy_preview_is_reported_incomplete(self):
        r=simulate(config(account_count=600,batch_size=600,stop_loss=1),[bar(low=98),bar(BASE+60)],INSTRUMENT);r.pop('time_statistics');r['id']='b'*32
        store=BacktestStore(':memory:')
        try:
            store.save_run(r['id'],r);s=store.batch(r['id'],summary_only=True)['summary']['time_statistics']
            self.assertFalse(s['available']);self.assertEqual(s['incomplete_rounds'],1);self.assertEqual(s['recorded_trades'],500)
            page=store.trade_times(r['id']);self.assertEqual(page['total'],500);self.assertEqual(page['expected'],600);self.assertFalse(page['complete'])
        finally:store.close()

    def test_merged_trade_time_pagination_sorts_by_open_not_close_and_preserves_ties(self):
        store=BacktestStore(':memory:');jid='c'*32;cfg=config(rounds=2)
        try:
            store.save_batch(jid,cfg,{})
            for i in range(2):
                rid=f'{i:032x}';ts=[trade(BASE+120,BASE+600,pnl=2),{**trade(BASE,BASE+1000,pnl=-1),'id':2}]
                result=dict(id=rid,duration_seconds=1,config=cfg,metrics=dict(total_trades=2),trades=ts)
                store.save_run(rid,result);store.save_batch_round(jid,i,result)
                if i==0:store.save_details(rid,[('trades',t) for t in ts])
            first=store.trade_times(jid,limit=3);last=store.trade_times(jid,offset=3,limit=3)
            self.assertEqual(first['total'],4);self.assertTrue(first['complete']);self.assertEqual(len(last['rows']),1)
            combined=first['rows']+last['rows'];self.assertEqual([t['opened_at'] for t in combined],[BASE+120,BASE+120,BASE,BASE])
            self.assertEqual([t['round_index'] for t in combined],[1,0,1,0])
            for kwargs in (dict(offset=-1),dict(limit=101)):
                with self.assertRaises(ValidationError):store.trade_times(jid,**kwargs)
        finally:store.close()


class TradeTimeHTTPTests(unittest.TestCase):
    def test_summary_and_paged_timestamps_are_offline(self):
        e=Engine(':memory:',seed=False);before=copy.deepcopy(e.data);service=BacktestService(':memory:')
        service.store.save_chunk(INSTRUMENT,60,[bar(),bar(BASE+60)])
        task=service.backtest(config(rounds=2,parallel_workers=1));service.active.result(timeout=10)
        server=create_server(e,port=0,backtests=service);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            for suffix,expected in (('/trade-times?limit=1',200),('/trade-times?limit=bad',400),('/trade-times?offset=-1',400)):
                client=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=5);client.request('GET','/api/backtests/batches/'+task['job_id']+suffix);r=client.getresponse();data=json.loads(r.read());client.close()
                self.assertEqual(r.status,expected)
                if expected==200:self.assertEqual(data['total'],2);self.assertIn('closed_at',data['rows'][0]);self.assertIn('opened_at',data['rows'][0])
            self.assertEqual(e.data,before)
            for zone,expected in (('UTC%2B05%3A30',200),('UTC-12%3A00',200),('UTC%2B15%3A00',400),('invalid',400)):
                client=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=5);client.request('GET','/api/backtests/batches/'+task['job_id']+'/time-statistics?zone='+zone);r=client.getresponse();data=json.loads(r.read());client.close();self.assertEqual(r.status,expected)
                if expected==200:self.assertEqual(data['recorded_trades'],2)
            self.assertEqual(e.data,before)
        finally:server.shutdown();server.server_close();thread.join();e.close()
