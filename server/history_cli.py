"""Explicit historical-data import. The upstream adapter supports GET only."""
import argparse
import json
import time
import uuid
from .market_history import BacktestStore,ReadOnlyHistoryBridge,sync_history


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--days',type=int,default=30)
    parser.add_argument('--db',default='runtime/backtests.sqlite')
    args=parser.parse_args();now=int(time.time()//60)*60
    store=BacktestStore(args.db);jid=uuid.uuid4().hex
    def progress(value):
        store.save_job(jid,'sync','running',value)
        print(json.dumps(value,ensure_ascii=False),flush=True)
    try:
        result=sync_history(store,ReadOnlyHistoryBridge(),{'roots':['MNQ','MES','MGC','MCL'],'interval':60,'from':now-args.days*86400,'to':now-60},progress)
        store.save_job(jid,'sync',result['status'],result)
        print(json.dumps({'job_id':jid,**result,'inventory':store.inventory()},ensure_ascii=False),flush=True)
        if result['status']=='error':raise SystemExit(1)
    finally:store.close()


if __name__=='__main__':main()
