"""Bounded result previews, streamed detail storage and lazy account selection."""
import copy
from collections import OrderedDict


class UnusedAccounts:
    """Sparse Fisher-Yates pool: even a million untouched IDs need no list."""
    def __init__(self,count):self.remaining=count;self.swaps={}

    def value(self,index):return self.swaps.get(index,index+1)

    def discard(self,indices):
        for index in sorted(indices,reverse=True):
            last=self.remaining-1
            if index!=last:self.swaps[index]=self.value(last)
            self.swaps.pop(last,None);self.remaining-=1


class DetailPreview:
    def __init__(self,sink=None,limit=500):
        self.sink=sink;self.limit=limit;self.rows={k:OrderedDict() for k in ('trades','accounts','claims')}

    def record(self,kind,record):
        row=copy.deepcopy(record);key=row['id'];bucket=self.rows[kind]
        bucket[key]=row;bucket.move_to_end(key)
        if len(bucket)>self.limit:bucket.popitem(last=False)
        if self.sink:self.sink(kind,row)

    def get(self,kind):return sorted(self.rows[kind].values(),key=lambda row:row['id'])


class DetailJournal:
    def __init__(self,store,run_id):self.store=store;self.run_id=run_id;self.buffer=[]

    def __call__(self,kind,row):
        self.buffer.append((kind,row))
        if len(self.buffer)>=128:self.flush()

    def flush(self):
        if self.buffer:self.store.save_details(self.run_id,self.buffer);self.buffer=[]
