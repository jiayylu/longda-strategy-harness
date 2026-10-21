import copy,json,tempfile,unittest,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).parent))
import worker
from partial_completion import describe_partial,add_partial_hint
from search_registry import claim_search

class PartialCompletionTests(unittest.TestCase):
    def setUp(self):
        self.sc=json.loads((worker.ROOT/'configs/orders20.json').read_text())
        self.sc['transport']['vehicle_count']=1
        self.sc['orders']=[dict(id='A',route='R',release_minutes=0,due_minutes=5760)]
        self.sc['routes']=[dict(id='R',operations=[dict(fire=1,temp=1150,hold_min=60,hold_max=90,forge=10)])]
        self.partial=dict(heat_start=[300],heat_end=[None],forge_start=[210],forge_end=[None],furnace=[0])
    def test_partial_classification(self):
        self.assertTrue(describe_partial(self.sc,self.partial,'construction_budget')['eligible'])
        for kind in ['program_error','score_bound','audit_failed']:
            self.assertFalse(describe_partial(self.sc,self.partial,kind)['eligible'])
        malformed=copy.deepcopy(self.partial);malformed['furnace']=[True]
        self.assertFalse(describe_partial(self.sc,malformed,'construction_budget')['eligible'])
    def test_partial_hint_adds_no_constraints_or_fixed_values(self):
        m,v,_=worker.build_model(self.sc);n=len(m.proto.constraints)
        r=add_partial_hint(m,v,self.partial)
        self.assertEqual(r['count'],3);self.assertEqual(len(m.proto.constraints),n)
    def test_partial_priority_even_with_full_incumbent_and_mutable_bad_timing(self):
        seed,err=worker.construct(self.sc,[['A']]);self.assertIsNone(err)
        with tempfile.TemporaryDirectory() as d:
            out=Path(d);worker.save(out/'seed.json',seed);worker.save(out/'partial.json',self.partial)
            req=dict(mode='partial_complete',preference='none',threads=2,seconds=8,
                     target_minutes=1440,stop_at_target=False,partial_path=str(out/'partial.json'),
                     seed_path=str(out/'seed.json'),incumbent_path=str(out/'seed.json'))
            r=worker.solve(self.sc,req,out)
            self.assertTrue(r['valid']);self.assertTrue(r['audit_valid'])
            self.assertEqual(r['hint_source'],'current_partial');self.assertEqual(r['hint_values'],3)
            self.assertFalse(r['baseline_improvement_required']);self.assertFalse(r['primary_optimal'])
            self.assertEqual(r['day_minutes'],10)
            s=json.loads((out/'best_schedule.json').read_text())
            self.assertGreaterEqual(s['heat_end'][0]-s['heat_start'][0],180)
            self.assertFalse(s['heat_start'][0]==300 and s['forge_start'][0]==210)
    def test_dedup_distinguishes_completion_from_improvement_and_partial_content(self):
        with tempfile.TemporaryDirectory() as d:
            out=Path(d);worker.save(out/'partial.json',self.partial)
            req=dict(mode='partial_complete',partial_path=str(out/'partial.json'),seconds=1,threads=1)
            path=str(out/'registry.json')
            a,prev=claim_search(path,self.sc,req,None,None,['A'],'a');self.assertIsNone(prev)
            b,prev=claim_search(path,self.sc,req,None,None,['A'],'b');self.assertEqual(a,b);self.assertIsNotNone(prev)
            c,_=claim_search(path,self.sc,{**req,'mode':'broad_improve'},None,None,['A'],'c');self.assertNotEqual(a,c)
            other=copy.deepcopy(self.partial);other['heat_start']=[301];worker.save(out/'partial.json',other)
            e,_=claim_search(path,self.sc,req,None,None,['A'],'e');self.assertNotEqual(a,e)

if __name__=='__main__':unittest.main()
