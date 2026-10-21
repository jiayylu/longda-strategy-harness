import copy,json,os,random,sys,tempfile,time,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).parent))
import worker
import test_calendar
from calendar_engine import Calendar
from algorithm import construct_algorithm,AlgorithmProgram
from state_store import StateStore

class V6Tests(unittest.TestCase):
    def scenario(self,n=3,cars=1):
        sc=test_calendar.CalendarTests().scenario(n,cars)
        sc['routes'][0]['operations'] += [dict(fire=2,temp=1130,hold_min=60,hold_max=180,forge=10)]
        return sc
    def test_incremental_full_differential(self):
        counts=0
        for cars in (1,2):
            for seed in range(3):
                sc=self.scenario(3,cars);e=Calendar(sc,[[o['id'] for o in sc['orders']]],20,compare_checks=True)
                rng=random.Random(seed);states=[0]
                for _ in range(18):
                    sid=rng.choice(states[-6:]);opts=e.candidates(sid,delays=[0,3,15])
                    if not opts:continue
                    child=e.advance(sid,rng.choice(opts)['id']);states.append(child)
                    self.assertIsNone(e.check(e.states[child]))
                counts+=e.stats.get('differential_checks',0)
        self.assertGreater(counts,1000)
    def test_eviction_never_deletes_handles_or_checkpoint_state(self):
        sc=self.scenario();e=Calendar(sc,[[o['id'] for o in sc['orders']]],20)
        e.states.cache_size=1
        opts=e.candidates(0);a=e.advance(0,opts[0]['id']);expected=copy.deepcopy(e.states[a])
        for c in opts[1:8]:e.advance(0,c['id'])
        self.assertEqual(e.states[a],expected)
        records=json.loads(json.dumps(e.states.dump()));other=StateStore(e.proposed,e.initial,1);other.load(records)
        self.assertEqual(other[a],expected)
        self.assertLessEqual(len(e.states.cache),1)
    def test_repeated_candidates_reuse_checks_and_advance_does_not_rescan(self):
        sc=self.scenario();e=Calendar(sc,[[o['id'] for o in sc['orders']]],10)
        a=e.candidates(0);checks=e.metrics.rows['check']['calls'];b=e.candidates(0)
        self.assertEqual(a,b);e.advance(0,b[0]['id']);self.assertEqual(e.metrics.rows['check']['calls'],checks)
        self.assertIn('peak_rss_mb',e.diagnostics()['performance'])
    def test_bound_pruning_is_explicit_not_infeasibility(self):
        sc=self.scenario();e=Calendar(sc,[[o['id'] for o in sc['orders']]],10,incumbent={'score_ticks':999999})
        self.assertEqual(e.candidates(0),[]);self.assertEqual(e.branch_status(0)['kind'],'BOUND_PRUNED')
    def test_infinite_generated_loop_ends_without_opcode_tracing(self):
        sc=self.scenario(1);begin=time.monotonic()
        result,error,stats=construct_algorithm(sc,[['A0']], 'def construct():\n while True:\n  x=1\n',seconds=.03)
        self.assertIsNone(result);self.assertIn('budget',error);self.assertLess(time.monotonic()-begin,1)
        self.assertEqual(stats['outcome_kind'],'BUDGET_EXHAUSTED')
    def test_cp_template_reused_and_unknown_not_pruned(self):
        sc=self.scenario(1);e=Calendar(sc,[['A0']],10)
        e.probe(0,.01);r=e.probe(0,.01)
        self.assertEqual(e.stats['cp_template_clones'],2)
        self.assertEqual(r['can_prune'],r['status']=='INFEASIBLE')
    def test_default_recomputes_groups_and_dual_audits(self):
        sc=self.scenario(3)
        s,error,d=construct_algorithm(sc,[[o['id'] for o in sc['orders']]],seconds=5)
        self.assertIsNone(error);self.assertTrue(worker.audit(sc,s)['valid']);self.assertTrue(worker.verify(sc,copy.deepcopy(s))['valid'])

if __name__=='__main__':unittest.main()
