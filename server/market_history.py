"""GET-only NT8 history adapter and a separate local backtest database.

The origin and paths are fixed. There is deliberately no arbitrary URL, account,
order, position, POST, proxy, or redirect facility in the upstream client.
"""
import copy
from contextlib import closing
import hashlib
import json
import math
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .engine import ValidationError
from .backtest_history import BacktestHistory

INSTRUMENTS = {
    'MNQ':dict(name='纳指微型',vest_symbol='NQ-PERP',point_value=2,tick_size=.25,leverage=50),
    'MES':dict(name='标普微型',vest_symbol='ES-PERP',point_value=5,tick_size=.25,leverage=75),
    'MGC':dict(name='黄金微型',vest_symbol='GC-PERP',point_value=10,tick_size=.1,leverage=25),
    'MCL':dict(name='原油微型',vest_symbol='CL-PERP',point_value=100,tick_size=.01,leverage=10),
}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValidationError('行情桥返回重定向，已拒绝；只允许固定只读接口',502)


class ReadOnlyHistoryBridge:
    ORIGIN='http://127.0.0.1:8090'
    PATHS={'/api/status','/api/symbols','/api/resolve','/api/history'}

    def __init__(self, opener=None):
        self.opener=opener or urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())

    def _get(self,path,params=None):
        if path not in self.PATHS:
            raise ValidationError('回测数据桥只允许读取状态、合约与历史K线',403)
        url=self.ORIGIN+path+('?' + urllib.parse.urlencode(params) if params else '')
        req=urllib.request.Request(url,headers={'Accept':'application/json'},method='GET')
        try:
            with self.opener.open(req,timeout=35) as response:
                if response.geturl()!=url:
                    raise ValidationError('行情桥改变了请求地址，已拒绝',502)
                raw=response.read(20_000_001)
                if len(raw)>20_000_000:
                    raise ValidationError('单次历史响应过大，请缩小读取窗口',502)
                result=json.loads(raw)
                if not isinstance(result,dict):
                    raise ValidationError('行情桥响应格式无效',502)
                return result
        except ValidationError:raise
        except (OSError,ValueError,urllib.error.URLError) as exc:
            raise ValidationError('无法读取 8090 行情桥：'+str(exc)[:200],502) from None

    def status(self):
        raw=self._get('/api/status')
        return {k:raw.get(k) for k in ('connected','historyWindowVersion','symbolCatalogVersion','marketError','error')}

    def instruments(self):
        raw=self._get('/api/symbols',{'currentOnly':'true'})
        if raw.get('currentOnly') is not True:
            raise ValidationError('行情桥不支持原生当前月份目录，请先更新数据桥',409)
        rows=[]
        for item in raw.get('symbols',[]):
            symbol=item.get('symbol','')
            match=re.fullmatch(r'(MNQ|MES|MGC|MCL) \d{2}-\d{2}',symbol)
            if not match:continue
            spec=INSTRUMENTS[match[1]]
            if item.get('pointValue')!=spec['point_value'] or item.get('tickSize')!=spec['tick_size']:
                continue
            rows.append({**item,'root':match[1]})
        return rows

    def resolve(self,root,symbol=None,catalog=None):
        if root not in INSTRUMENTS:raise ValidationError('只支持 MNQ、MES、MGC、MCL')
        if symbol:
            if not re.fullmatch(re.escape(root)+r' \d{2}-\d{2}',symbol):
                raise ValidationError('必须使用对应品种的原生完整月份合约，不接受裸根代码')
        else:
            matches=[x for x in (catalog if catalog is not None else self.instruments()) if x['root']==root]
            if len(matches)!=1:raise ValidationError(root+' 的当前月份不能唯一确定，请填写完整合约')
            symbol=matches[0]['symbol']
        result=self._get('/api/resolve',{'symbol':symbol})
        spec=INSTRUMENTS[root]
        if result.get('symbol')!=symbol or result.get('pointValue')!=spec['point_value'] or result.get('tickSize')!=spec['tick_size']:
            raise ValidationError('行情桥合约名称、点值或最小跳动与微型合约不符',409)
        return {**result,'root':root,'point_value':result['pointValue'],'tick_size':result['tickSize']}

    def history(self,symbol,interval,start,end):
        if not re.fullmatch(r'(MNQ|MES|MGC|MCL) \d{2}-\d{2}',symbol):raise ValidationError('历史请求合约无效')
        return self._get('/api/history',dict(symbol=symbol,interval=interval,**{'from':start,'to':end})).get('bars',[])


def validated_bar(raw,start,end,interval,now):
    keys=('time','open','high','low','close','volume')
    if not isinstance(raw,dict) or any(isinstance(raw.get(k),bool) or not isinstance(raw.get(k),(int,float)) or not math.isfinite(raw[k]) for k in keys):
        raise ValidationError('历史K线包含无效或非有限数值')
    t=raw['time']
    if int(t)!=t or not start<=t<=end:raise ValidationError('历史K线时间不在请求范围内')
    if min(raw[k] for k in ('open','high','low','close'))<=0 or raw['volume']<0 or raw['low']>min(raw['open'],raw['close']) or raw['high']<max(raw['open'],raw['close']):
        raise ValidationError('历史K线 OHLC 或成交量关系无效')
    if t+interval>now:return None # Never cache an unfinished candle as final history.
    return {k:raw[k] for k in keys}


class BacktestStore(BacktestHistory):
    def __init__(self,path='runtime/backtests.sqlite'):
        if path!=':memory:':Path(path).resolve().parent.mkdir(parents=True,exist_ok=True)
        self.path=str(path);self.lock=threading.RLock()
        self.db=sqlite3.connect(str(path),check_same_thread=False)
        self.db.row_factory=sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS bt_instruments(symbol TEXT PRIMARY KEY,root TEXT NOT NULL,metadata TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS bt_bars(symbol TEXT,interval INTEGER,time INTEGER,open REAL,high REAL,low REAL,close REAL,volume REAL,PRIMARY KEY(symbol,interval,time));
            CREATE TABLE IF NOT EXISTS bt_jobs(id TEXT PRIMARY KEY,kind TEXT,status TEXT,detail TEXT,updated_at REAL);
            CREATE TABLE IF NOT EXISTS bt_runs(id TEXT PRIMARY KEY,created_at REAL,result TEXT);
            CREATE TABLE IF NOT EXISTS bt_settings(key TEXT PRIMARY KEY,value TEXT);
            CREATE TABLE IF NOT EXISTS bt_record_names(id TEXT PRIMARY KEY,custom_name TEXT NOT NULL,strategy_name TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS bt_details(run_id TEXT,kind TEXT,seq INTEGER,phase TEXT,payload TEXT,PRIMARY KEY(run_id,kind,seq));
            CREATE TABLE IF NOT EXISTS bt_batches(id TEXT PRIMARY KEY,created_at REAL,updated_at REAL,status TEXT,config TEXT,summary TEXT);
            CREATE TABLE IF NOT EXISTS bt_batch_rounds(batch_id TEXT,round_index INTEGER,run_id TEXT,seed INTEGER,duration REAL,metrics TEXT,PRIMARY KEY(batch_id,round_index));
            CREATE TABLE IF NOT EXISTS bt_round_seeds(batch_id TEXT,round_index INTEGER,seed TEXT,PRIMARY KEY(batch_id,round_index),UNIQUE(batch_id,seed));
            CREATE TABLE IF NOT EXISTS bt_timings(id INTEGER PRIMARY KEY,signature TEXT,seconds REAL,kind TEXT,measured_at REAL);
            CREATE INDEX IF NOT EXISTS bt_timing_signature ON bt_timings(signature,id);
            CREATE INDEX IF NOT EXISTS bt_runs_created ON bt_runs(created_at DESC);
            CREATE INDEX IF NOT EXISTS bt_batch_run ON bt_batch_rounds(run_id);
        ''')
        self.db.commit()

    def recover_jobs(self):
        with self.lock,self.db:self.db.execute("UPDATE bt_jobs SET status='error',detail=? WHERE status IN ('queued','running')",(json.dumps({'error':'服务重启，未完成任务已中断；可重新执行只读任务'},ensure_ascii=False),))
        with self.lock,self.db:self.db.execute("UPDATE bt_batches SET status='interrupted' WHERE status='running'")

    def close(self):
        with self.lock:self.db.close()

    def save_chunk(self,instrument,interval,bars):
        with self.lock,self.db:
            self.db.execute('INSERT OR REPLACE INTO bt_instruments VALUES(?,?,?)',(instrument['symbol'],instrument['root'],json.dumps(instrument)))
            self.db.executemany('INSERT OR REPLACE INTO bt_bars VALUES(?,?,?,?,?,?,?,?)',[(instrument['symbol'],interval,b['time'],b['open'],b['high'],b['low'],b['close'],b['volume']) for b in bars])
            self.db.execute('INSERT OR REPLACE INTO bt_settings VALUES(?,?)',('history_revision',json.dumps(str(time.time_ns()))))

    def inventory(self):
        with self.lock:
            # Aggregate the covering candle index before joining metadata. Joining
            # first sorts millions of copies of the instrument JSON for GROUP BY.
            return [dict(r) for r in self.db.execute('''SELECT i.symbol,i.root,i.metadata,b.interval,COALESCE(b.bars,0) AS bars,b.first_time,b.last_time
                FROM bt_instruments i LEFT JOIN
                (SELECT symbol,interval,COUNT(*) AS bars,MIN(time) AS first_time,MAX(time) AS last_time FROM bt_bars GROUP BY symbol,interval) b
                ON i.symbol=b.symbol ORDER BY i.root,i.symbol,b.interval''')]

    def bars(self,symbol,interval,start,end):
        with self.lock:
            return [dict(r) for r in self.db.execute('SELECT time,open,high,low,close,volume FROM bt_bars WHERE symbol=? AND interval=? AND time>=? AND time<=? ORDER BY time',(symbol,interval,start,end))]

    def instrument(self,symbol):
        with self.lock:
            r=self.db.execute('SELECT metadata FROM bt_instruments WHERE symbol=?',(symbol,)).fetchone()
            if not r:raise ValidationError('本地没有该合约，请先拉取K线')
            return json.loads(r[0])

    def job(self,jid):
        with self.lock:
            r=self.db.execute('SELECT * FROM bt_jobs WHERE id=?',(jid,)).fetchone()
            if not r:raise ValidationError('任务不存在',404)
            return {**dict(r),'detail':json.loads(r['detail'])}

    def save_job(self,jid,kind,status,detail):
        with self.lock,self.db:
            self.db.execute('INSERT OR REPLACE INTO bt_jobs VALUES(?,?,?,?,?)',(jid,kind,status,json.dumps(detail,ensure_ascii=False,allow_nan=False),time.time()))

    def save_run(self,jid,result):
        with self.lock,self.db:self.db.execute('INSERT INTO bt_runs VALUES(?,?,?)',(jid,time.time(),json.dumps(result,ensure_ascii=False,allow_nan=False)))

    def save_details(self,jid,rows):
        with self.lock,self.db:
            self.db.executemany('INSERT OR REPLACE INTO bt_details VALUES(?,?,?,?,?)',
                [(jid,kind,row['id'],row.get('phase'),json.dumps(row,ensure_ascii=False,allow_nan=False)) for kind,row in rows])

    def details(self,jid,kind,offset=0,limit=100,phase=None):
        if kind not in ('trades','accounts','claims'):raise ValidationError('明细类型无效')
        if offset<0 or not 1<=limit<=500:raise ValidationError('明细分页参数无效')
        clause='run_id=? AND kind=?';args=[jid,kind]
        if phase:
            if phase not in ('1/2','2/2','live'):raise ValidationError('明细阶段无效')
            clause+=' AND phase=?';args.append(phase)
        with self.lock:
            total=self.db.execute('SELECT COUNT(*) FROM bt_details WHERE '+clause,args).fetchone()[0]
            rows=self.db.execute('SELECT payload FROM bt_details WHERE '+clause+' ORDER BY seq DESC LIMIT ? OFFSET ?',[*args,limit,offset]).fetchall()
        if total:return dict(total=total,offset=offset,limit=limit,rows=[json.loads(r[0]) for r in rows])
        result=self.run(jid)
        # Existing v1 results keep their full details in the result JSON.
        rows=[r for r in result.get(kind,[]) if not phase or r.get('phase')==phase]
        rows.reverse()
        return dict(total=len(rows),offset=offset,limit=limit,rows=rows[offset:offset+limit])

    def runs(self):
        with self.lock:
            rows=self.db.execute('SELECT id,created_at,result FROM bt_runs ORDER BY created_at DESC LIMIT 50').fetchall()
        return [{'id':r['id'],'created_at':r['created_at'],'config':json.loads(r['result'])['config'],'metrics':json.loads(r['result'])['metrics']} for r in rows]

    def save_batch(self,jid,config,summary,status='running'):
        now=time.time()
        with self.lock,self.db:
            self.db.execute('INSERT INTO bt_batches VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET updated_at=excluded.updated_at,status=excluded.status,summary=excluded.summary',
                (jid,now,now,status,json.dumps(config,ensure_ascii=False),json.dumps(summary,ensure_ascii=False,allow_nan=False)))
            self._history_name(jid,config)

    def save_batch_round(self,jid,index,result):
        with self.lock,self.db:self.db.execute('INSERT INTO bt_batch_rounds VALUES(?,?,?,?,?,?)',
            (jid,index,result['id'],result['config']['seed'],result['duration_seconds'],json.dumps(result['metrics'],allow_nan=False)))

    def reserve_round_seed(self,jid,index,seed):
        with self.lock,self.db:
            if self.db.execute('SELECT 1 FROM bt_round_seeds WHERE batch_id=? AND round_index=?',(jid,index)).fetchone():
                raise ValidationError('该轮种子已经分配，不能重复执行')
            try:self.db.execute('INSERT INTO bt_round_seeds VALUES(?,?,?)',(jid,index,seed))
            except sqlite3.IntegrityError:return False
        return True

    def batch(self,jid,offset=0,limit=100,*,summary_only=False):
        if offset<0 or not 1<=limit<=500:raise ValidationError('多轮分页参数无效')
        self._ensure_single_statistics(jid)
        self._upgrade_statistics(jid)
        with self.lock:
            row=self.db.execute('SELECT * FROM bt_batches WHERE id=?',(jid,)).fetchone()
            if not row:raise ValidationError('多轮测试不存在',404)
            total=self.db.execute('SELECT COUNT(*) FROM bt_batch_rounds WHERE batch_id=?',(jid,)).fetchone()[0]
            rounds=[] if summary_only else self.db.execute('SELECT * FROM bt_batch_rounds WHERE batch_id=? ORDER BY round_index LIMIT ? OFFSET ?',(jid,limit,offset)).fetchall()
        return {**dict(row),**self._history_fields(jid,row['created_at'],json.loads(row['config'])),'config':json.loads(row['config']),'summary':json.loads(row['summary']),'total':total,'offset':offset,'limit':limit,
            'rows':[{**dict(r),'metrics':json.loads(r['metrics'])} for r in rounds]}

    def batches(self):
        with self.lock:
            singles=self.db.execute('SELECT id,result FROM bt_runs r WHERE NOT EXISTS(SELECT 1 FROM bt_batch_rounds b WHERE b.run_id=r.id) ORDER BY created_at DESC LIMIT 20').fetchall()
        for row in singles:
            if not json.loads(row['result']).get('batch_id'):self._ensure_single_statistics(row['id'])
        with self.lock:rows=self.db.execute('SELECT * FROM bt_batches ORDER BY created_at DESC LIMIT 20').fetchall()
        records=[]
        for r in rows:
            summary=json.loads(r['summary']);summary.pop('cash_curves',None)
            records.append({**dict(r),**self._history_fields(r['id'],r['created_at'],json.loads(r['config'])),'config':json.loads(r['config']),'summary':summary})
        return records

    def _account_starts(self,result):
        if result['config'].get('account_count')!='infinity' or all('cash' in p for p in result.get('curve',[])):return None
        with self.lock:
            cursor=self.db.execute("SELECT payload FROM bt_details WHERE run_id=? AND kind='accounts'",(result['id'],));starts=[]
            for row in cursor:
                account=json.loads(row[0]);times=[p['entered_at'] for p in account.get('phase_records',{}).values() if p.get('entered_at') is not None]
                if times:starts.append(min(times))
            cursor.close()
        if starts:return starts
        return [min(p['entered_at'] for p in a.get('phase_records',{}).values() if p.get('entered_at') is not None) for a in result.get('accounts',[]) if any(p.get('entered_at') is not None for p in a.get('phase_records',{}).values())]

    def _ensure_single_statistics(self,jid):
        from .backtest_statistics import BatchStatistics
        with self.lock:
            if self.db.execute('SELECT 1 FROM bt_batches WHERE id=?',(jid,)).fetchone():return
            row=self.db.execute('SELECT * FROM bt_runs WHERE id=?',(jid,)).fetchone()
            if not row:return
            result=json.loads(row['result'])
            if result.get('batch_id'):return
            result.update(id=jid,round_index=0);stats=BatchStatistics();result=self._statistics_result(result);stats.add_result(result,self._account_starts(result))
            duration=result.get('duration_seconds');config={**result['config'],'rounds':1}
            summary=dict(requested_rounds=1,completed_rounds=1,parallel_workers=1,duration_seconds=duration,mean_round_seconds=duration,**stats.result())
            self.save_batch(jid,config,summary,'completed')
            self.save_batch_round(jid,0,{**result,'duration_seconds':duration or 0})
            with self.db:self.db.execute('UPDATE bt_batches SET created_at=?,updated_at=? WHERE id=?',(row['created_at'],row['created_at'],jid))

    def _upgrade_statistics(self,jid):
        from .backtest_statistics import BatchStatistics,STATISTICS_VERSION
        with self.lock:
            row=self.db.execute('SELECT summary FROM bt_batches WHERE id=?',(jid,)).fetchone()
            if not row:return
            summary=json.loads(row[0])
            if summary.get('statistics_version')==STATISTICS_VERSION:return
            cursor=self.db.execute('SELECT r.result,b.round_index,b.run_id FROM bt_batch_rounds b JOIN bt_runs r ON r.id=b.run_id WHERE b.batch_id=? ORDER BY b.round_index',(jid,))
        stats=BatchStatistics()
        try:
            while True:
                with self.lock:record=cursor.fetchone()
                if not record:break
                result=json.loads(record[0]);result.update(id=record['run_id'],round_index=record['round_index'])
                result=self._statistics_result(result);stats.add_result(result,self._account_starts(result))
        finally:
            with self.lock:cursor.close()
        summary.update(stats.result())
        with self.lock,self.db:self.db.execute('UPDATE bt_batches SET summary=? WHERE id=?',(json.dumps(summary,ensure_ascii=False,allow_nan=False),jid))

    def _statistics_result(self,result):
        from .backtest_settlement import settle_archived,POLICY
        from .backtest_expectancy import from_accounts,populate_metrics
        with self.lock:
            if (result.get('settlement') or {}).get('policy')!=POLICY:
                cursor=self.db.execute("SELECT payload FROM bt_details WHERE run_id=? AND kind='claims'",(result['id'],));first=cursor.fetchone()
                try:
                    def rows():
                        yield json.loads(first[0])
                        for row in cursor:yield json.loads(row[0])
                    result=settle_archived(result,rows() if first else result.get('claims',[]))
                finally:cursor.close()
            if result['metrics'].get('remaining_expectancy') is not None:return populate_metrics(result)
            cursor=self.db.execute("SELECT payload FROM bt_details WHERE run_id=? AND kind='accounts'",(result['id'],));first=cursor.fetchone()
            try:
                def accounts():
                    yield json.loads(first[0])
                    for row in cursor:yield json.loads(row[0])
                return from_accounts(result,accounts() if first else result.get('accounts',[]))
            finally:cursor.close()

    def save_timing(self,signature,seconds,kind='run'):
        with self.lock,self.db:
            self.db.execute('INSERT INTO bt_timings(signature,seconds,kind,measured_at) VALUES(?,?,?,?)',(signature,seconds,kind,time.time()))
            self.db.execute('DELETE FROM bt_timings WHERE signature=? AND id NOT IN (SELECT id FROM bt_timings WHERE signature=? ORDER BY id DESC LIMIT 20)',(signature,signature))

    def timings(self,signature):
        with self.lock:return [dict(r) for r in self.db.execute('SELECT seconds,kind FROM bt_timings WHERE signature=? ORDER BY id DESC LIMIT 20',(signature,))]

    def import_details(self,jid,path):
        with closing(sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True)) as source:
            rows=source.execute('SELECT run_id,kind,seq,phase,payload FROM bt_details WHERE run_id=?',(jid,))
            while chunk:=rows.fetchmany(1000):
                with self.lock,self.db:self.db.executemany('INSERT OR REPLACE INTO bt_details VALUES(?,?,?,?,?)',chunk)
        with closing(sqlite3.connect(path,timeout=30)) as source:
            with source:source.execute('DELETE FROM bt_details WHERE run_id=?',(jid,))

    def jobs(self):
        with self.lock:rows=self.db.execute('SELECT * FROM bt_jobs ORDER BY updated_at DESC LIMIT 10').fetchall()
        return [{**dict(r),'detail':json.loads(r['detail'])} for r in rows]

    def run(self,jid):
        with self.lock:r=self.db.execute('SELECT result FROM bt_runs WHERE id=?',(jid,)).fetchone()
        if not r:raise ValidationError('回测记录不存在',404)
        return json.loads(r[0])

    def setting(self,key,value=None):
        with self.lock:
            if value is not None:
                with self.db:self.db.execute('INSERT OR REPLACE INTO bt_settings VALUES(?,?)',(key,json.dumps(value,ensure_ascii=False,allow_nan=False)))
            row=self.db.execute('SELECT value FROM bt_settings WHERE key=?',(key,)).fetchone()
            return json.loads(row[0]) if row else None


def sync_history(store,bridge,config,progress=lambda d:None,clock=time.time):
    roots=config.get('roots',list(INSTRUMENTS))
    if not isinstance(roots,list) or not roots or any(not isinstance(r,str) or r not in INSTRUMENTS for r in roots) or len(set(roots))!=len(roots):raise ValidationError('历史品种列表无效')
    interval=config.get('interval',60)
    if interval not in (60,300,900,3600):raise ValidationError('历史周期只支持 1、5、15、60 分钟')
    start=config.get('from');end=config.get('to')
    if any(isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) for v in (start,end)) or not 0<start<end<=clock() or end-start>366*86400:
        raise ValidationError('历史日期范围需在过去一年以内，结束时间不能晚于当前时间')
    status=bridge.status()
    if status.get('connected') is not True:raise ValidationError('8090 行情桥未连接，未生成或填充行情',409)
    catalog=bridge.instruments();output=[];errors=[]
    window=7*86400 if status.get('historyWindowVersion')==1 else interval*480
    for index,root in enumerate(roots):
        try:
            inst=bridge.resolve(root,(config.get('contracts') or {}).get(root),catalog)
            count=0;cursor=int(start)
            while cursor<=end:
                until=min(int(end),cursor+window-1)
                raw=bridge.history(inst['symbol'],interval,cursor,until)
                if not isinstance(raw,list):raise ValidationError('行情桥未返回K线数组')
                bars=[validated_bar(b,cursor,until,interval,clock()) for b in raw]
                bars=[b for b in bars if b is not None]
                store.save_chunk(inst,interval,bars);count+=len(bars)
                progress(dict(root=root,symbol=inst['symbol'],completed=index,total=len(roots),bars=count,through=until))
                cursor=until+1
            if not count:raise ValidationError(root+' 在该区间没有返回已完成K线；请检查数据源和月份')
            output.append(dict(root=root,symbol=inst['symbol'],bars_received=count))
        except (ValidationError,OSError) as exc:errors.append(dict(root=root,error=str(exc)))
    return dict(status='completed' if not errors else 'partial' if output else 'error',instruments=output,errors=errors,
        source='NT8 GET history @127.0.0.1:8090',interval=interval,**{'from':start,'to':end})
