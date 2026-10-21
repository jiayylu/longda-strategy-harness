"""Finite v5 checks: frozen rules, unchanged controls, full-code construction and WIP.
No model requests and no persistent search. Results are not a speed benchmark.
"""
import copy,hashlib,importlib.util,json,sys,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'python'))
import worker
from algorithm import construct_algorithm
from production_state import snapshot,audit_state
from constructor import construct

old=ROOT.parent/'longda-strategy-harness-v4-20261016'
if not old.exists():raise SystemExit('Comparison needs the preserved v4 sibling directory')
spec=importlib.util.spec_from_file_location('v4_constructor',old/'python/constructor.py')
v4=importlib.util.module_from_spec(spec);spec.loader.exec_module(v4)
started=time.monotonic();rows=[]
sc=json.loads((ROOT/'configs/orders20.json').read_text())
manifest=json.loads((ROOT/'reference-manifest.json').read_text())
assert manifest==json.loads((old/'reference-manifest.json').read_text())
for rel,digest in manifest.items():
 for base in (old,ROOT):assert hashlib.sha256((base/'reference'/rel).read_bytes()).hexdigest()==digest,rel
frozen=['physical_model.py','verification.py','production_state.py','scoring.py','legacy_constructor.py','program.py']
for rel in frozen:assert (ROOT/'python'/rel).read_bytes()==(old/'python'/rel).read_bytes(),rel
worker.integrity()
waves=[[f'{r}-{i:02d}' for r in ['C430','C280R','C280I','CFLAT']] for i in range(1,6)]
for cars in (1,2):
 sc['transport']['vehicle_count']=cars
 for engine,policy in [('legacy',3),('extended',0),('extended',3)]:
  opts=dict(engine=engine,policy=policy,wip_limit=4,seconds=3)
  a,ae,_=v4.construct(copy.deepcopy(sc),waves,**opts)
  b,be,_=construct(copy.deepcopy(sc),waves,**opts)
  assert a==b and ae==be,(cars,engine,policy)
  valid=None
  if b is not None:
   valid=worker.audit(sc,b)['valid'] and worker.verify(sc,copy.deepcopy(b))['valid'];assert valid
  rows.append(dict(test='old_new_control',cars=cars,engine=engine,policy=policy,identical=True,complete=valid))
code='''def construct():
    rows=inspect(root())["order_details"]
    routes=[]
    for row in rows:
        if row["route"] not in routes: routes.append(row["route"])
    queues=[[r["order"] for r in rows if r["route"]==route] for route in routes]
    waves=[]
    for i in range(max(len(q) for q in queues)):
        waves.append([q[i] for q in queues if i<len(q)])
    return batches(waves,policy=3)
'''
for cars in (1,2):
 sc['transport']['vehicle_count']=cars
 result,error,stats=construct_algorithm(sc,waves,code,seconds=10)
 assert result is not None,error
 assert worker.audit(sc,result)['valid'] and worker.verify(sc,copy.deepcopy(result))['valid']
 rows.append(dict(test='full_code_current_order_grouping',cars=cars,complete=True,day_minutes=worker.evaluate_schedule(sc,result)['score_ticks']/sc['ticks_per_minute'],api_calls=stats['algorithm_api_calls']))
# Small changed-count and WIP cases check that current state, not a saved future plan, controls execution.
for n in (1,3):
 case=copy.deepcopy(sc);case['transport']['vehicle_count']=1
 case['routes']=[dict(id='R',operations=[dict(fire=1,temp=1150,hold_min=60,hold_max=180,forge=10)])]
 case['orders']=[dict(id=f'N{i}',route='R',release_minutes=0,due_minutes=5760) for i in range(n)]
 seed,error,_=construct_algorithm(case,[[o['id'] for o in case['orders']]],code,seconds=10)
 assert seed is not None,error
 case['production_state']=snapshot(case,seed,30*case['ticks_per_minute'])
 greedy='''def construct():
    state=root()
    while remaining()>0:
        if complete(state): return state
        opts=candidates(state)
        if not opts: return None
        action=sorted([(c["start"],c["id"]) for c in opts])[0][1]
        state=advance(state,action)
    return None
'''
 result,error,stats=construct_algorithm(case,[[o['id'] for o in case['orders']]],greedy,seconds=10)
 assert result is not None,error
 assert audit_state(case,result)['valid'] and worker.audit(case,result)['valid'] and worker.verify(case,copy.deepcopy(result))['valid']
 rows.append(dict(test='changed_count_observed_heating',orders=n,complete=True,observed_audit=True))
report=dict(reference_unchanged=True,reference_files=len(manifest),protected_and_control_files=frozen,checks=rows,elapsed_s=time.monotonic()-started)
out=ROOT/'runs/acceptance-v5';out.mkdir(exist_ok=True,parents=True);(out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
print(json.dumps(report,ensure_ascii=False,indent=2))
