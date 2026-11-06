"""Isolated browser fixture. No connection to 8090 or real trading accounts."""
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from server.app import create_server
from server.engine import Engine
from server.backtest_service import BacktestService
from server.backtesting import DEFAULT_CONFIG
from server.market_history import INSTRUMENTS


class FakeBridge:
    def __init__(self,audit):self.audit=audit;self.requests=[]
    def record(self,path):
        self.requests.append({'method':'GET','path':path})
        self.audit.write_text(json.dumps(self.requests),encoding='utf-8')
    def status(self):self.record('/api/status');return {'connected':True,'historyWindowVersion':1}
    def instruments(self):
        self.record('/api/symbols')
        return [dict(root=r,symbol=r+' 12-26',point_value=s['point_value'],tick_size=s['tick_size'],test_fixture=True) for r,s in INSTRUMENTS.items()]
    def resolve(self,root,symbol=None,catalog=None):
        self.record('/api/resolve')
        return next(x for x in (catalog or self.instruments()) if x['root']==root)
    def history(self,symbol,interval,start,end):
        self.record('/api/history')
        return [b for b in fixture_bars(symbol.split()[0]) if start<=b['time']<=end and b['time']%interval==0]


def fixture_bars(root):
    final=int(time.time()//60)*60-60
    price={'MNQ':20000,'MES':6000,'MGC':2500,'MCL':70}[root];tick=INSTRUMENTS[root]['tick_size']
    bars=[]
    for day in range(4):
        for i in range(120):
            p=price+i*tick
            bars.append(dict(time=final-(3-day)*86400-(119-i)*60,open=p,high=p+80*tick,low=p,close=p+4*tick,volume=100+i))
    return bars


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--port',type=int);p.add_argument('--db');args=p.parse_args()
    engine=Engine(args.db,seed=False)
    bridge=FakeBridge(Path(args.db).with_suffix('.history-reads.json'))
    service=BacktestService(Path(args.db).parent/'backtests.sqlite',bridge=bridge)
    for instrument in bridge.instruments():service.store.save_chunk(instrument,60,fixture_bars(instrument['root']))
    source=fixture_bars('MNQ')
    service.store.setting('form',dict(DEFAULT_CONFIG,root='MNQ',symbol='MNQ 12-26',interval=60,
        leverage=50,slippage_mode='bps',slippage_bps=.1,**{'from':source[0]['time'],'to':source[-1]['time']}))
    service.observe_costs(dict(symbol='NQ-PERP',fee_bps=.25,slippage_bps=.1,observed_at=time.time(),notional=40000,source_text='Isolated browser test quote'))
    server=create_server(engine,port=args.port,backtests=service)
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:server.server_close();engine.close()
