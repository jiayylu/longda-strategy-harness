import copy,json,sys,tempfile,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).parent))
import worker
from constructor import construct
from production_state import snapshot,state_data,audit_state,features,add_state_constraints
from scoring import evaluate_schedule,add_objective
from longda_cpsat.model import build_model,extract_schedule
from ortools.sat.python import cp_model

class StateTests(unittest.TestCase):
 def scenario(self,cars=2):
  sc=json.loads((worker.ROOT/'configs/orders20.json').read_text());sc['orders']=sc['orders'][:2];sc['transport']['vehicle_count']=cars
  sc['routes']=[dict(id=sc['orders'][0]['route'],operations=[dict(fire=1,temp=1150,hold_min=60,hold_max=180,forge=10),dict(fire=2,temp=1130,hold_min=60,hold_max=180,forge=10)])]
  return sc
 def base(self,cars=2):
  sc=self.scenario(cars);s,e,_=construct(sc,[[o['id'] for o in sc['orders']]],engine='extended',admission='rolling',vehicle_policy='split');self.assertIsNotNone(s,e);return sc,s
 def test_observed_heating_inflight_and_press_continue(self):
  for cars in (1,2):
   sc,s=self.base(cars)
   points=[10*3,2*3,s['forge_start'][0]+3,s['heat_end'][0]+3,s['forge_end'][0]+3]
   for now in points:
    with self.subTest(cars=cars,now=now):
     x=copy.deepcopy(sc);x['production_state']=snapshot(sc,s,now)
     result,err,stats=construct(x,[[o['id'] for o in x['orders']]],engine='extended',admission='rolling',vehicle_policy='split',backtrack_budget=16,seconds=2)
     self.assertIsNotNone(result,(err,stats));self.assertTrue(worker.audit(x,result)['valid']);self.assertTrue(worker.verify(x,copy.deepcopy(result))['valid']);self.assertTrue(audit_state(x,result)['valid'])
 def test_no_future_answers_and_no_backdating(self):
  sc,s=self.base();sc['production_state']=snapshot(sc,s,30);data=state_data(sc)
  self.assertTrue(all(t['start']<30 for t in data['tasks']))
  self.assertIsNone(data['known']['forge_start'][0])
  forged=copy.deepcopy(sc);forged['production_state']['operations'][0]['forge_start']=s['forge_start'][0]
  with self.assertRaises(ValueError):state_data(forged)
  wrong=copy.deepcopy(s);wrong['heat_start'][0]+=1
  self.assertFalse(audit_state(sc,wrong)['valid'])
 def test_cp_state_constraints_search_and_reject_mutation(self):
  sc,s=self.base();sc['production_state']=snapshot(sc,s,30)
  m,v,ops=build_model(sc);add_state_constraints(sc,m,v)
  solver=cp_model.CpSolver();solver.parameters.max_time_in_seconds=3;solver.parameters.num_search_workers=2
  status=solver.solve(m);self.assertIn(status,(cp_model.FEASIBLE,cp_model.OPTIMAL));got=extract_schedule(solver,v)
  self.assertTrue(audit_state(sc,got)['valid']);self.assertTrue(worker.audit(sc,got)['valid'])
  i=next(i for i,x in enumerate(state_data(sc)['known']['heat_start']) if x is not None);m.add(v['heat_start'][i]!=s['heat_start'][i]);self.assertEqual(solver.solve(m),cp_model.INFEASIBLE)
 def test_day_two_window_scores_overlap_correctly(self):
  sc,s=self.base();sc['score_window_start_ticks']=4320;sc['production_state']={'now_ticks':4320}
  # Scoring checks its window only; this analytic witness need not be a valid WIP input.
  for k in ['forge_start','forge_end','heat_start','heat_end']:s[k]=[x+4320 for x in s[k]]
  score=evaluate_schedule(sc,s)['score_ticks'];self.assertEqual(score,120)
  m,v,ops=build_model(self.scenario());o=add_objective(m,v,ops,sc,first=True)
  for i in range(len(ops)):m.add(v['forge_start'][i]==s['forge_start'][i]);m.add(v['heat_start'][i]==s['heat_start'][i]);m.add(v['furnace'][i]==s['furnace'][i])
  solver=cp_model.CpSolver();solver.parameters.max_time_in_seconds=3
  status=solver.solve(m);self.assertIn(status,(cp_model.OPTIMAL,cp_model.FEASIBLE));self.assertEqual(solver.value(o['score']),score)
 def test_features_distinguish_cold_and_wip(self):
  sc,s=self.base();cold=features(sc);sc['production_state']=snapshot(sc,s,30);hot=features(sc)
  self.assertEqual(cold['active_orders'],0);self.assertGreater(hot['active_orders'],0);self.assertGreater(sum(hot['heating_by_temperature'].values()),0)
 def test_remaining_work_excludes_elapsed_forging(self):
  sc,s=self.base();sc['production_state']=snapshot(sc,s,s['forge_start'][0]+3)
  got=features(sc)
  self.assertEqual(sum(got['remaining_forge_minutes'].values()),39)
 def test_complete_hints_with_observed_constants_reach_real_solver(self):
  for cars in (1,2):
   sc,s=self.base(cars)
   for now in (30,s['forge_start'][0]+3,4500):
    x=copy.deepcopy(sc);x['production_state']=snapshot(sc,s,now)
    with tempfile.TemporaryDirectory() as d:
     p=Path(d);worker.save(p/'seed.json',s)
     result=worker.solve(x,dict(seed_path=str(p/'seed.json'),mode='seeded_search',preference='none',threads=2,seconds=3,target_minutes=0,stop_at_target=True,presolve='off'),p)
     self.assertTrue(result['cp_solver_called']);self.assertTrue(result['valid']);self.assertTrue(result['audit_valid'])
     self.assertTrue(audit_state(x,json.loads((p/'best_schedule.json').read_text()))['valid'])
 def test_observed_identities_are_not_cold_start_symmetry(self):
  sc,s=self.base();sc['production_state']=snapshot(sc,s,s['forge_start'][0]+3)
  sc['orders']=list(reversed(sc['orders']))
  result,err,_=construct(sc,[[o['id'] for o in sc['orders']]],engine='extended',admission='rolling',vehicle_policy='split',seconds=2)
  self.assertIsNotNone(result,err);self.assertTrue(worker.verify(sc,copy.deepcopy(result))['valid']);self.assertTrue(audit_state(sc,result)['valid'])
 def test_snapshots_at_event_boundaries_keep_source_witness_feasible(self):
  sc,s=self.base()
  points={t[k]+d for t in s['transport'] for k in ('start','end') for d in (0,1) if t[k]+d>0}
  for now in sorted(points):
   x=copy.deepcopy(sc);x['production_state']=snapshot(sc,s,now)
   self.assertTrue(audit_state(x,s)['valid'],(now,audit_state(x,s)))

if __name__=='__main__':unittest.main()
