import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
import worker
from constructor import construct
from search_registry import claim_search, finish_search


class RepairTests(unittest.TestCase):
    def setUp(self):
        self.sc=json.loads((worker.ROOT/'configs/orders20.json').read_text())
        self.sc['transport']['vehicle_count']=1
        self.waves=[[f'{r}-{i:02d}' for r in ['C430','C280R','C280I','CFLAT']] for i in range(1,6)]

    def test_deadend_contains_actual_constraints_and_reservations(self):
        s,e,stats=construct(self.sc,self.waves,engine='extended',wip_limit=4,seconds=2)
        self.assertIsNone(s)
        self.assertEqual(e,'bounded_deadend')
        d=stats['failure_context']
        self.assertIn('hold_upper_exceeded',d['rejections'])
        self.assertEqual(len(d['vehicles']),1)
        self.assertEqual(len(d['furnaces']),6)
        self.assertTrue(d['recent_choices'])
        bad=next(x for x in d['examples'] if x['reason']=='hold_upper_exceeded')
        self.assertGreater(bad['earliest_pickup_ticks'],bad['latest_pickup_ticks'])

    def test_program_error_is_not_labeled_deadend(self):
        with tempfile.TemporaryDirectory() as d:
            result=worker.construct_batch(self.sc,dict(configs=[dict(waves=self.waves,engine='extended',
                program_source="def rank(candidates,state): return [0 if c is not None else 1 for c in candidates]")],
                seconds=1,preference='none'),Path(d))
        self.assertEqual(result['failure']['kind'],'program_error')
        self.assertEqual(result['failure']['details']['stage'],'program_validation')
        self.assertEqual(result['failure']['proof_scope'],'none')
        self.assertFalse(result['cp_solver_called'])

    def test_cp_identity_uses_effective_inputs_not_strategy_names(self):
        with tempfile.TemporaryDirectory() as d:
            reg=str(Path(d)/'registry.json')
            req=dict(seed=0,seconds=1,threads=1,preference='none',direction='broad',name='A')
            k,dup=claim_search(reg,self.sc,req,None,None,['a','b'],'task-a')
            self.assertIsNone(dup)
            same,dup=claim_search(reg,self.sc,{**req,'direction':'press_gap','name':'B'},None,None,['b','a'],'task-b')
            self.assertEqual(k,same);self.assertEqual(dup['owner'],'task-a')
            finish_search(reg,k,'task-a','UNKNOWN')
            self.assertIsNotNone(claim_search(reg,self.sc,req,None,None,['a','b'],'task-c')[1])
            self.assertIsNone(claim_search(reg,self.sc,{**req,'seed':1},None,None,['a','b'],'task-d')[1])
            self.assertIsNone(claim_search(reg,self.sc,{**req,'seconds':2},None,None,['a','b'],'task-e')[1])
            finish_search(reg,k,'task-a','CANCELLED',True)
            self.assertIsNone(claim_search(reg,self.sc,req,None,None,['a','b'],'resumed')[1])

    def test_real_cp_duplicate_is_skipped_before_solve(self):
        sc=copy.deepcopy(self.sc);sc['orders']=sc['orders'][:1]
        sc['routes']=[dict(id=sc['orders'][0]['route'],operations=[dict(fire=1,temp=1150,hold_min=60,hold_max=90,forge=10)])]
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)
            req=dict(mode='seeded_search',preference='none',threads=1,seconds=2,target_minutes=1440,
                     stop_at_target=False,registry_path=str(p/'registry.json'))
            (p/'a').mkdir();(p/'b').mkdir()
            one=worker.solve(sc,req,p/'a')
            two=worker.solve(sc,req,p/'b')
            self.assertTrue(one['valid']);self.assertTrue(one['cp_solver_called'])
            self.assertEqual(two['status'],'DUPLICATE');self.assertFalse(two['cp_solver_called'])

if __name__=='__main__': unittest.main()
