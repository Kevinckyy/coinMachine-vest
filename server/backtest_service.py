"""Background history imports and offline backtests; independent from live state."""
import copy
import math
import threading
import time
import uuid
import multiprocessing
import queue
import tempfile
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor,ProcessPoolExecutor,wait,FIRST_COMPLETED

from .backtesting import DEFAULT_CONFIG,RULE_SOURCES,simulate,validate_config
from .engine import ValidationError
from .market_history import BacktestStore,ReadOnlyHistoryBridge,INSTRUMENTS,sync_history
from .strategies import catalogue
from .backtest_runtime import DetailJournal
from .backtest_batches import BacktestCancelled,performance_key,estimate_duration,init_worker,run_worker_round,MAX_PARALLEL
from .backtest_statistics import BatchStatistics
from . import backtest_random


class BacktestService:
    def __init__(self,path='runtime/backtests.sqlite',*,bridge=None,store=None):
        self.store=store or BacktestStore(path);self.bridge=bridge or ReadOnlyHistoryBridge()
        self.store.recover_jobs()
        self.executor=ThreadPoolExecutor(max_workers=1,thread_name_prefix='offline-backtest')
        self.stop=threading.Event();self.active=None;self.lock=threading.Lock();self.cancel_event=threading.Event();self.active_id=None

    def close(self):
        if self.stop.is_set():return
        self.stop.set();self.executor.shutdown(wait=True,cancel_futures=True);self.store.close()

    def metadata(self,state=None):
        defaults=copy.deepcopy(DEFAULT_CONFIG)
        defaults.update(rng_algorithm=backtest_random.ALGORITHM,seed_policy=backtest_random.SEED_POLICY)
        state=state or {};saved=(state.get('preferences') or {}).get('strategy_config') or (state.get('strategy') or {}).get('config',{})
        for key in ('strategy_id','batch_size','side','size_percent','stop_loss','skip_stop_loss_on_low_drawdown',
                    'drawdown_stop_loss_threshold','order_delay_min','order_delay_max','batch_delay_min','batch_delay_max'):
            if key in saved:defaults[key]=saved[key]
        current=None
        for a in state.get('accounts',[]):
            metric=(a.get('capital_metrics') or {}).get('drawdown')
            if a.get('auto_discovered') and not a.get('archived') and not a.get('execution_excluded') and isinstance(metric,dict) and metric.get('limit'):
                capital=round(metric['limit']/.06,2)
                if capital in (5000,10000,25000):
                    defaults.update(initial_capital=capital,max_drawdown=metric['limit'],phase1_goal=capital*.1,phase2_goal=capital*.05,
                        live_profit_target=capital*.05,account_fee={5000:15,10000:35,25000:90}[capital])
                    daily=(a.get('capital_metrics') or {}).get('daily_loss')
                    if daily and daily.get('limit'):defaults['daily_loss_percent']=round(daily['limit']/capital*100,4)
                    current=dict(capital=capital,max_drawdown=metric['limit'],daily_loss=(daily or {}).get('limit'),
                        source='根据当前账户页面回撤上限及两阶段方案识别；目标和分润按官方两阶段规则预置，可修改')
                    break
        form,migration=self._display_form()
        if form:form={**form,'rng_algorithm':backtest_random.ALGORITHM,'seed_policy':backtest_random.SEED_POLICY}
        return dict(strategies=catalogue(),instruments=INSTRUMENTS,inventory=self.store.inventory(),defaults=defaults,
            saved_form=form,slippage_migration=migration,current_profile=current,runs=self.store.runs(),jobs=self.store.jobs(),
            execution_quotes=self.store.setting('execution_quotes') or {},sources=RULE_SOURCES,batches=self.store.batches(),parallelism_limit=MAX_PARALLEL,
            bridge=dict(origin=ReadOnlyHistoryBridge.ORIGIN,mode='GET-only',paths=sorted(ReadOnlyHistoryBridge.PATHS)))

    def _display_form(self):
        saved=self.store.setting('form')
        if not saved or saved.get('slippage_mode')!='bps':return saved,None
        # A percentage cannot become a fixed dollar amount without a reference
        # order. Convert only the editable view; preserve archived bps results
        # and the saved form until the user submits the new configuration.
        form={**DEFAULT_CONFIG,**copy.deepcopy(saved)};form.update(slippage_mode='usd',slippage_usd=None)
        try:
            c=validate_config(saved);instrument=self.store.instrument(c['symbol'])
            with self.store.lock:
                row=self.store.db.execute('SELECT time,open FROM bt_bars WHERE symbol=? AND interval=? AND time>=? AND time<=? ORDER BY time LIMIT 1',
                    (c['symbol'],c['interval'],c['from'],c['to'])).fetchone()
            if row is None:raise ValidationError('没有参考行情')
            tick=instrument['tick_size'];price=row['open'];entry=math.ceil(price*(1+c['slippage_bps']/10000)/tick-1e-10)*tick
            quantity=c['initial_capital']*c['size_percent']/100/(entry*instrument['point_value']*(1/c['leverage']+c['fee_bps']/10000))
            quantity=math.floor(quantity) if c['sizing']=='micro_contracts' else math.floor(quantity*10000)/10000
            amount=round(price*quantity*instrument['point_value']*c['slippage_bps']/10000,8);form['slippage_usd']=amount
            note=f"旧滑点 {c['slippage_bps']:g} bps 已按所选区间首根价格 {price:g}、初始买入仓位 {quantity:g} 份估算为每笔每边 {amount:g} USD。新测试使用固定金额；历史结果仍按原单位保存。"
            form['cost_source']=('旧 bps 滑点按区间首根初始仓位估算换算为固定 USD；'+str(form.get('cost_source','')))[:300]
        except (ValidationError,KeyError,ValueError,TypeError):
            note='旧 bps 参数缺少可用换算依据，请填写每笔每边 USD 滑点或切换 tick；历史结果保留原单位。'
        return form,note

    def _submit(self,kind,work,*,form=None):
        with self.lock:
            if self.active and not self.active.done():raise ValidationError('已有读取或回测任务正在处理，请等待完成',409)
            self.cancel_event.clear()
            if form is not None:self.store.setting('form',form)
            jid=uuid.uuid4().hex;self.store.save_job(jid,kind,'queued',{})
            self.active_id=jid
            def progress(detail):
                if self.stop.is_set():raise ValidationError('服务关闭，任务中断')
                if self.cancel_event.is_set():raise BacktestCancelled('已停止本次测试，完成的轮次已保留')
                self.store.save_job(jid,kind,'running',detail)
            def run():
                try:
                    progress({});result=work(jid,progress)
                    status=result.get('status','completed') if kind=='sync' else 'completed'
                    detail=result if kind=='sync' else {'estimate':result} if kind=='estimate' else result if kind=='batch' else {'run_id':jid,'batch_id':result.get('batch_id'),'metrics':result['metrics']}
                    self.store.save_job(jid,kind,status,detail)
                except BacktestCancelled as exc:
                    detail={'message':str(exc)}
                    if kind=='batch':detail.update(batch_id=jid,summary=self.store.batch(jid)['summary'])
                    self.store.save_job(jid,kind,'cancelled',detail)
                except Exception as exc:
                    detail={'error':str(exc)[:500]}
                    if kind=='batch':detail['batch_id']=jid
                    self.store.save_job(jid,kind,'error',detail)
            self.active=self.executor.submit(run)
            return {'job_id':jid,'kind':kind,'status':'queued'}

    def sync(self,body):
        config=copy.deepcopy(body)
        return self._submit('sync',lambda jid,progress:sync_history(self.store,self.bridge,config,progress))

    def backtest(self,body):
        config=validate_config({**body,'seed':backtest_random.fresh_seed(),'rng_algorithm':backtest_random.ALGORITHM,'seed_policy':backtest_random.SEED_POLICY});self.store.instrument(config['symbol'])
        if config['rounds']>1:return self._submit('batch',lambda jid,progress:self._run_batch(jid,config,progress),form=config)
        def run(jid,progress):
            backtest_random.reserve_seed(self.store,jid,0,config['seed'])
            bars=self.store.bars(config['symbol'],config['interval'],config['from'],config['to'])
            result=self._round(jid,config,bars,self.store.instrument(config['symbol']),progress,batch_id=jid,round_index=0)
            stats=BatchStatistics();stats.add_result(result)
            summary=dict(requested_rounds=1,completed_rounds=1,parallel_workers=1,duration_seconds=result['duration_seconds'],
                mean_round_seconds=result['duration_seconds'],dataset_sha256=result['dataset']['sha256'],**stats.result())
            self.store.save_batch(jid,config,summary,'completed');self.store.save_batch_round(jid,0,result)
            return result
        return self._submit('run',run,form=config)

    def cancel(self,jid):
        with self.lock:
            if jid!=self.active_id or not self.active or self.active.done():raise ValidationError('任务已结束，不能停止',409)
            if self.store.job(jid)['kind']=='sync':raise ValidationError('此入口只停止回测或测时任务')
            self.cancel_event.set()
        return {'ok':True,'cancel_requested':True}

    def _signature(self,config):return performance_key(config,self.store.setting('history_revision'))

    def estimate(self,body):
        if not isinstance(body.get('config'),dict):raise ValidationError('请提供完整的回测参数')
        config=validate_config({**body['config'],'rng_algorithm':backtest_random.ALGORITHM,'seed_policy':backtest_random.SEED_POLICY});self.store.instrument(config['symbol']);signature=self._signature(config)
        estimate=estimate_duration(config,self.store.timings(signature))
        if estimate:return {'estimate':estimate}
        if body.get('cached_only'):return {'needs_calibration':True}
        def measure(jid,progress):
            began=time.perf_counter()
            bars=self.store.bars(config['symbol'],config['interval'],config['from'],config['to'])
            simulate({**config,'rounds':1,'seed':backtest_random.fresh_seed()},bars,self.store.instrument(config['symbol']),progress)
            self.store.save_timing(signature,time.perf_counter()-began,'calibration')
            return estimate_duration(config,self.store.timings(signature))
        return self._submit('estimate',measure)

    def _round(self,jid,config,bars,instrument,progress,*,batch_id=None,round_index=None):
        began=time.perf_counter();journal=DetailJournal(self.store,jid)
        try:result=simulate(config,bars,instrument,progress,detail_sink=journal)
        finally:journal.flush()
        result.update(id=jid,created_at=time.time(),duration_seconds=time.perf_counter()-began)
        if batch_id is not None:result.update(batch_id=batch_id,round_index=round_index)
        self.store.save_run(jid,result);self.store.save_timing(self._signature(config),result['duration_seconds'])
        return result

    def _run_batch(self,jid,config,progress):
        began=time.perf_counter();stats=BatchStatistics();completed=0;duration_sum=0.;last_run=None;last_index=-1;fingerprint=None
        workers=min(config['parallel_workers'],config['rounds']);fractions={};last_report=0.
        initial_estimate=estimate_duration(config,self.store.timings(self._signature(config)))
        def summary():
            return dict(requested_rounds=config['rounds'],completed_rounds=completed,parallel_workers=workers,
                duration_seconds=time.perf_counter()-began,mean_round_seconds=duration_sum/completed if completed else None,dataset_sha256=fingerprint,**stats.result())
        self.store.save_batch(jid,config,summary())
        def report(detail=None,force=False):
            nonlocal last_report
            now=time.perf_counter()
            if not force and now-last_report<.3:return
            elapsed=now-began;units=completed+sum(fractions.values())
            remaining=elapsed/units*max(0,config['rounds']-units) if units>=.05 else max(0,initial_estimate['total_seconds']-elapsed) if initial_estimate else None
            progress(dict(detail or {},batch_id=jid,rounds_completed=completed,rounds_total=config['rounds'],parallel_workers=workers,
                elapsed_seconds=elapsed,remaining_seconds=remaining,estimated_total_seconds=elapsed+remaining if remaining is not None else None,
                per_round_seconds=duration_sum/completed if completed else None))
            last_report=now
        def accept(index,result):
            nonlocal completed,duration_sum,last_run,last_index,fingerprint
            current=result['dataset']['sha256']
            if fingerprint is not None and fingerprint!=current:raise ValidationError('同批次各轮历史数据不一致，已停止比较')
            fingerprint=current
            result.update(batch_id=jid,round_index=index)
            self.store.save_batch_round(jid,index,result);stats.add_result(result);completed+=1;duration_sum+=result['duration_seconds']
            if index>last_index:last_index=index;last_run=result['id']
            self.store.save_batch(jid,config,summary());report(force=True)
        try:
            bars=self.store.bars(config['symbol'],config['interval'],config['from'],config['to']);instrument=self.store.instrument(config['symbol'])
            if workers==1:
                for index in range(config['rounds']):
                    cfg={**config,'rounds':1,'seed':backtest_random.reserve_seed(self.store,jid,index,config['seed'])}
                    def update(detail):
                        fractions[index]=detail.get('completed',0)/max(1,detail.get('total',1));report(detail,force=self.cancel_event.is_set() or self.stop.is_set())
                    result=self._round(uuid.uuid4().hex,cfg,bars,instrument,update,batch_id=jid,round_index=index)
                    fractions.pop(index,None);accept(index,result)
            else:
                root=(Path(self.store.path).resolve().parent if self.store.path!=':memory:' else Path(__file__).resolve().parents[1]/'runtime')
                root.mkdir(parents=True,exist_ok=True)
                with tempfile.TemporaryDirectory(prefix='batch-workers-',dir=root) as workdir:
                    workroot=Path(workdir).resolve()
                    if workroot.parent!=root.resolve():raise ValidationError('临时回测目录不在预期位置')
                    # Freeze the parent-loaded candles once so external imports
                    # cannot make different seeds observe different histories.
                    snapshot=BacktestStore(workroot/'history.sqlite')
                    try:snapshot.save_chunk(instrument,config['interval'],bars)
                    finally:snapshot.close()
                    report(force=True)
                    ctx=multiprocessing.get_context('spawn');cancel=ctx.Event();updates=ctx.Queue(maxsize=workers*8)
                    source=str(workroot/'history.sqlite');payload=(config['symbol'],config['interval'],config['from'],config['to'])
                    pool=ProcessPoolExecutor(max_workers=workers,mp_context=ctx,initializer=init_worker,initargs=(source,payload,instrument,workdir,cancel,updates))
                    futures={};next_index=0
                    def enqueue():
                        nonlocal next_index
                        index=next_index;next_index+=1;rid=uuid.uuid4().hex
                        cfg={**config,'rounds':1,'seed':backtest_random.reserve_seed(self.store,jid,index,config['seed'])}
                        futures[pool.submit(run_worker_round,index,cfg,rid)]=(index,rid)
                    try:
                        for _ in range(workers):enqueue()
                        while futures:
                            if self.cancel_event.is_set() or self.stop.is_set():report(force=True)
                            done,_=wait(futures,timeout=.1,return_when=FIRST_COMPLETED)
                            inflight={i for i,_ in futures.values()}
                            while True:
                                try:detail=updates.get_nowait()
                                except queue.Empty:break
                                if detail['round_index'] in inflight:fractions[detail['round_index']]=detail.get('completed',0)/max(1,detail.get('total',1))
                            for future in done:
                                index,rid=futures.pop(future);packet=future.result();path=Path(packet['detail_path']).resolve()
                                if path.parent!=workroot:raise ValidationError('并行回测明细不在本次临时目录')
                                self.store.import_details(rid,path);result=packet['result'];result.update(batch_id=jid,round_index=index)
                                self.store.save_run(rid,result);self.store.save_timing(self._signature(config),result['duration_seconds'])
                                fractions.pop(index,None);accept(index,result)
                                if next_index<config['rounds']:enqueue()
                            report()
                    finally:
                        cancel.set();pool.shutdown(wait=True,cancel_futures=True);updates.close()
            final=summary();self.store.save_batch(jid,config,final,'completed')
            return dict(batch_id=jid,summary=final,run_id=last_run)
        except Exception as exc:
            self.store.save_batch(jid,config,summary(),'cancelled' if isinstance(exc,BacktestCancelled) else 'error')
            raise

    def observe_costs(self,body):
        symbol=body.get('symbol')
        if symbol not in {x['vest_symbol'] for x in INSTRUMENTS.values()}:raise ValidationError('费用观察只接受四个回测市场')
        from .backtesting import finite
        quote=dict(symbol=symbol,fee_bps=finite(body.get('fee_bps'),'页面手续费',0,100),
            observed_at=finite(body.get('observed_at'),'费用观察时间',time.time()-30,time.time()+5),
            source='vest-order-form',source_text=str(body.get('source_text',''))[:300])
        if body.get('slippage_bps') is not None:
            quote['slippage_bps']=finite(body['slippage_bps'],'页面预估滑点',0,100)
        if body.get('notional') is not None:quote['notional']=finite(body['notional'],'报价名义金额',.01,1e12)
        if 'notional' in quote and 'slippage_bps' in quote:quote['slippage_usd']=quote['notional']*quote['slippage_bps']/10000
        saved=self.store.setting('execution_quotes') or {};saved[symbol]=quote;self.store.setting('execution_quotes',saved)
        return {'ok':True,'quote':quote}
