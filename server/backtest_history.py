"""Names and searchable metadata for backtests; simulation records stay immutable."""
import json
import math
import multiprocessing
import re
from datetime import datetime,timedelta,timezone

from .engine import ValidationError
from .strategies import require_strategy


def custom_name(value):
    if not isinstance(value,str) or len(value)>80 or any(ord(ch)<32 or ord(ch)==127 for ch in value):
        raise ValidationError('自定义名称需为不超过 80 字的单行文本')
    return value.strip()


def name_fields(row):
    created=datetime.fromtimestamp(row['created_at'],timezone(timedelta(hours=8)))
    stamp=created.strftime('%Y%m%d-%H%M%S')+f'{created.microsecond//1000:03}'
    return dict(custom_name=row['custom_name'],strategy_name=row['strategy_name'],
        name=f"{row['strategy_name']}-{row['custom_name'] or '未命名'}-{stamp}")


def _match_names(pattern,names,sender):
    try:
        compiled=re.compile(pattern,re.IGNORECASE)
        sender.send([i for i,name in enumerate(names) if compiled.search(name)])
    finally:sender.close()


def matching_names(pattern,names,timeout=2):
    if not isinstance(pattern,str) or len(pattern)>200:raise ValidationError('名称正则最多 200 字')
    try:re.compile(pattern,re.IGNORECASE)
    except re.error as exc:raise ValidationError('名称正则无效：'+str(exc)) from None
    if not pattern:return list(range(len(names)))
    # Bound regex execution even for pathological expressions. Do not hold the
    # database lock or run the expression in the trading service's main thread.
    ctx=multiprocessing.get_context('spawn');receiver,sender=ctx.Pipe(duplex=False)
    process=ctx.Process(target=_match_names,args=(pattern,names,sender),daemon=True)
    try:
        process.start();sender.close()
        if not receiver.poll(timeout):raise ValidationError('名称正则匹配超时，请简化表达式')
        try:return receiver.recv()
        except EOFError:raise ValidationError('名称正则匹配失败，请简化表达式') from None
    finally:
        receiver.close();sender.close()
        if process.pid:
            if process.is_alive():process.terminate()
            process.join();process.close()


class BacktestHistory:
    def _history_name(self,jid,config):
        strategy_id=config.get('strategy_id','random_entry')
        try:strategy=require_strategy(strategy_id)['name']
        except ValueError:strategy=strategy_id
        self.db.execute('INSERT OR IGNORE INTO bt_record_names VALUES(?,?,?)',
            (jid,custom_name(config.get('record_name','')),strategy))

    def _history_fields(self,jid,created_at,config):
        with self.lock:
            row=self.db.execute('SELECT * FROM bt_record_names WHERE id=?',(jid,)).fetchone()
            if row is None:
                with self.db:self._history_name(jid,config)
                row=self.db.execute('SELECT * FROM bt_record_names WHERE id=?',(jid,)).fetchone()
        return name_fields({**dict(row),'created_at':created_at})

    def _legacy_history(self):
        with self.lock:
            rows=self.db.execute('''SELECT id FROM bt_runs r WHERE
                NOT EXISTS(SELECT 1 FROM bt_batch_rounds b WHERE b.run_id=r.id)
                AND NOT EXISTS(SELECT 1 FROM bt_batches b WHERE b.id=r.id)''').fetchall()
        for row in rows:self._ensure_single_statistics(row['id'])

    def history(self,*,root='',strategy_id='',start=None,end=None,time_basis='created',pattern='',offset=0,limit=20):
        if isinstance(offset,bool) or not isinstance(offset,int) or offset<0 or isinstance(limit,bool) or not isinstance(limit,int) or not 1<=limit<=100:
            raise ValidationError('历史分页参数无效')
        if time_basis not in ('created','data'):raise ValidationError('历史时间筛选口径无效')
        for value in (start,end):
            if value is not None and (isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or not 0<=value<=1e12):raise ValidationError('历史时间筛选无效')
        if start is not None and end is not None and start>end:raise ValidationError('筛选开始时间不能晚于结束时间')
        # Compile before touching historical statistics, even for an empty list.
        if not isinstance(pattern,str) or len(pattern)>200:raise ValidationError('名称正则最多 200 字')
        try:re.compile(pattern,re.IGNORECASE)
        except re.error as exc:raise ValidationError('名称正则无效：'+str(exc)) from None
        self._legacy_history()
        clauses=[];args=[]
        if root:clauses.append("json_extract(b.config,'$.root')=?");args.append(root)
        if strategy_id:clauses.append("json_extract(b.config,'$.strategy_id')=?");args.append(strategy_id)
        if start is not None:
            clauses.append(("b.created_at" if time_basis=='created' else "json_extract(b.config,'$.to')")+'>=?');args.append(start)
        if end is not None:
            clauses.append(("b.created_at" if time_basis=='created' else "json_extract(b.config,'$.from')")+'<=?');args.append(end)
        query='SELECT b.id,b.created_at,b.status,b.config FROM bt_batches b'+(' WHERE '+' AND '.join(clauses) if clauses else '')+' ORDER BY b.created_at DESC,b.id DESC'
        with self.lock:rows=self.db.execute(query,args).fetchall()
        records=[]
        for row in rows:
            c=json.loads(row['config'])
            records.append(dict(id=row['id'],created_at=row['created_at'],status=row['status'],root=c.get('root'),symbol=c.get('symbol'),
                strategy_id=c.get('strategy_id','random_entry'),rounds=c.get('rounds',1),**{'from':c.get('from'),'to':c.get('to')},
                **self._history_fields(row['id'],row['created_at'],c)))
        indices=matching_names(pattern,[r['name'] for r in records]) if pattern else list(range(len(records)))
        return dict(total=len(indices),offset=offset,limit=limit,rows=[records[i] for i in indices[offset:offset+limit]],timezone='Asia/Shanghai')

    def rename_history(self,jid,value):
        value=custom_name(value)
        with self.lock,self.db:
            row=self.db.execute('SELECT created_at,config FROM bt_batches WHERE id=?',(jid,)).fetchone()
            if not row:
                self._ensure_single_statistics(jid)
                row=self.db.execute('SELECT created_at,config FROM bt_batches WHERE id=?',(jid,)).fetchone()
            if not row:raise ValidationError('回测历史记录不存在',404)
            self._history_name(jid,json.loads(row['config']))
            self.db.execute('UPDATE bt_record_names SET custom_name=? WHERE id=?',(value,jid))
            return dict(id=jid,**self._history_fields(jid,row['created_at'],json.loads(row['config'])))
