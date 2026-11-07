"""Shared, versioned strategy catalogue. No broker or execution dependencies."""
import copy

STRATEGIES = {'random_entry': {
    'id':'random_entry','name':'随机下单','version':2,
    'description':'优先选择 Live 号，同阶段随机抽取；逐笔等待随机间隔，整批持仓结束后等待下一批。',
    'live_symbols':['NQ-PERP'],'backtest_roots':['MNQ','MES','MGC','MCL'],
}}


def catalogue():
    return copy.deepcopy(list(STRATEGIES.values()))


def require_strategy(value):
    if not isinstance(value,str) or value not in STRATEGIES:
        raise ValueError('不支持的策略，请选择已注册的自动策略')
    return STRATEGIES[value]


def choose_batch(strategy_id, rng, candidates, size):
    require_strategy(strategy_id)
    return rng.sample(candidates,size)


def choose_side(strategy_id, rng, side):
    require_strategy(strategy_id)
    return rng.choice(('buy','sell')) if side=='random' else side


def choose_live_first(strategy_id,rng,candidates,size,is_live):
    live=[a for a in candidates if is_live(a)]
    if not live:return choose_batch(strategy_id,rng,candidates,size)
    picked=choose_batch(strategy_id,rng,live,min(size,len(live)))
    if len(picked)<size:
        picked+=choose_batch(strategy_id,rng,[a for a in candidates if not is_live(a)],size-len(picked))
    return picked
