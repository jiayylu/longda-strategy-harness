"""Bounded engineering probe, not an optimization benchmark."""
import json,sys,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'python'))
import worker
from algorithm import construct_algorithm
sc=json.loads((ROOT/'configs/orders20.json').read_text());sc['transport']['vehicle_count']=1
code='''def construct():
 state=root()
 while remaining()>0:
  if complete(state): return state
  opts=candidates(state)
  if not opts: return None
  ranked=sorted([(c['start']+(c['min_hold']*.15 if c['kind']=='admit' else c['slack']*.15),c['id']) for c in opts])
  state=advance(state,ranked[0][1])
 return None
'''
t=time.monotonic();s,e,d=construct_algorithm(sc,[[o['id'] for o in sc['orders']]],code,seconds=8)
result=dict(elapsed_s=time.monotonic()-t,error=e,ops=d['completed_operations'],nodes=d['nodes'],candidates=d['candidates'],window_prunes=d['window_prunes'],gap_insertions=d['gap_insertions'],complete=s is not None)
out=ROOT/'runs/calendar20-probe';out.mkdir(parents=True,exist_ok=True)
worker.save(out/'diagnostics.json',d);worker.save(out/'report.json',result)
print(json.dumps(result))
