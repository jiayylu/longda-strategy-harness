"""Persistent state IDs backed by parent/action records, with a bounded materialization cache.

Eviction NEVER deletes a search branch. All IDs can be reconstructed; checkpoints
store roots and deltas, not the LRU cache. No opaque controller handle is scanned
or guessed to decide whether it is still live.
"""
from collections import OrderedDict
import copy

class StateStore:
    def __init__(self, apply, root, cache_size=32):
        self.apply=apply;self.cache_size=cache_size
        self.records={0:dict(base=root)};self.cache=OrderedDict([(0,root)])
        self.reconstructions=0
    def __contains__(self,key):return key in self.records
    def __len__(self):return len(self.records)
    def __setitem__(self,key,state):
        self.records[key]=dict(base=state);self.put(key,state)
    def put(self,key,state):
        self.cache[key]=state;self.cache.move_to_end(key)
        while len(self.cache)>self.cache_size:self.cache.popitem(last=False)
    def add(self,key,parent,action,state):
        self.records[key]=dict(parent=parent,action=copy.copy(action));self.put(key,state)
    def __getitem__(self,key):
        trail=[];current=key
        while current not in self.cache:
            rec=self.records[current]
            if 'base' in rec:
                self.put(current,rec['base']);break
            trail.append(current);current=rec['parent']
        state=self.cache[current];self.cache.move_to_end(current)
        for ident in reversed(trail):
            rec=self.records[ident];state=self.apply(state,rec['action']);self.put(ident,state)
            self.reconstructions+=1
        return state
    def dump(self):return self.records
    def load(self,records):
        self.records={int(k):v for k,v in records.items()};self.cache.clear()
        # Fail closed on missing/cyclic/non-ancestral records, before generated code runs.
        for ident,rec in self.records.items():
            if 'base' not in rec and (rec['parent'] not in self.records or rec['parent']>=ident):
                raise ValueError('Corrupt persistent state ancestry')
        if 0 not in self.records:raise ValueError('Missing root state')
