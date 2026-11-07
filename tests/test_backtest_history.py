import copy
import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode

from server.app import create_server
from server.backtest_batches import performance_key
from server.backtest_history import matching_names
from server.backtest_service import BacktestService
from server.backtest_statistics import BatchStatistics
from server.backtesting import simulate,validate_config
from server.engine import Engine,ValidationError
from server.market_history import BacktestStore
from test_backtesting import config,bar,BASE,INSTRUMENT


class HistoryTests(unittest.TestCase):
    def setUp(self):self.store=BacktestStore(':memory:')
    def tearDown(self):self.store.close()
    def record(self,index=0,created=1794018030.123,**changes):
        jid=f'{index:032x}';cfg=config(**changes)
        with patch('server.market_history.time.time',return_value=created):
            self.store.save_batch(jid,cfg,dict(completed_rounds=0,**BatchStatistics().result()),'completed')
        return jid

    def test_name_format_beijing_timestamp_and_update_preserves_original_snapshot(self):
        jid=self.record(record_name='  十月低回撤  ')
        row=self.store.history()['rows'][0]
        self.assertRegex(row['name'],r'^随机下单-十月低回撤-20261107-\d{9}$')
        before=self.store.batch(jid,summary_only=True);renamed=self.store.rename_history(jid,'新名称 [A/B]')
        self.assertEqual(renamed['name'].split('-')[-1],row['name'].split('-')[-1])
        self.assertEqual(renamed['strategy_name'],'随机下单')
        after=self.store.batch(jid,summary_only=True)
        self.assertEqual(after['config'],before['config']);self.assertEqual(after['summary'],before['summary'])
        self.assertEqual(self.store.history()['rows'][0]['custom_name'],'新名称 [A/B]')
        self.assertEqual(self.store.batches()[0]['name'],renamed['name'])
        self.assertIn('-未命名-',self.store.rename_history(jid,'')['name'])

    def test_custom_name_validation_and_name_does_not_affect_timing_signature(self):
        for value in (None,12,True,[],{},'a'*81,'a\nb','\x00'):
            with self.subTest(value=value),self.assertRaises(ValidationError):validate_config(config(record_name=value))
        jid=self.record(record_name='baseline');before=self.store.history()['rows'][0]['name']
        with self.assertRaises(ValidationError):self.store.rename_history(jid,'bad\nname')
        self.assertEqual(self.store.history()['rows'][0]['name'],before)
        self.assertEqual(performance_key(config(record_name='one'),'r'),performance_key(config(record_name='two'),'r'))

    def test_filter_root_strategy_and_regex_intersect(self):
        self.record(0,root='MNQ',record_name='十月-A')
        self.record(1,root='MES',record_name='十月-B')
        self.record(2,root='MNQ',record_name='十一月-C',strategy_id='future_strategy')
        result=self.store.history(root='MNQ',strategy_id='random_entry',pattern='十月-[a-z]')
        self.assertEqual(result['total'],1);self.assertEqual(result['rows'][0]['id'],f'{0:032x}')
        self.assertEqual(self.store.history(pattern='十月|十一月')['total'],3)
        self.assertEqual(self.store.history(pattern='^future_strategy-')['total'],1)
        self.assertEqual(self.store.history(root="MNQ' OR 1=1 --")['total'],0)

    def test_time_filters_distinguish_creation_and_data_overlap(self):
        self.record(0,created=1000,**{'from':100,'to':200})
        self.record(1,created=2000,**{'from':200,'to':300})
        self.assertEqual(self.store.history(start=1000,end=1000)['total'],1)
        self.assertEqual(self.store.history(start=1500)['rows'][0]['id'],f'{1:032x}')
        self.assertEqual(self.store.history(time_basis='data',start=150,end=200)['total'],2)
        self.assertEqual(self.store.history(time_basis='data',end=99)['total'],0)
        for changes in (dict(start=10,end=1),dict(start=float('nan')),dict(time_basis='invalid'),dict(offset=-1),dict(limit=101),dict(limit=True),dict(pattern='[')):
            with self.subTest(changes=changes),self.assertRaises(ValidationError):self.store.history(**changes)

    def test_pagination_includes_more_than_twenty_records_and_stable_ties(self):
        for i in range(45):self.record(i)
        first=self.store.history(limit=20);second=self.store.history(offset=20,limit=20);third=self.store.history(offset=40)
        self.assertEqual(first['total'],45);self.assertEqual(len(third['rows']),5)
        ids=[r['id'] for page in (first,second,third) for r in page['rows']]
        self.assertEqual(len(set(ids)),45);self.assertEqual(ids,sorted(ids,reverse=True))

    def test_legacy_single_records_all_indexed_without_duplicating_multi_rounds(self):
        result=simulate(config(),[bar(),bar(BASE+60)],INSTRUMENT);original=copy.deepcopy(result)
        for i in range(25):self.store.save_run(f'{i:032x}',result)
        self.record(100)
        self.store.save_run(f'{101:032x}',{**result,'batch_id':f'{100:032x}'})
        page=self.store.history(limit=100)
        self.assertEqual(page['total'],26);self.assertEqual(self.store.history(limit=100)['total'],26)
        self.assertEqual(self.store.run(f'{0:032x}'),original)
        self.assertEqual(self.store.batch(f'{0:032x}',summary_only=True)['summary']['completed_rounds'],1)

    def test_renaming_persists_on_restart_and_survives_batch_progress_updates(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]/'runtime') as folder:
            store=BacktestStore(Path(folder)/'cache.sqlite');cfg=config(record_name='before');jid='a'*32
            store.save_batch(jid,cfg,{});store.rename_history(jid,'after');store.save_batch(jid,cfg,{},'completed');store.close()
            store=BacktestStore(Path(folder)/'cache.sqlite')
            try:self.assertEqual(store.history()['rows'][0]['custom_name'],'after')
            finally:store.close()

    def test_regex_error_and_timeout_do_not_leave_a_worker_running(self):
        self.assertEqual(matching_names('alpha|beta',['Alpha','Gamma','Beta']),[0,2])
        with self.assertRaisesRegex(ValidationError,'正则无效'):matching_names('[',[])
        with self.assertRaisesRegex(ValidationError,'超时'):matching_names('(a+)+$',['a'*80+'!'],timeout=.5)
        self.assertEqual(matching_names('ok',['ok']),[0])

    def test_failed_submitted_single_is_named_and_discoverable(self):
        service=BacktestService(':memory:')
        try:
            service.store.save_chunk(INSTRUMENT,60,[])
            task=service.backtest(config(record_name='无数据对照'));service.active.result(timeout=5)
            row=service.store.history()['rows'][0]
            self.assertEqual(row['id'],task['job_id']);self.assertEqual(row['status'],'error');self.assertEqual(row['custom_name'],'无数据对照')
        finally:service.close()


class HistoryHTTPTests(unittest.TestCase):
    def setUp(self):
        self.engine=Engine(':memory:',seed=False);self.service=BacktestService(':memory:')
        self.service.store.save_chunk(INSTRUMENT,60,[bar(),bar(BASE+60)])
        self.server=create_server(self.engine,port=0,backtests=self.service)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join();self.engine.close()
    def request(self,method,path,body=None,headers=None):
        connection=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=8)
        connection.request(method,path,json.dumps(body) if body is not None else None,{'Content-Type':'application/json',**(headers or {})})
        result=connection.getresponse();status=result.status;data=json.loads(result.read());connection.close();return status,data
    def test_named_submission_filter_rename_and_detail_do_not_change_trading_state(self):
        before=copy.deepcopy(self.engine.data)
        code,task=self.request('POST','/api/backtests/run',config(record_name='历史测试',rounds=2,parallel_workers=1))
        self.assertEqual(code,200);self.service.active.result(timeout=10)
        code,page=self.request('GET','/api/backtests/history?'+urlencode(dict(root='MNQ',pattern='历史测试')))
        self.assertEqual(code,200);self.assertEqual(page['total'],1)
        jid=task['job_id'];code,renamed=self.request('POST','/api/backtests/history/'+jid+'/name',{'custom_name':'改名'})
        self.assertEqual(code,200);self.assertIn('-改名-',renamed['name'])
        code,detail=self.request('GET','/api/backtests/batches/'+jid+'?summary_only=1')
        self.assertEqual(code,200);self.assertEqual(detail['name'],renamed['name']);self.assertEqual(detail['rows'],[])
        self.assertEqual(detail['config']['record_name'],'历史测试')
        self.assertEqual(detail['summary']['completed_rounds'],2);self.assertEqual(self.engine.data,before)
        self.assertEqual(self.request('GET','/api/backtests/history?from=no')[0],400)
        self.assertEqual(self.request('GET','/api/backtests/history?pattern=%5B')[0],400)
        self.assertEqual(self.request('POST','/api/backtests/history/'+('f'*32)+'/name',{'custom_name':'a'})[0],404)
        self.assertEqual(self.request('POST','/api/backtests/history/'+jid+'/name',{'custom_name':'x'},{'Origin':'chrome-extension://'+'a'*32})[0],403)
