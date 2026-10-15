"""Bounded matched ablation: same generated configuration, seed and CPU, all audits."""
import copy,json,time,sys,statistics
from pathlib import Path
sys.path.insert(0,str(Path(__file__).parent))
import worker
from constructor import construct
BASE=json.loads((worker.ROOT/'configs/orders20.json').read_text())
PROGRAM="""def rank(candidates,state):
 return [c['start']+.15*c['min_hold'] if c['kind']=='admit' else c['start']+.15*c['slack']-(10 if c['car']!=c['pickup_car'] else 0) for c in candidates]
"""
VARIANTS={
 'legacy':dict(engine='legacy'),
 'extended-batch':dict(engine='extended',admission='batch'),
 'rolling':dict(engine='extended',admission='rolling'),
 'split-cars':dict(engine='extended',admission='rolling',vehicle_policy='split'),
 'backtracking':dict(engine='extended',admission='rolling',vehicle_policy='split',backtrack_budget=8),
 'program-hook-control':dict(engine='extended',admission='rolling',vehicle_policy='split',backtrack_budget=8,program_source=PROGRAM),
}
def run(out):
 out.mkdir(parents=True,exist_ok=True);rows=[];started=time.monotonic()
 for per_route in [1,2,5]:
  sc=copy.deepcopy(BASE);sc['orders']=[o for o in sc['orders'] if int(o['id'].rsplit('-',1)[1])<=per_route]
  routes=sorted(sc['routes'],key=lambda r:-sum(x['forge'] for x in r['operations'])/sum(x['hold_min']+x['forge'] for x in r['operations']))
  waves=[]
  for k in range(0,len(routes),2):
   pair=routes[k:k+2]
   for offset in range(0,per_route,3):
    wave=[]
    for j in range(offset,min(per_route,offset+3)):
     for r in pair:wave.append([o['id'] for o in sc['orders'] if o['route']==r['id']][j])
    waves.append(wave)
  for cars in [1,2]:
   sc['transport']['vehicle_count']=cars
   for seed in [0,1]:
    for variant,cfg in VARIANTS.items():
     t=time.monotonic();s,err,stats=construct(sc,waves,seed=seed,policy=0,wip_limit=6,seconds=1,**cfg);construction=time.monotonic()-t
     valid=False;errors=[];audit_s=0;metrics=None
     if s:
      at=time.monotonic();a=worker.audit(sc,s)
      if a['valid']:
       p=worker.verify(sc,copy.deepcopy(s));valid=p['valid'];errors=p.get('errors',[])[:2]
      else:errors=a.get('errors',[])[:2]
      audit_s=time.monotonic()-at
      if valid:metrics=worker.evaluate_schedule(sc,s)
     stats.pop('partial',None)
     row=dict(orders=len(sc['orders']),cars=cars,seed=seed,variant=variant,valid=valid,day_minutes=metrics['score_ticks']/3 if metrics else None,construct_s=construction,audit_s=audit_s,total_s=time.monotonic()-t,error=err,errors=errors,stats=stats)
     rows.append(row);worker.save(out/'results.json',dict(rows=rows,elapsed_s=time.monotonic()-started));print(json.dumps(row),flush=True)
 worker.save(out/'summary.json',dict(note='program-hook-control is a hand-written test program, not an LLM-generated success claim; each case has a 1-second constructive cap plus full audits',elapsed_s=time.monotonic()-started,by_variant={v:dict(trials=len(a:=[r for r in rows if r['variant']==v]),valid=sum(r['valid'] for r in a),median_s=statistics.median(r['total_s'] for r in a)) for v in VARIANTS}))
if __name__=='__main__':run(Path(sys.argv[1]))
