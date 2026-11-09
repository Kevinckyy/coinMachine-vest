"""Shared, versioned strategy catalogue. No broker or execution dependencies."""
import copy

STRATEGIES = {'random_entry': {
    'id':'random_entry','name':'随机下单','version':3,
    'description':'优先选择 Live 号；同批同品种统一方向，随机方向每批抽取一次；逐笔等待随机间隔，整批结束后等待下一批。',
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


def choose_batch_side(strategy_id, rng, preferences):
    """Resolve one side for all members of one symbol's batch."""
    require_strategy(strategy_id)
    choices=set(preferences)
    if not choices or not choices.issubset({'buy','sell','random'}):
        raise ValueError('批次方向参数无效')
    fixed=choices-{'random'}
    if len(fixed)>1:
        raise ValueError('同批同品种必须同向：考核号与 Live 的固定方向冲突，请改为一致方向或随机')
    return next(iter(fixed)) if fixed else choose_side(strategy_id,rng,'random')


def choose_live_first(strategy_id,rng,candidates,size,is_live):
    live=[a for a in candidates if is_live(a)]
    if not live:return choose_batch(strategy_id,rng,candidates,size)
    picked=choose_batch(strategy_id,rng,live,min(size,len(live)))
    if len(picked)<size:
        picked+=choose_batch(strategy_id,rng,[a for a in candidates if not is_live(a)],size-len(picked))
    return picked
