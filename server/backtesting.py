"""Deterministic OHLC account simulation. No execution or broker client imports.

Historical futures prices proxy Vest prices; fills, basis, funding and order-book
liquidity cannot be reconstructed from candles. Every result records assumptions.
"""
import copy
import math
from datetime import datetime,timedelta,timezone,time as walltime,date

from .engine import ValidationError
from .market_history import INSTRUMENTS
from .strategies import require_strategy
from .trading_day import NEW_YORK
from .backtest_batches import MAX_ROUNDS,MAX_PARALLEL
from .backtest_random import ALGORITHM,LEGACY_ALGORITHM,normalize_seed

PHASES=('1/2','2/2','live')
MAX_ACCOUNT_COUNT=1_000_000
RULE_SOURCES=[
    dict(title='Vest 两阶段方案',url='https://docs.vestmarkets.com/vest-capital/account-tiers'),
    dict(title='固定回撤、日亏损与 20:00 ET',url='https://docs.vestmarkets.com/vest-capital/loss-limits.md'),
    dict(title='分润及 24 小时到账',url='https://docs.vestmarkets.com/vest-capital/claiming-profit'),
    dict(title='指数和商品单边 Taker 费 0.25 bps',url='https://docs.vestmarkets.com/trading/markets.md'),
    dict(title='等值微型合约与杠杆上限',url='https://docs.vestmarkets.com/trading/cross-margin-and-leverage'),
]
DEFAULT_CONFIG=dict(strategy_id='random_entry',account_count=50,seed=42,rng_algorithm=LEGACY_ALGORITHM,seed_policy='fixed',rounds=1,parallel_workers=min(4,MAX_PARALLEL),initial_capital=5000,
    phase1_goal=500,phase2_goal=250,max_drawdown=300,daily_loss_percent=3,profit_split_percent=80,
    account_fee=15,payout_min_profit=1,live_profit_target=250,payout_delay_hours=24,
    risk_reset_hour=20,strategy_reset_hour=17,initial_phase='1/2',repeat_daily=True,
    batch_size=2,side='random',size_percent=25,stop_loss=130,skip_stop_loss_on_low_drawdown=False,
    drawdown_stop_loss_threshold=200,order_delay_min=2,order_delay_max=8,batch_delay_min=15,batch_delay_max=45,
    fee_bps=.25,slippage_mode='ticks',slippage_ticks=1,slippage_usd=1,slippage_bps=0,funding_bps_hourly=0,
    sizing='micro_contracts',cost_source='官方手续费；滑点为可编辑的每边 1 tick 假设')


def finite(value,label,low=0,high=1e9,integer=False):
    if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or not low<=value<=high or (integer and value!=int(value)):
        kind='整数' if integer else '有效数字'
        bounds=f'{int(low)}–{int(high)}' if integer else f'{low:g}–{high:g}'
        raise ValidationError(f'{label}必须为 {bounds} 之间的{kind}')
    return int(value) if integer else float(value)


def validate_config(raw):
    c={**DEFAULT_CONFIG,**copy.deepcopy(raw)}
    try:c['seed']=normalize_seed(c['seed'])
    except (ValueError,OverflowError):raise ValidationError('随机种子格式无效：需32位整数或128位十六进制字符串') from None
    if c['rng_algorithm'] not in (ALGORITHM,LEGACY_ALGORITHM):raise ValidationError('随机数算法无效')
    if c['seed_policy'] not in ('fixed','random_128'):raise ValidationError('种子生成规则无效')
    c['rounds']=finite(c['rounds'],'测试轮数',1,MAX_ROUNDS,True)
    c['parallel_workers']=finite(c['parallel_workers'],'并行任务数',1,MAX_PARALLEL,True)
    if c['account_count'] in ('∞','infinity'):c['account_count']='infinity'
    else:c['account_count']=finite(c['account_count'],'模拟账户数',1,MAX_ACCOUNT_COUNT,True)
    try:require_strategy(c['strategy_id'])
    except ValueError as exc:raise ValidationError(str(exc)) from None
    root=c.get('root')
    if not isinstance(root,str) or root not in INSTRUMENTS:raise ValidationError('请选择已缓存的 MNQ、MES、MGC 或 MCL 合约')
    for key,low,high,integer in [('initial_capital',1,1e7,False),
        ('phase1_goal',.01,1e7,False),('phase2_goal',.01,1e7,False),('max_drawdown',.01,1e7,False),
        ('daily_loss_percent',0,100,False),('profit_split_percent',0,100,False),('account_fee',0,1e6,False),
        ('payout_min_profit',.01,1e7,False),('live_profit_target',0,1e7,False),('payout_delay_hours',0,168,False),
        ('risk_reset_hour',0,23,True),('strategy_reset_hour',0,23,True),('batch_size',1,10000,True),
        ('stop_loss',.01,1e7,False),('drawdown_stop_loss_threshold',.01,1e7,False),
        ('fee_bps',0,100,False),('slippage_ticks',0,1000,False),('slippage_usd',0,1e7,False),('slippage_bps',0,100,False),('funding_bps_hourly',-100,100,False)]:
        label={'account_count':'模拟账户数','seed':'随机种子','batch_size':'每批账户数',
            'initial_capital':'初始资金','phase1_goal':'第一阶段目标','phase2_goal':'第二阶段目标',
            'max_drawdown':'固定最大回撤','daily_loss_percent':'日亏损比例','profit_split_percent':'分润比例',
            'account_fee':'单号购买成本','payout_min_profit':'最低可申请利润','live_profit_target':'live 出金平仓目标',
            'payout_delay_hours':'分润到账延迟小时','risk_reset_hour':'风控日切','strategy_reset_hour':'开仓额度日切',
            'stop_loss':'每笔止损','drawdown_stop_loss_threshold':'不设止损阈值','fee_bps':'单边手续费',
            'slippage_ticks':'每边滑点 tick','slippage_usd':'每笔每边滑点 USD','slippage_bps':'历史每边滑点 bps','funding_bps_hourly':'每小时资金费'}[key]
        c[key]=finite(c[key],label,low,high,integer)
    c['leverage']=finite(c.get('leverage',INSTRUMENTS[root]['leverage']),'杠杆',1,INSTRUMENTS[root]['leverage'])
    if c['max_drawdown']>=c['initial_capital']:raise ValidationError('最大回撤必须小于起始资金')
    if c['account_count']!='infinity' and c['batch_size']>c['account_count']:raise ValidationError('每批账户数不能超过回测账户数')
    if c['size_percent'] not in (25,50,75,100) or isinstance(c['size_percent'],bool):raise ValidationError('仓位大小无效')
    if c['side'] not in ('buy','sell','random') or c['initial_phase'] not in PHASES or c['slippage_mode'] not in ('ticks','usd','bps') or c['sizing'] not in ('micro_contracts','vest_equivalent'):
        raise ValidationError('方向、阶段、数量或滑点模式无效')
    for key in ('repeat_daily','skip_stop_loss_on_low_drawdown'):
        if not isinstance(c[key],bool):raise ValidationError(key+'必须为布尔值')
    for field in ('order_delay_min','order_delay_max','batch_delay_min','batch_delay_max'):
        c[field]=finite(c[field],field,0,86400)
    if c['order_delay_min']>c['order_delay_max'] or c['batch_delay_min']>c['batch_delay_max']:raise ValidationError('随机间隔上限不能小于下限')
    if c.get('interval',60) not in (60,300,900,3600):raise ValidationError('回测周期无效')
    c['interval']=c.get('interval',60)
    c['from']=finite(c.get('from'), '开始时间',1,1e12)
    c['to']=finite(c.get('to'),'结束时间',c['from']+1,1e12)
    if not isinstance(c.get('symbol'),str) or not c['symbol'].startswith(root+' '):raise ValidationError('回测合约与品种不符')
    c['cost_source']=str(c['cost_source'])[:300]
    return c


def ny_day(ts,hour):
    day=datetime.fromtimestamp(ts,timezone.utc).date()
    def boundary(day):
        if NEW_YORK is not None:return datetime.combine(day,walltime(hour),NEW_YORK).timestamp()
        march=date(day.year,3,1);november=date(day.year,11,1)
        start=march+timedelta(days=(6-march.weekday())%7+7)
        end=november+timedelta(days=(6-november.weekday())%7)
        dst=start<day<end or (day==start and hour>=3) or (day==end and hour<2)
        return datetime.combine(day,walltime(hour),timezone(timedelta(hours=-4 if dst else -5))).timestamp()
    while ts<boundary(day):
        day-=timedelta(days=1)
    return day.isoformat()


def simulate(raw,bars,instrument,progress=lambda d:None,*,detail_sink=None):
    from .backtest_simulation import simulate as simulate_accounts
    config=validate_config(raw);paired=config['fee_bps']>0;total=len(bars)*(2 if paired else 1)
    def update(offset,mode):
        def report(detail):progress({**detail,'completed':offset+detail.get('completed',0),'total':total,'fee_mode':mode})
        return report
    result=simulate_accounts(config,bars,instrument,update(0,'with_fees'),detail_sink=detail_sink)
    if paired:
        comparison=simulate_accounts({**config,'fee_bps':0},bars,instrument,update(len(bars),'without_fees'))
        result['cash_curve_without_fees']=comparison['curve']
    else:result['cash_curve_without_fees']=result['curve']
    result['fee_comparison_settlement_complete']=True
    result['fee_comparison_basis']='同种子重算，仅交易手续费置零；滑点、资金费、购号成本和全部风控规则保持原配置'
    return result
