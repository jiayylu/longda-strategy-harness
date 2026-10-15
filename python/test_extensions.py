import unittest,copy,json,tempfile,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).parent))
import worker
from constructor import construct
from program import Program

class ExtensionTests(unittest.TestCase):
 def scenario(self,cars=2):
  sc=json.loads((worker.ROOT/'configs/orders20.json').read_text());sc['transport']['vehicle_count']=cars
  sc['orders']=sc['orders'][:2]
  sc['routes']=[dict(id=sc['orders'][0]['route'],operations=[dict(fire=1,temp=1150,hold_min=60,hold_max=180,forge=10),dict(fire=2,temp=1130,hold_min=60,hold_max=120,forge=10)])]
  return sc
 def test_legacy_two_car_schedule_unchanged(self):
  from construct_example import construct as original
  from legacy_constructor import construct as current
  sc=self.scenario(2);waves=[[o['id'] for o in sc['orders']]]
  self.assertEqual(original(sc,waves),current(sc,waves))
 def test_generated_program_and_safety(self):
  p=Program("def rank(candidates, state):\n return [c['start'] + c['slack']*.2 - (20 if c['kind']=='admit' and state['active_count']<3 else 0) for c in candidates]")
  self.assertEqual(p.scores([dict(start=20,slack=10,kind='admit')],dict(active_count=0)),[2.0])
  for code in ["import os\ndef rank(candidates,state): return []", "def rank(candidates,state): return open('/tmp/test')", "def rank(candidates,state): return state.clear()", "def rank(candidates,state):\n while True: pass"]:
   with self.assertRaises(ValueError):Program(code)
  with self.assertRaises(ValueError):Program("def rank(candidates,state):\n return [sum(x for x in range(1000000)) for c in candidates]").scores([{}],{})
 def test_single_double_and_extensions_keep_exact_rules(self):
  for cars in [1,2]:
   sc=self.scenario(cars);waves=[[o['id'] for o in sc['orders']]]
   for cfg in [dict(engine='legacy'),dict(engine='extended',admission='rolling'),dict(engine='extended',admission='rolling',vehicle_policy='split',backtrack_budget=4),dict(engine='extended',admission='rolling',vehicle_policy='split',program_source="def rank(candidates,state):\n return [c['start']+c['min_hold']*.15 if c['kind']=='admit' else c['start']+.15*c['slack'] for c in candidates]")]:
    s,err,stats=construct(sc,waves,seconds=2,**cfg)
    self.assertIsNotNone(s,(cars,cfg,err));self.assertTrue(worker.audit(sc,s)['valid']);self.assertTrue(worker.verify(sc,copy.deepcopy(s))['valid'])
 def test_seeded_search_still_applies_baseline_cut(self):
  sc=self.scenario(1);sc['orders']=sc['orders'][:1];sc['routes'][0]['operations']=sc['routes'][0]['operations'][:1]
  s,err,_=construct(sc,[[sc['orders'][0]['id']]])
  with tempfile.TemporaryDirectory() as d:
   p=Path(d);worker.save(p/'seed.json',s)
   r=worker.solve(sc,dict(mode='seeded_search',seed_path=str(p/'seed.json'),incumbent_path=str(p/'seed.json'),preference='none',threads=2,seconds=5,target_minutes=1440,stop_at_target=False),p)
   self.assertEqual(r['status'],'INFEASIBLE');self.assertEqual(r['proof_scope'],'no_improvement_global')
 def test_constructor_upper_bound_prunes_but_not_declares_infeasible(self):
  sc=self.scenario();s,err,stats=construct(sc,[[o['id'] for o in sc['orders']]],engine='extended',incumbent={'score_ticks':999999})
  self.assertIsNone(s);self.assertEqual(err,'day1_bound');self.assertEqual(stats['pruned'],1)
 def test_reject_wrong_hold_is_never_published(self):
  sc=self.scenario();s,err,_=construct(sc,[[o['id'] for o in sc['orders']]])
  s['heat_start'][0]=s['heat_end'][0]
  with tempfile.TemporaryDirectory() as d:
   r=worker.audit_delivery(sc,s,Path(d));self.assertFalse(r['valid']);self.assertNotIn('schedule_path',r)

if __name__=='__main__':unittest.main()
