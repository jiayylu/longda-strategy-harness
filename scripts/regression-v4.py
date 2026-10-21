"""Finite before/after witnesses and unchanged physical-rule evidence."""
import copy,importlib.util,json,sys,hashlib,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'python'))
import worker
from constructor import construct
old=ROOT.parent/'longda-strategy-harness-v3-20261015'
spec=importlib.util.spec_from_file_location('old_constructor',old/'python/constructor.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
sc=json.load(open(ROOT/'configs/orders20.json'))
waves=[[f'{r}-{i:02d}' for r in ['C430','C280R','C280I','CFLAT']] for i in range(1,6)]
start=time.monotonic();rows=[]
for cars in (1,2):
 sc['transport']['vehicle_count']=cars
 for engine,policy in [('legacy',3),('extended',0),('extended',3)]:
  opts=dict(engine=engine,policy=policy,wip_limit=4,seconds=3)
  a,ae,_=m.construct(copy.deepcopy(sc),waves,**opts)
  b,be,stats=construct(copy.deepcopy(sc),waves,**opts)
  assert a==b and ae==be,(cars,engine,policy)
  audited=None
  if b is not None:
   audited=worker.audit(sc,b)['valid'] and worker.verify(sc,copy.deepcopy(b))['valid']
   assert audited
  rows.append(dict(cars=cars,engine=engine,policy=policy,schedule_identical=True,valid=audited,error=be))
manifest=json.load(open(ROOT/'reference-manifest.json'))
assert manifest==json.load(open(old/'reference-manifest.json'))
for rel,h in manifest.items():
 assert hashlib.sha256((old/'reference'/rel).read_bytes()).hexdigest()==h
 assert hashlib.sha256((ROOT/'reference'/rel).read_bytes()).hexdigest()==h
frozen=['physical_model.py','production_state.py','scoring.py','verification.py','legacy_constructor.py','program.py']
for rel in frozen: assert (ROOT/'python'/rel).read_bytes()==(old/'python'/rel).read_bytes(),rel
report=dict(orders=20,tests=rows,reference_files_unchanged=len(manifest),additional_frozen_files=frozen,elapsed_s=time.monotonic()-start)
p=ROOT/'runs/regression-v4';p.mkdir(exist_ok=True,parents=True);(p/'report.json').write_text(json.dumps(report,indent=2))
print(json.dumps(report,ensure_ascii=False))
