"""Finite comparisons only. Old code/rules are read, never edited; outputs all in v6.
Same-action replay isolates runtime, default comparison includes algorithm selection.
"""
import argparse,copy,hashlib,json,os,subprocess,sys,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
p=argparse.ArgumentParser();p.add_argument('--worker-root');p.add_argument('--mode');p.add_argument('--out');p.add_argument('--trace',default='off');a=p.parse_args()
if a.worker_root:
    began=time.monotonic();root=Path(a.worker_root);sys.path[:0]=[str(root/'python')]
    import worker
    from calendar_engine import Calendar
    from algorithm import construct_algorithm
    from scoring import evaluate_schedule
    sc=json.loads((root/'configs/orders20.json').read_text());sc['transport']['vehicle_count']=1
    waves=[[o['id'] for o in sc['orders']]]
    out=Path(a.out);out.mkdir(parents=True,exist_ok=True)
    if a.mode=='replay':
        e=Calendar(sc,waves,30);sid=0;actions=[]
        t=time.monotonic()
        for _ in range(44):
            choices=e.candidates(sid)
            if not choices:break
            c=min(choices,key=lambda c:(c['start']+(c['min_hold']*.15 if c['kind']=='admit' else c['slack']*.15),c['id']))
            actions.append({k:v for k,v in c.items() if k!='id'});sid=e.advance(sid,c['id'])
        stats=e.diagnostics();stats.pop('partial',None)
        row=dict(mode=a.mode,construction_s=time.monotonic()-t,complete=e.complete(sid),ops=e.inspect(sid)['completed_operations'],action_hash=hashlib.sha256(json.dumps(actions,sort_keys=True).encode()).hexdigest(),diagnostics=stats)
    else:
        if a.mode=='default':code=None
        elif a.mode=='grouped':
            code='''def construct():
 rows=inspect(root())["order_details"]
 routes=[]
 for row in rows:
  if row["route"] not in routes:routes.append(row["route"])
 queues=[[r["order"] for r in rows if r["route"]==route] for route in routes]
 waves=[[q[i] for q in queues if i<len(q)] for i in range(max([len(q) for q in queues]))]
 return batches(waves,policy=3)
'''
        else:
            code='''def construct():
 state=root()
 while remaining()>0:
  if complete(state):return state
  opts=candidates(state)
  if not opts:return None
  pick=sorted([(c["start"]+(c["min_hold"]*.15 if c["kind"]=="admit" else c["slack"]*.15),c["id"]) for c in opts])[0][1]
  state=advance(state,pick)
 return None
'''
        os.environ['LONGDA_ALGORITHM_TRACE']=a.trace
        t=time.monotonic();s,error,stats=construct_algorithm(sc,waves,code,seconds=8);construction=time.monotonic()-t
        stats.pop('partial',None)
        row=dict(mode=a.mode,trace=a.trace,construction_s=construction,error=error,complete=False,ops=stats.get('completed_operations'),diagnostics=stats)
        if s:
            checked=worker.audit_delivery(sc,s,out);row.update(complete=checked['valid'],audit_s=checked['audit_s'],day_minutes=checked.get('day_minutes'),schedule_hash=hashlib.sha256(json.dumps(s,sort_keys=True).encode()).hexdigest())
    row['total_s']=time.monotonic()-began;worker.save(out/'measurement.json',row)
    print(json.dumps({k:v for k,v in row.items() if k!='diagnostics'}));sys.exit()
old=ROOT.parent/'longda-strategy-harness-v5-20261016';out=ROOT/'runs/benchmark-v6';out.mkdir(parents=True,exist_ok=True)
for name in ['reference-manifest.json','guard-manifest.json']:
    assert (old/name).read_bytes()==(ROOT/name).read_bytes()
for name in json.loads((ROOT/'guard-manifest.json').read_text()):
    assert (old/name).read_bytes()==(ROOT/name).read_bytes()
rows=[]
# Three interleaved runs for replay and complete grouping; two default runs, then trace ablation.
for mode,reps in [('replay',3),('grouped',3),('default',2)]:
 for rep in range(reps):
  for tag,root in [('v5',old),('v6',ROOT)] if rep%2==0 else [('v6',ROOT),('v5',old)]:
   dest=out/f'{mode}-{tag}-{rep}';t=time.monotonic()
   r=subprocess.run([str(ROOT/'.venv/bin/python'),str(Path(__file__).resolve()),'--worker-root',str(root),'--mode',mode,'--out',str(dest)],capture_output=True,text=True,timeout=45)
   if r.returncode:raise RuntimeError(r.stderr)
   d=json.loads((dest/'measurement.json').read_text());d.update(version=tag,repetition=rep,process_wall_s=time.monotonic()-t)
   rows.append(d);print(json.dumps({k:v for k,v in d.items() if k!='diagnostics'}),flush=True)
   (out/'report.json').write_text(json.dumps(dict(rules_unchanged=True,measurements=rows),indent=2))
for trace in ['opcode','off']:
 dest=out/f'trace-{trace}';t=time.monotonic()
 subprocess.run([str(ROOT/'.venv/bin/python'),str(Path(__file__).resolve()),'--worker-root',str(ROOT),'--mode','greedy','--trace',trace,'--out',str(dest)],check=True,timeout=45)
 d=json.loads((dest/'measurement.json').read_text());d.update(version='v6',process_wall_s=time.monotonic()-t);rows.append(d)
(out/'report.json').write_text(json.dumps(dict(rules_unchanged=True,measurements=rows),indent=2))
