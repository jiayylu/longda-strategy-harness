"""Fresh bounded construction of fixtures; only past observations enter queries."""
import copy,json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).parent))
import worker
from constructor import construct
from production_state import snapshot,features,audit_state

def main():
 root=worker.ROOT;out=root/'runs/context-cases';out.mkdir(parents=True,exist_ok=True)
 base=json.loads((root/'configs/orders20.json').read_text());records=[]
 for n,cars in [(4,2),(8,2),(12,2),(20,2),(4,1)]:
  sc=copy.deepcopy(base);sc['name']=f'fresh-{n}-car{cars}';sc['transport']['vehicle_count']=cars
  sc['orders']=[o for i,o in enumerate(sc['orders']) if i%5<n//4]
  ids={r['id']:[o['id'] for o in sc['orders'] if o['route']==r['id']] for r in sc['routes']}
  spread=[names[j] for j in range(n//4) for names in ids.values()]
  waves=[spread[i:i+4] for i in range(0,len(spread),4)]
  s,err,_=construct(sc,waves,engine='legacy',policy=0,seconds=3)
  if not s:
   for width in [3,2,1]:
    waves=[spread[i:i+width] for i in range(0,len(spread),width)]
    s,err,_=construct(sc,waves,engine='legacy',policy=0,seconds=3)
    if s:break
  if not s:raise ValueError((n,cars,err))
  check=worker.audit(sc,s);proof=worker.verify(sc,copy.deepcopy(s))
  if not check['valid'] or not proof['valid']:raise ValueError('Source fixture invalid')
  name=f'cold{n}-car{cars}';worker.save(out/(name+'.json'),sc);worker.save(out/(name+'-source.json'),s)
  records.append(dict(name=name,orders=n,cars=cars,state='cold',features=features(sc)))
  if n in (8,20) or cars==1:
   times=[(50,'heating'),((s['forge_start'][0]+3)/3,'press')]
   if n==20:times=[(1500,'day2')]
   for at,label in times:
    target=copy.deepcopy(sc);target['production_state']=snapshot(sc,s,int(at*3));target['name']=f'{name}-{label}'
    if n==8 and label=='press':
     for j,r in enumerate(sc['routes'][:2]):target['orders'].append(dict(id=f'NEW{j}',route=r['id'],release_minutes=at,due_minutes=5760))
    worker.save(out/(target['name']+'.json'),target);records.append(dict(name=target['name'],orders=len(target['orders']),cars=cars,state=label,features=features(target)))
 worker.save(out/'manifest.json',dict(cases=records,fixture_method='New v3 construction; only started actions and measured equipment state are exported. Future source schedules never enter query scenarios or Memory.'))
 print(json.dumps(records,ensure_ascii=False))

if __name__=='__main__':main()
