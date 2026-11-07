"""Bounded time buckets over all closed trades, plus paged execution timestamps."""
import heapq
import json
import math
import re
import hashlib
from datetime import date,datetime,timedelta,timezone
from functools import lru_cache

from .engine import ValidationError
from .trading_day import NEW_YORK

TIME_VERSION=1
ZONES=('America/New_York','Asia/Shanghai')
AXES=('hour','weekday','week','month')


def normalize_zone(zone):
    if zone in ZONES:return zone
    match=re.fullmatch(r'UTC([+-])(\d{1,2})(?::([0-5]\d))?',zone) if isinstance(zone,str) else None
    if not match:raise ValidationError('时区需为美东、北京时间或 UTC±HH:MM')
    minutes=(int(match[2])*60+int(match[3] or 0))*(1 if match[1]=='+' else -1)
    if not -720<=minutes<=840 or minutes%15:raise ValidationError('UTC偏移需在 -12 至 +14 小时之间，以15分钟为一档')
    return f"UTC{'+' if minutes>=0 else '-'}{abs(minutes)//60:02}:{abs(minutes)%60:02}"


@lru_cache(maxsize=128)
def fixed_offset(zone):
    normalized=normalize_zone(zone)
    if normalized in ZONES:return None
    sign=1 if normalized[3]=='+' else -1
    return sign*(int(normalized[4:6])*60+int(normalized[7:9]))


def local_time(ts,zone):
    utc=datetime.fromtimestamp(ts,timezone.utc)
    if zone=='Asia/Shanghai':return utc.astimezone(timezone(timedelta(hours=8)))
    if zone!='America/New_York':return utc.astimezone(timezone(timedelta(minutes=fixed_offset(zone))))
    if NEW_YORK is not None:return utc.astimezone(NEW_YORK)
    # US transitions happen at 07:00 UTC in March and 06:00 UTC in November.
    march=date(utc.year,3,1);november=date(utc.year,11,1)
    start=march+timedelta(days=(6-march.weekday())%7+7)
    end=november+timedelta(days=(6-november.weekday())%7)
    dst=datetime.combine(start,datetime.min.time(),timezone.utc)+timedelta(hours=7)<=utc<datetime.combine(end,datetime.min.time(),timezone.utc)+timedelta(hours=6)
    return utc.astimezone(timezone(timedelta(hours=-4 if dst else -5)))


def valid_trade(trade):
    values=[trade.get(k) for k in ('opened_at','closed_at','pnl')]
    return all(isinstance(v,(int,float)) and not isinstance(v,bool) and math.isfinite(v) for v in values) and 0<values[0]<=values[1]<=253402300799


def bucket():return dict(trades=0,wins=0,pnl=0.,holding_seconds=0.,closes=0)


@lru_cache(maxsize=4096)
def time_keys(ts,zone):
    local=local_time(ts,zone)
    return str(local.hour),str(local.weekday()),(local.date()-timedelta(days=local.weekday())).isoformat(),local.strftime('%Y-%m')


class RoundTimeStatistics:
    def __init__(self,zones=ZONES):self.zones={zone:{axis:{} for axis in AXES} for zone in zones};self.count=0;self.grid={}

    def add(self,trade):
        if not valid_trade(trade):return
        self.count+=1;opened=trade['opened_at'];closed=trade['closed_at'];pnl=trade['pnl']
        won=trade['is_win'] if isinstance(trade.get('is_win'),bool) else pnl>0
        start=self.grid.setdefault(math.floor(opened/900)*900,[0,0,0.,0.,0]);start[0]+=1;start[1]+=won;start[2]+=pnl;start[3]+=closed-opened
        self.grid.setdefault(math.floor(closed/900)*900,[0,0,0.,0.,0])[4]+=1
        for zone,axes in self.zones.items():
            keys=time_keys(opened,zone)
            for axis,key in zip(AXES,keys):
                r=axes[axis].setdefault(key,bucket());r['trades']+=1;r['wins']+=won;r['pnl']+=pnl;r['holding_seconds']+=closed-opened
            axes['hour'].setdefault(time_keys(closed,zone)[0],bucket())['closes']+=1

    def result(self,expected,start=None,end=None):
        return dict(version=TIME_VERSION,complete=self.count==expected,expected_trades=expected,recorded_trades=self.count,
            start=start,end=end,zones=self.zones,utc_grid=[[ts,*self.grid[ts]] for ts in sorted(self.grid)])


def packet_from_grid(packet,zone):
    stats=RoundTimeStatistics((zone,))
    for ts,n,wins,pnl,held,closes in packet['utc_grid']:
        keys=time_keys(ts,zone)
        if n:
            for axis,key in zip(AXES,keys):
                row=stats.zones[zone][axis].setdefault(key,bucket());row['trades']+=n;row['wins']+=wins;row['pnl']+=pnl;row['holding_seconds']+=held
        if closes:stats.zones[zone]['hour'].setdefault(keys[0],bucket())['closes']+=closes
    return {key:packet[key] for key in ('version','complete','expected_trades','recorded_trades','start','end')}|dict(zones=stats.zones)


def from_trades(result,trades):
    stats=RoundTimeStatistics()
    for trade in trades:stats.add(trade)
    dataset=result.get('dataset') or {};c=result['config']
    start=dataset.get('first_time',c.get('from'));end=(dataset['last_time']+dataset.get('interval',c.get('interval',60))) if dataset.get('last_time') is not None else c.get('to')
    return stats.result(result['metrics'].get('total_trades',0),start,end)


def calendar_keys(axis,start,end,zone):
    if axis=='hour':return [str(i) for i in range(24)]
    if axis=='weekday':return [str(i) for i in range(7)]
    if start is None or end is None or end<=start:return []
    first=local_time(start,zone).date();last=local_time(max(start,end-1e-6),zone).date()
    if axis=='week':
        first-=timedelta(days=first.weekday());last-=timedelta(days=last.weekday())
        return [(first+timedelta(days=i*7)).isoformat() for i in range((last-first).days//7+1)]
    return [f'{n//12:04}-{n%12+1:02}' for n in range(first.year*12+first.month-1,last.year*12+last.month)]


class TimeSummary:
    def __init__(self,zones=ZONES):
        self.rounds=0;self.complete=0;self.expected=0;self.recorded=0;self.start=None;self.end=None;self.rate_rounds=0
        self.zones={zone:{axis:{} for axis in AXES} for zone in zones}

    def add(self,packet):
        self.rounds+=1;self.expected+=packet.get('expected_trades',0);self.recorded+=packet.get('recorded_trades',0)
        if not packet.get('complete'):return
        self.complete+=1;start=packet.get('start');end=packet.get('end')
        days=(end-start)/86400 if start is not None and end is not None and end>start else None
        if days:self.rate_rounds+=1
        if start is not None:self.start=min(self.start,start) if self.start is not None else start
        if end is not None:self.end=max(self.end,end) if self.end is not None else end
        for zone,axes in packet['zones'].items():
            for axis,rows in axes.items():
                for key,r in rows.items():
                    target=self.zones[zone][axis].setdefault(key,dict(**bucket(),active_rounds=0,win_rate_sum=0.,daily_opens_sum=0.,daily_closes_sum=0.))
                    for field in bucket():target[field]+=r[field]
                    if r['trades']:target['active_rounds']+=1;target['win_rate_sum']+=r['wins']/r['trades']*100
                    if axis=='hour' and days:target['daily_opens_sum']+=r['trades']/days;target['daily_closes_sum']+=r['closes']/days

    def result(self):
        zones={}
        for zone,axes in self.zones.items():
            zones[zone]={}
            for axis,rows in axes.items():
                keys=set(calendar_keys(axis,self.start,self.end,zone))|set(rows)
                ordered=sorted(keys,key=int if axis in ('hour','weekday') else None)
                out=[]
                for key in ordered:
                    r=rows.get(key,dict(**bucket(),active_rounds=0,win_rate_sum=0.,daily_opens_sum=0.,daily_closes_sum=0.));n=r['trades'];active=r['active_rounds']
                    out.append(dict(key=key,trades=n,wins=r['wins'],active_rounds=active,total_pnl=round(r['pnl'],8),
                        mean_round_pnl=r['pnl']/self.complete if self.complete else None,avg_trade_pnl=r['pnl']/n if n else None,
                        pooled_win_rate=r['wins']/n*100 if n else None,mean_round_win_rate=r['win_rate_sum']/active if active else None,
                        avg_holding_seconds=r['holding_seconds']/n if n else None,total_holding_seconds=r['holding_seconds'],closes=r['closes'],
                        mean_opens=n/self.complete if self.complete else None,mean_closes=r['closes']/self.complete if self.complete else None,
                        daily_opens=r['daily_opens_sum']/self.complete if self.rate_rounds==self.complete and self.complete else None,
                        daily_closes=r['daily_closes_sum']/self.complete if self.rate_rounds==self.complete and self.complete else None))
                zones[zone][axis]=out
        return dict(version=TIME_VERSION,available=self.complete>0,complete_rounds=self.complete,incomplete_rounds=self.rounds-self.complete,
            expected_trades=self.expected,recorded_trades=self.recorded,start=self.start,end=self.end,basis='opened_at',zones=zones)


class BacktestTimeStore:
    def _utc_grid(self,jid):
        with self.lock:
            cached=self.db.execute('SELECT payload FROM bt_time_grids WHERE run_id=?',(jid,)).fetchone()
            if cached:return json.loads(cached[0])
            row=self.db.execute("""SELECT json_extract(result,'$.time_statistics.utc_grid') AS grid,
                json_extract(result,'$.metrics.total_trades') AS expected,
                json_extract(result,'$.dataset.first_time') AS start,
                json_extract(result,'$.dataset.last_time') AS last,
                json_extract(result,'$.config') AS config FROM bt_runs WHERE id=?""",(jid,)).fetchone()
            if row is None:raise ValidationError('回测记录不存在',404)
            c=json.loads(row['config']);start=row['start'] if row['start'] is not None else c.get('from')
            end=row['last']+c.get('interval',60) if row['last'] is not None else c.get('to');expected=row['expected'] or 0
            if row['grid'] is not None:grid=json.loads(row['grid'])
            else:
                count=self.db.execute("SELECT COUNT(*) FROM bt_details WHERE run_id=? AND kind='trades'",(jid,)).fetchone()[0]
                if not count:
                    packet=from_trades(self.run(jid),self.run(jid).get('trades',[]));grid=packet['utc_grid']
                else:
                    # Derive a universal 15-minute UTC grid once. Any fixed offset
                    # on this lattice can then be regrouped without replaying fills.
                    where="""run_id=? AND kind='trades'
                        AND json_type(payload,'$.opened_at') IN ('integer','real')
                        AND json_type(payload,'$.closed_at') IN ('integer','real')
                        AND json_type(payload,'$.pnl') IN ('integer','real')
                        AND json_extract(payload,'$.opened_at')>0
                        AND json_extract(payload,'$.closed_at')>=json_extract(payload,'$.opened_at')
                        AND json_extract(payload,'$.closed_at')<=253402300799
                        AND abs(json_extract(payload,'$.pnl'))<=1.7976931348623157e308"""
                    opens=self.db.execute("""SELECT CAST(json_extract(payload,'$.opened_at')/900 AS INTEGER)*900 AS ts,
                        COUNT(*),SUM(CASE WHEN json_type(payload,'$.is_win') IN ('true','false') THEN json_extract(payload,'$.is_win') ELSE json_extract(payload,'$.pnl')>0 END),
                        SUM(json_extract(payload,'$.pnl')),SUM(json_extract(payload,'$.closed_at')-json_extract(payload,'$.opened_at'))
                        FROM bt_details WHERE """+where+' GROUP BY ts',(jid,)).fetchall()
                    cells={r[0]:[r[1],r[2],r[3],r[4],0] for r in opens}
                    closes=self.db.execute("SELECT CAST(json_extract(payload,'$.closed_at')/900 AS INTEGER)*900 AS ts,COUNT(*) FROM bt_details WHERE "+where+' GROUP BY ts',(jid,)).fetchall()
                    for ts,n in closes:cells.setdefault(ts,[0,0,0.,0.,0])[4]=n
                    grid=[[ts,*cells[ts]] for ts in sorted(cells)]
            recorded=sum(r[1] for r in grid)
            packet=dict(version=TIME_VERSION,complete=recorded==expected,expected_trades=expected,recorded_trades=recorded,start=start,end=end,utc_grid=grid)
            with self.db:self.db.execute('INSERT OR REPLACE INTO bt_time_grids VALUES(?,?)',(jid,json.dumps(packet,allow_nan=False)))
            return packet

    def zone_statistics(self,jid,zone):
        zone=normalize_zone(zone);self._ensure_single_statistics(jid)
        with self.lock:
            if not self.db.execute('SELECT 1 FROM bt_batches WHERE id=?',(jid,)).fetchone():raise ValidationError('回测历史记录不存在',404)
            runs=self.db.execute('SELECT run_id,round_index FROM bt_batch_rounds WHERE batch_id=? ORDER BY round_index',(jid,)).fetchall()
            revision=hashlib.sha256(json.dumps([tuple(r) for r in runs]).encode()).hexdigest()
            cached=self.db.execute('SELECT revision,payload FROM bt_time_zones WHERE batch_id=? AND zone=?',(jid,zone)).fetchone()
            if cached and cached['revision']==revision:return json.loads(cached['payload'])
        stats=TimeSummary((zone,))
        for run in runs:stats.add(packet_from_grid(self._utc_grid(run['run_id']),zone))
        result=stats.result()
        with self.lock,self.db:self.db.execute('INSERT OR REPLACE INTO bt_time_zones VALUES(?,?,?,?)',(jid,zone,revision,json.dumps(result,allow_nan=False)))
        return result

    def _time_result(self,result):
        if (result.get('time_statistics') or {}).get('version')==TIME_VERSION:return result
        jid=result['id']
        with self.lock:
            row=self.db.execute('SELECT payload FROM bt_time_statistics WHERE run_id=? AND version=?',(jid,TIME_VERSION)).fetchone()
            if row:return {**result,'time_statistics':json.loads(row[0])}
            cursor=self.db.execute("SELECT payload FROM bt_details WHERE run_id=? AND kind='trades' ORDER BY seq",(jid,));first=cursor.fetchone()
            try:
                def trades():
                    yield json.loads(first[0])
                    for row in cursor:yield json.loads(row[0])
                packet=from_trades(result,trades() if first else result.get('trades',[]))
            finally:cursor.close()
            with self.db:self.db.execute('INSERT OR REPLACE INTO bt_time_statistics VALUES(?,?,?)',(jid,TIME_VERSION,json.dumps(packet,allow_nan=False)))
        return {**result,'time_statistics':packet}

    def trade_times(self,jid,offset=0,limit=20):
        if not isinstance(offset,int) or isinstance(offset,bool) or offset<0 or not isinstance(limit,int) or isinstance(limit,bool) or not 1<=limit<=100:
            raise ValidationError('开平仓记录分页参数无效')
        self._ensure_single_statistics(jid)
        with self.lock:
            if not self.db.execute('SELECT 1 FROM bt_batches WHERE id=?',(jid,)).fetchone():raise ValidationError('回测历史记录不存在',404)
            runs=self.db.execute('SELECT run_id,round_index,metrics FROM bt_batch_rounds WHERE batch_id=? ORDER BY round_index',(jid,)).fetchall()
        iterators=[];expected=0;total=0;known_complete=True
        try:
            for run in runs:
                expected+=json.loads(run['metrics']).get('total_trades',0)
                with self.lock:
                    count=self.db.execute("SELECT COUNT(*) FROM bt_details WHERE run_id=? AND kind='trades'",(run['run_id'],)).fetchone()[0]
                    cached=self.db.execute('SELECT payload FROM bt_time_statistics WHERE run_id=? AND version=?',(run['run_id'],TIME_VERSION)).fetchone()
                    if cached and not json.loads(cached[0]).get('complete'):known_complete=False
                if count:
                    total+=count
                    iterator=self._ordered_trade_times(run['run_id'],run['round_index']);iterators.append(iterator)
                else:
                    trades=self.run(run['run_id']).get('trades',[]);trades=[t for t in trades if valid_trade(t)];total+=len(trades)
                    iterators.append(iter([{**t,'run_id':run['run_id'],'round_index':run['round_index']} for t in sorted(trades,key=lambda t:(t['opened_at'],t['id']),reverse=True)]))
            merged=heapq.merge(*iterators,key=lambda t:(-t['opened_at'],-t['round_index'],-t['id']))
            rows=[]
            for i,trade in enumerate(merged):
                if i>=offset:rows.append(trade)
                if len(rows)>=limit:break
            return dict(total=total,expected=expected,complete=total==expected and known_complete,offset=offset,limit=limit,rows=rows)
        finally:
            for iterator in iterators:
                if hasattr(iterator,'close'):iterator.close()

    def _ordered_trade_times(self,run_id,index):
        with self.lock:cursor=self.db.execute("SELECT payload FROM bt_details INDEXED BY bt_trade_open_time WHERE run_id=? AND kind='trades' ORDER BY json_extract(payload,'$.opened_at') DESC,seq DESC",(run_id,))
        try:
            while True:
                with self.lock:row=cursor.fetchone()
                if row is None:break
                t=json.loads(row[0])
                if valid_trade(t):yield {**t,'run_id':run_id,'round_index':index}
        finally:
            with self.lock:cursor.close()
