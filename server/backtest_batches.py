"""Batch summaries, timing estimates and isolated CPU worker processes."""
import hashlib
from contextlib import closing
import json
import math
import os
import queue
import sqlite3
import time
from pathlib import Path

MAX_ROUNDS=2**32
MAX_PARALLEL=max(1,min(32,os.cpu_count() or 1))


class BacktestCancelled(Exception):pass


def performance_key(config,revision):
    body={k:v for k,v in config.items() if k not in ('seed','seed_policy','rounds','parallel_workers')}
    return hashlib.sha256(json.dumps([6,revision,body],sort_keys=True,separators=(',',':')).encode()).hexdigest()


def estimate_duration(config,samples):
    if not samples:return None
    measured=[s['seconds'] for s in samples if s['kind']=='run']
    calibrated=not measured
    values=measured or [s['seconds']*1.2 for s in samples]
    per_round=sum(values)/len(values);workers=min(config['parallel_workers'],config['rounds'])
    # Startup and contention are explicit assumptions until a parallel batch
    # provides actual wall-clock observations. Never promise linear scaling.
    startup=1.5 if workers>1 else 0
    total=math.ceil(config['rounds']/workers)*per_round*(1.15 if workers>1 else 1)+startup
    return dict(config=config,per_round_seconds=per_round,total_seconds=total,lower_seconds=total*.65,upper_seconds=total*1.5,
        effective_workers=workers,samples=len(values),source='当前配置单轮计算实测，含 20% 保存开销估计' if calibrated else '当前配置历史单轮实测',
        note='并行总时长按批次、进程启动及竞争开销估算；不同种子、CPU 负载和写盘会影响实际耗时')


class MetricSummary:
    def __init__(self):self.values={}

    def add(self,metrics):
        for key,value in metrics.items():
            if value is None or isinstance(value,bool) or not isinstance(value,(int,float)):continue
            r=self.values.setdefault(key,dict(count=0,mean=0.,m2=0.,min=value,max=value));r['count']+=1
            delta=value-r['mean'];r['mean']+=delta/r['count'];r['m2']+=delta*(value-r['mean'])
            r['min']=min(r['min'],value);r['max']=max(r['max'],value)

    def result(self):
        return {k:dict(count=r['count'],mean=r['mean'],min=r['min'],max=r['max'],stddev=math.sqrt(max(0,r['m2']/r['count']))) for k,r in self.values.items()}


def init_worker(source_path,bars,instrument,workdir,cancel,updates):
    global _bars,_instrument,_store,_cancel,_updates
    from .market_history import BacktestStore
    _instrument=instrument;_cancel=cancel;_updates=updates
    _updates.cancel_join_thread()
    if source_path:
        with closing(sqlite3.connect(Path(source_path).resolve().as_uri()+'?mode=ro',uri=True)) as db:
            db.row_factory=sqlite3.Row
            symbol,interval,start,end=bars
            _bars=[dict(r) for r in db.execute('SELECT time,open,high,low,close,volume FROM bt_bars WHERE symbol=? AND interval=? AND time>=? AND time<=? ORDER BY time',(symbol,interval,start,end))]
    else:_bars=bars
    _store=BacktestStore(Path(workdir)/('worker-'+str(os.getpid())+'.sqlite'))


def run_worker_round(index,config,run_id):
    from .backtesting import simulate
    from .backtest_runtime import DetailJournal
    def progress(detail):
        if _cancel.is_set():raise BacktestCancelled('已停止多轮回测')
        try:_updates.put_nowait(dict(round_index=index,**detail))
        except queue.Full:pass
    began=time.perf_counter();journal=DetailJournal(_store,run_id)
    try:result=simulate(config,_bars,_instrument,progress,detail_sink=journal)
    finally:journal.flush()
    result.update(id=run_id,created_at=time.time(),duration_seconds=time.perf_counter()-began)
    return dict(result=result,detail_path=_store.path,round_index=index)
