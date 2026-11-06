"""Versioned simulation streams and independent OS-generated round seeds."""
import random
import re
import secrets

ALGORITHM='pcg64dxsm_v1'
LEGACY_ALGORITHM='mt19937_v1'
SEED_POLICY='random_128'


def fresh_seed():
    # Prefix prevents SQLite numeric affinity and JavaScript number rounding.
    return '0x'+secrets.token_hex(16)


def normalize_seed(value):
    if isinstance(value,str) and re.fullmatch(r'0[xX][0-9a-fA-F]{32}',value):return value.lower()
    if not isinstance(value,bool) and isinstance(value,(int,float)) and 0<=value<=2**32-1 and value==int(value):return int(value)
    raise ValueError('随机种子必须为32位无符号整数或0x开头的128位十六进制字符串')


class PCG64DXSMStream:
    """Stable sampling adapter over NumPy's guaranteed raw integer stream.

    Buffer raw words for scalar strategy calls; use rejection sampling and a
    sparse Fisher-Yates shuffle instead of version-dependent Generator helpers.
    """
    def __init__(self,seed):
        try:import numpy
        except ImportError:
            from .engine import ValidationError
            raise ValidationError('PCG64DXSM 需要 NumPy，请先运行 python -m pip install -r requirements.txt') from None
        self.library_version=numpy.__version__
        self._bitgen=numpy.random.PCG64DXSM(int(seed,16) if isinstance(seed,str) else seed)
        self._words=iter(())

    def word(self):
        try:return next(self._words)
        except StopIteration:
            self._words=iter(self._bitgen.random_raw(1024).tolist())
            return next(self._words)

    def below(self,n):
        if not 1<=n<=2**64:raise ValueError('随机抽样范围无效')
        limit=2**64-(2**64%n)
        while True:
            value=self.word()
            if value<limit:return value%n

    def random(self):return (self.word()>>11)*(1./2**53)

    def uniform(self,a,b):return a+(b-a)*self.random()

    def choice(self,population):
        if not len(population):raise IndexError('不能从空集合抽样')
        return population[self.below(len(population))]

    def sample(self,population,k):
        n=len(population)
        if not 0<=k<=n:raise ValueError('抽样数超出集合范围')
        swaps={};selected=[]
        for i in range(k):
            j=self.below(n-i);selected.append(population[swaps.get(j,j)])
            swaps[j]=swaps.get(n-i-1,n-i-1)
        return selected


def create_stream(config):
    if config['rng_algorithm']==LEGACY_ALGORITHM:
        seed=config['seed'];return random.Random(int(seed,16) if isinstance(seed,str) else seed)
    return PCG64DXSMStream(config['seed'])


def reserve_seed(store,batch_id,index,first):
    for attempt in range(16):
        seed=first if index==0 and attempt==0 else fresh_seed()
        if store.reserve_round_seed(batch_id,index,seed):return seed
    raise ValueError('随机源连续产生重复种子，请重新开始测试')
