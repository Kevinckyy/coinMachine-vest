"""Audited public MNQ candles and a locally constructed continuous series."""
import argparse
import csv
import gzip
import hashlib
import io
import json
import math
import re
import sqlite3
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor,as_completed
from contextlib import closing
from datetime import datetime,date,timedelta,timezone
from pathlib import Path
from functools import lru_cache
from .market_history import BacktestStore,ReadOnlyHistoryBridge,validated_bar
from .trading_day import NEW_YORK

REPOSITORY='mbytes21/MNQ_DATA'
SOURCE_COMMIT='fc5508e2c152938d6d9eb70a36b888ae26107176'
ROOT=Path(__file__).resolve().parents[1]
DATA_ROOT=ROOT/'runtime/market-data/mnq'


def fetch(url,limit=5_000_000):
    request=urllib.request.Request(url,headers={'User-Agent':'VestLocalHistoricalResearch/1.0'})
    with urllib.request.urlopen(request,timeout=45) as response:
        raw=response.read(limit+1)
        if len(raw)>limit:raise ValueError('公开数据响应超过大小限制')
        return raw


def download_public():
    DATA_ROOT.mkdir(parents=True,exist_ok=True)
    tree_path=DATA_ROOT/'source-tree.json'
    if not tree_path.exists():tree_path.write_bytes(fetch('https://api.github.com/repos/'+REPOSITORY+'/git/trees/'+SOURCE_COMMIT+'?recursive=1'))
    tree=json.loads(tree_path.read_bytes())
    if tree.get('truncated'):raise ValueError('公开文件目录不完整')
    selected={}
    for row in tree['tree']:
        match=re.fullmatch(r'plaintext_csv/(?:plaintext_csv/)?(MNQ (?:03|06|09|12)-\d{2})/(\d{8}\.Last\.csv)',row['path'])
        if row['type']!='blob' or not match:continue
        key=match[1]+'/'+match[2]
        if key in selected and selected[key]['sha']!=row['sha']:raise ValueError('公开源的重复文件内容冲突')
        if key not in selected or len(row['path'])<len(selected[key]['path']):selected[key]=row
    if not selected:raise ValueError('未发现公开 MNQ 成交 K线')
    def one(item):
        key,row=item;target=DATA_ROOT/'raw'/(key+'.gz');target.parent.mkdir(parents=True,exist_ok=True)
        for attempt in range(5):
            try:
                raw=gzip.decompress(target.read_bytes()) if target.exists() else fetch('https://raw.githubusercontent.com/'+REPOSITORY+'/'+SOURCE_COMMIT+'/'+urllib.parse.quote(row['path']),2_000_000)
                digest=hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest()
                if digest!=row['sha']:raise ValueError('公开文件 Git blob 校验失败：'+key)
                if not target.exists():target.write_bytes(gzip.compress(raw,compresslevel=6,mtime=0))
                return dict(contract=key.split('/')[0],file=key.split('/')[1],path=str(target.relative_to(DATA_ROOT)),git_sha=row['sha'],sha256=hashlib.sha256(raw).hexdigest(),bytes=len(raw))
            except Exception:
                if attempt==4:raise
                time.sleep(.4*2**attempt)
    rows=[]
    with ThreadPoolExecutor(max_workers=10) as pool:
        futures=[pool.submit(one,item) for item in selected.items()]
        for future in as_completed(futures):
            rows.append(future.result())
            if len(rows)%100==0:print(json.dumps({'stage':'download','completed':len(rows),'total':len(selected)}),flush=True)
    provenance=dict(repository='https://github.com/'+REPOSITORY,commit=SOURCE_COMMIT,downloaded_at=time.time(),
        timezone='America/New_York',timestamp_convention='minute end; converted to UTC bar open by subtracting 60 seconds',
        verification=dict(contract='MNQ 03-26',date='2026-01-20',overlapping_bars=1379,exact_ohlc_matches=1379,reference='GET-only NT8 bridge @127.0.0.1:8090'),files=sorted(rows,key=lambda r:(r['contract'],r['file'])))
    (DATA_ROOT/'provenance.json').write_text(json.dumps(provenance,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'stage':'download_completed','files':len(rows),'uncompressed_bytes':sum(r['bytes'] for r in rows)},ensure_ascii=False),flush=True)
    return provenance


@lru_cache(None)
def ny_bounds(year):
    march=date(year,3,1);november=date(year,11,1)
    start=march+timedelta(days=(6-march.weekday())%7+7);end=november+timedelta(days=(6-november.weekday())%7)
    return datetime.combine(start,datetime.min.time()).replace(hour=2),datetime.combine(end,datetime.min.time()).replace(hour=2)


def ny_timestamp(local):
    start,end=ny_bounds(local.year)
    if start<=local<start+timedelta(hours=1) or end-timedelta(hours=1)<=local<end:raise ValueError('纽约夏令时歧义或不存在的时间')
    if NEW_YORK is not None:return int(local.replace(tzinfo=NEW_YORK).timestamp())
    return int(local.replace(tzinfo=timezone.utc).timestamp())+(4 if start<=local<end else 5)*3600


def ny_local(ts):
    utc=datetime.fromtimestamp(ts,timezone.utc)
    if NEW_YORK is not None:return utc.astimezone(NEW_YORK).replace(tzinfo=None)
    start,end=ny_bounds(utc.year)
    dst=int(start.replace(tzinfo=timezone.utc).timestamp())+5*3600<=ts<int(end.replace(tzinfo=timezone.utc).timestamp())+4*3600
    return (utc+timedelta(hours=-4 if dst else -5)).replace(tzinfo=None)


def session_key(local):return (local.date() if local.hour>=18 else local.date()-timedelta(days=1)).isoformat()


def market_open(local):
    return local.weekday()!=5 and not (local.weekday()==6 and local.hour<18) and not (local.weekday()==4 and local.hour>=17) and local.hour!=17


def open_source_db():
    db=sqlite3.connect(DATA_ROOT/'contracts.sqlite');db.row_factory=sqlite3.Row
    db.execute('PRAGMA journal_mode=WAL');db.execute('PRAGMA synchronous=NORMAL')
    db.executescript('''CREATE TABLE IF NOT EXISTS candles(contract TEXT,time INTEGER,session TEXT,open REAL,high REAL,low REAL,close REAL,volume REAL,source TEXT,PRIMARY KEY(contract,time));
        CREATE INDEX IF NOT EXISTS candle_sessions ON candles(contract,session,time);
        CREATE TABLE IF NOT EXISTS imported_files(sha TEXT PRIMARY KEY,rows INTEGER);
        CREATE TABLE IF NOT EXISTS bridge_chunks(contract TEXT,start INTEGER,end INTEGER,rows INTEGER,PRIMARY KEY(contract,start,end));''')
    return db


def import_public(db):
    manifest=json.loads((DATA_ROOT/'provenance.json').read_text(encoding='utf-8'));now=time.time()
    for index,row in enumerate(manifest['files']):
        if db.execute('SELECT 1 FROM imported_files WHERE sha=?',(row['git_sha'],)).fetchone():continue
        raw=gzip.decompress((DATA_ROOT/row['path']).read_bytes())
        if hashlib.sha256(raw).hexdigest()!=row['sha256']:raise ValueError('本地公开源校验失败')
        records=[]
        for item in csv.DictReader(io.StringIO(raw.decode('utf-8-sig'))):
            end=datetime.fromisoformat(item['datetime']);ts=ny_timestamp(end)-60
            bar=validated_bar(dict(time=ts,**{k:float(item[k]) for k in ('open','high','low','close','volume')}),ts,ts,60,now)
            if bar:records.append((row['contract'],ts,session_key(end-timedelta(minutes=1)),bar['open'],bar['high'],bar['low'],bar['close'],bar['volume'],'github:'+row['git_sha']))
        with db:
            db.executemany('INSERT OR REPLACE INTO candles VALUES(?,?,?,?,?,?,?,?,?)',records)
            db.execute('INSERT INTO imported_files VALUES(?,?)',(row['git_sha'],len(records)))
        if index%100==0:print(json.dumps({'stage':'import_public','completed':index+1,'total':len(manifest['files'])}),flush=True)


def import_bridge_tail(db):
    bridge=ReadOnlyHistoryBridge();now=int(time.time()//60)*60-60
    windows=[('MNQ 03-26',date(2026,2,1),date(2026,3,21)),('MNQ 06-26',date(2026,2,1),date(2026,6,20)),
        ('MNQ 09-26',date(2026,5,1),date(2026,9,19)),('MNQ 12-26',date(2026,8,1),datetime.fromtimestamp(now,timezone.utc).date()+timedelta(days=1))]
    for contract,start,end in windows:
        bridge.resolve('MNQ',contract);cursor=int(datetime.combine(start,datetime.min.time(),timezone.utc).timestamp())
        final=min(now,int(datetime.combine(end,datetime.min.time(),timezone.utc).timestamp()))
        while cursor<final:
            until=min(cursor+86400-1,final);day=datetime.fromtimestamp(cursor,timezone.utc).weekday()
            present=db.execute('SELECT COUNT(*) FROM candles WHERE contract=? AND time>=? AND time<=?',(contract,cursor,until)).fetchone()[0]
            threshold=60 if day==6 else 1100 if day==4 else 1300
            known=db.execute('SELECT 1 FROM bridge_chunks WHERE contract=? AND start=? AND end=?',(contract,cursor,until)).fetchone()
            if day!=5 and present<threshold and not known:
                records=[]
                for raw in bridge.history(contract,60,cursor,until):
                    b=validated_bar(raw,cursor,until,60,time.time())
                    if b:records.append((contract,b['time'],session_key(ny_local(b['time'])),b['open'],b['high'],b['low'],b['close'],b['volume'],'nt8-get-history'))
                with db:
                    db.executemany('INSERT OR REPLACE INTO candles VALUES(?,?,?,?,?,?,?,?,?)',records)
                    db.execute('INSERT INTO bridge_chunks VALUES(?,?,?,?)',(contract,cursor,until,len(records)))
                print(json.dumps({'stage':'bridge_tail','contract':contract,'through':until,'bars':len(records)}),flush=True)
            cursor=until+1


def expiry(contract):
    month,year=map(int,contract.split()[1].split('-'));day=date(2000+year,month,1)
    return day+timedelta(days=(4-day.weekday())%7+14)


def build_continuous(db,cache_path):
    contracts=[r[0] for r in db.execute('SELECT DISTINCT contract FROM candles')];contracts.sort(key=expiry)
    rolls=[]
    for old,new in zip(contracts,contracts[1:]):
        a,b=expiry(old),expiry(new)
        if (b.year-a.year)*12+b.month-a.month!=3:raise ValueError('季度合约缺失：'+old+' → '+new)
        old_end=expiry(old);volumes={}
        for symbol in (old,new):volumes[symbol]={r['session']:r['v'] for r in db.execute('SELECT session,SUM(volume) v FROM candles WHERE contract=? AND session>=? AND session<=? GROUP BY session',(symbol,(old_end-timedelta(days=30)).isoformat(),old_end.isoformat()))}
        counts={symbol:{r['session']:r['n'] for r in db.execute('SELECT session,COUNT(*) n FROM candles WHERE contract=? GROUP BY session',(symbol,))} for symbol in (old,new)}
        candidates=[s for s in sorted(volumes[old].keys()&volumes[new].keys()) if volumes[old][s]>0 and volumes[new][s]>volumes[old][s]
            and (counts[old].get(s,0)>=1000 and counts[new].get(s,0)>=1000)]
        rule='completed_session_volume_crossover'
        if candidates:
            decision=candidates[0]
            following=next((r[0] for r in db.execute('SELECT DISTINCT session FROM candles WHERE contract=? AND session>? ORDER BY session',(new,decision)) if date.fromisoformat(r[0]).weekday() not in (4,5)),None)
            if following is None:raise ValueError('换月后没有新合约数据：'+new)
            switch=ny_timestamp(datetime.fromisoformat(following).replace(hour=18))
            ref=db.execute('SELECT o.time,o.close old_close,n.close new_close FROM candles o JOIN candles n ON n.contract=? AND n.time=o.time WHERE o.contract=? AND o.session=? ORDER BY o.time DESC LIMIT 1',(new,old,decision)).fetchone()
            earlier=min(db.execute('SELECT MAX(time) FROM candles WHERE contract=? AND session=?',(s,decision)).fetchone()[0] for s in (old,new))
        else:
            # Many public exports stop the outgoing file at their own rollover,
            # before a full crossover session exists. Preserve that observed
            # handoff and label it separately; do not invent volume evidence.
            last=next((r[0] for r in db.execute('SELECT time FROM candles WHERE contract=? ORDER BY time DESC LIMIT 100',(old,)) if market_open(ny_local(r[0]))),None)
            if last is None or not old_end-timedelta(days=14)<=ny_local(last).date()<=old_end:raise ValueError('缺少可验证的换月覆盖交接：'+old+' → '+new)
            switch=last+60;decision=session_key(ny_local(last));rule='observed_source_coverage_handoff'
            ref=db.execute('SELECT o.time,o.close old_close,n.close new_close FROM candles o JOIN candles n ON n.contract=? AND n.time=o.time WHERE o.contract=? AND o.time<=? ORDER BY o.time DESC LIMIT 1',(new,old,last)).fetchone();earlier=last
        if not ref:raise ValueError('换月缺少同一时点的价格：'+old)
        max_age=3600 if rule=='observed_source_coverage_handoff' else 60
        if ref['time']<earlier-max_age:raise ValueError('换月参考价格过旧：'+old)
        rolls.append(dict(outgoing=old,incoming=new,rule=rule,decision_session=decision,switch_utc=switch,reference_utc=ref['time'],
            reference_age_seconds=earlier-ref['time'],outgoing_volume=volumes[old].get(decision),incoming_volume=volumes[new].get(decision),old_close=ref['old_close'],new_close=ref['new_close'],difference=ref['new_close']-ref['old_close']))
    adjustments={contracts[-1]:0.}
    for roll in reversed(rolls):adjustments[roll['outgoing']]=adjustments[roll['incoming']]+roll['difference'];roll['back_adjustment']=adjustments[roll['outgoing']]
    metadata=dict(root='MNQ',symbol='MNQ CONTINUOUS BA',point_value=2,tick_size=.25,continuous=True,
        adjustment='additive_back_adjusted',roll_rule='completed-session volume crossover where observed; otherwise audited outgoing coverage handoff near expiry',
        source_description='公开 MNQ 分月成交数据 + NT8 只读历史；本地换月、加法后复权连续序列（换月证据见记录）',
        source_url='https://github.com/'+REPOSITORY,source_commit=SOURCE_COMMIT,roll_count=len(rolls),provenance=str(DATA_ROOT/'provenance.json'),rolls=str(DATA_ROOT/'rolls.json'))
    (DATA_ROOT/'rolls.json').write_text(json.dumps(rolls,ensure_ascii=False,indent=2),encoding='utf-8')
    export=DATA_ROOT/'MNQ_CONTINUOUS_BACKADJUSTED_1m.csv.gz';store=BacktestStore(cache_path);count=0;first=None;last=None;chunk=[]
    try:
        with gzip.open(export,'wt',encoding='utf-8',newline='') as handle:
            writer=csv.writer(handle);writer.writerow(('time_utc','open','high','low','close','volume','source_contract','back_adjustment'))
            for i,contract in enumerate(contracts):
                start=rolls[i-1]['switch_utc'] if i else 0;end=rolls[i]['switch_utc'] if i<len(rolls) else 2**63-1
                adjustment=adjustments[contract]
                for raw in db.execute('SELECT * FROM candles WHERE contract=? AND time>=? AND time<? ORDER BY time',(contract,start,end)):
                    b={k:raw[k] for k in ('time','open','high','low','close','volume')}
                    if not market_open(ny_local(b['time'])):continue
                    for key in ('open','high','low','close'):b[key]=round(b[key]+adjustment,8)
                    if min(b[k] for k in ('open','high','low','close'))<=0:raise ValueError('复权后价格非正')
                    if last is not None and b['time']<=last:raise ValueError('连续数据时间未严格递增')
                    first=b['time'] if first is None else first;last=b['time'];count+=1;chunk.append(b)
                    writer.writerow((datetime.fromtimestamp(b['time'],timezone.utc).isoformat(),b['open'],b['high'],b['low'],b['close'],b['volume'],contract,adjustment))
                    if len(chunk)>=10000:store.save_chunk(metadata,60,chunk);chunk=[]
                print(json.dumps({'stage':'continuous','contract':contract,'bars':count}),flush=True)
            if chunk:store.save_chunk(metadata,60,chunk)
        report=dict(**metadata,bars=count,first_time=first,last_time=last,export=str(export),sha256=hashlib.sha256(export.read_bytes()).hexdigest())
        (DATA_ROOT/'continuous-manifest.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps({'stage':'completed','bars':count,'first_time':first,'last_time':last,'rolls':len(rolls),'export':str(export)},ensure_ascii=False),flush=True)
    finally:store.close()


def audit_continuous(cache_path):
    report=json.loads((DATA_ROOT/'continuous-manifest.json').read_text(encoding='utf-8'));sessions={};boundary=0;current=None;last=None;gaps=0
    with closing(sqlite3.connect(Path(cache_path).resolve().as_uri()+'?mode=ro',uri=True)) as db:
        invalid=db.execute("SELECT COUNT(*) FROM bt_bars WHERE symbol=? AND interval=60 AND (open<=0 OR low>MIN(open,close) OR high<MAX(open,close) OR time%60!=0 OR ABS(open*4-ROUND(open*4))>.00001 OR ABS(close*4-ROUND(close*4))>.00001)",('MNQ CONTINUOUS BA',)).fetchone()[0]
        if invalid:raise ValueError('连续数据 OHLC、时间或最小跳动校验失败')
        count=0
        for (t,) in db.execute('SELECT time FROM bt_bars WHERE symbol=? AND interval=60 ORDER BY time',('MNQ CONTINUOUS BA',)):
            if t>=boundary:
                current=date.fromisoformat(session_key(ny_local(t)));boundary=ny_timestamp(datetime.combine(current+timedelta(days=1),datetime.min.time()).replace(hour=18))
            sessions[current]=sessions.get(current,0)+1
            if last is not None:gaps+=t-last>60
            last=t;count+=1
    if count!=report['bars']:raise ValueError('缓存与导出根数不符')
    missing=[];day=min(sessions);end=max(sessions)
    while day<=end:
        if day.weekday() not in (4,5) and day not in sessions:missing.append(day.isoformat())
        day+=timedelta(days=1)
    quality=dict(bars=count,invalid_bars=invalid,time_gaps=gaps,empty_weekday_sessions_including_holidays=missing,
        sparse_sessions_including_short_days=[dict(session=d.isoformat(),bars=n) for d,n in sessions.items() if n<1000],
        note='保留真实缺口，不补造价格；空交易日包括节假日及源数据缺失，稀疏时段可能为短交易日或低流动性。')
    (DATA_ROOT/'quality-report.json').write_text(json.dumps(quality,ensure_ascii=False,indent=2),encoding='utf-8')
    report['quality_report']=str(DATA_ROOT/'quality-report.json');report['quality_summary']=dict(invalid_bars=invalid,empty_sessions=len(missing),sparse_sessions=len(quality['sparse_sessions_including_short_days']))
    (DATA_ROOT/'continuous-manifest.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'stage':'audit_completed',**report['quality_summary'],'bars':count}),flush=True)


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--download-only',action='store_true');parser.add_argument('--build-only',action='store_true');parser.add_argument('--with-bridge-tail',action='store_true');parser.add_argument('--db',default=str(ROOT/'runtime/backtests.sqlite'));args=parser.parse_args()
    if not args.build_only:download_public()
    if args.download_only:return
    with closing(open_source_db()) as db:
        import_public(db)
        if args.with_bridge_tail:import_bridge_tail(db)
        build_continuous(db,args.db)
    audit_continuous(args.db)


if __name__=='__main__':main()
