import copy,json,sys,tempfile,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).parent))
import worker
from calendar_engine import Calendar
from algorithm import AlgorithmProgram,construct_algorithm
from time_windows import route_windows


class CalendarTests(unittest.TestCase):
    def scenario(self,n=2,cars=1):
        sc=json.loads((worker.ROOT/'configs/orders20.json').read_text())
        sc['transport']['vehicle_count']=cars
        sc['orders']=[dict(id=f'A{i}',route='R',release_minutes=0,due_minutes=5760) for i in range(n)]
        sc['routes']=[dict(id='R',operations=[dict(fire=1,temp=1150,hold_min=60,hold_max=180,forge=10)])]
        return sc

    def env(self,sc):return Calendar(sc,[[o['id'] for o in sc['orders']]],seconds=20)

    def test_static_windows_match_original_cp_domains(self):
        sc=self.scenario();m,v,ops=worker.build_model(sc);bounds=route_windows(sc)
        for i,b in enumerate(bounds):
            self.assertEqual(list(v['forge_start'][i].proto.domain),[b['earliest_forge'],b['latest_forge']])
            self.assertEqual(list(v['heat_start'][i].proto.domain),[b['earliest_heat'],b['latest_heat']])

    def test_route_deadline_rejected_before_forging(self):
        sc=self.scenario(1);sc['orders'][0]['due_minutes']=100
        env=self.env(sc);opts=env.candidates(0,delays=[1000])
        self.assertEqual(opts,[])
        sc['orders'][0]['release_minutes']=90
        env=self.env(sc);self.assertFalse(env.candidates(0));self.assertGreater(env.stats['window_prunes'],0)

    def test_all_distinct_pending_routes_not_only_head(self):
        sc=self.scenario(2);sc['routes'].append(dict(id='Q',operations=copy.deepcopy(sc['routes'][0]['operations'])))
        sc['orders'][1]['route']='Q';env=self.env(sc)
        choices=env.candidates(0)
        self.assertEqual({x['order'] for x in choices},{'A0','A1'})
        self.assertGreater(len(choices),4)

    def test_insert_cold_trip_before_reserved_future_forge(self):
        sc=self.scenario();env=self.env(sc)
        a=env.advance(0,env.candidates(0)[0]['id'])
        forge=next(x for x in env.candidates(a) if x['kind']=='forge')
        b=env.advance(a,forge['id'])
        load=next(x for x in env.candidates(b) if x['kind']=='admit' and x['start']<forge['start'])
        c=env.advance(b,load['id'])
        self.assertGreater(env.stats['gap_insertions'],0)
        self.assertIsNone(env.check(env.states[c]))

    def test_generated_loop_constructs_and_audits_one_two_cars(self):
        code='''def construct():
    state=root()
    while remaining()>0:
        if complete(state): return state
        choices=candidates(state)
        if not choices: return None
        pick=sorted([(c["start"],c["id"]) for c in choices])[0][1]
        state=advance(state,pick)
    return None
'''
        for cars in (1,2):
            sc=self.scenario(cars=cars)
            s,e,stats=construct_algorithm(sc,[[o['id'] for o in sc['orders']]],code,seconds=15)
            self.assertIsNone(e);self.assertTrue(worker.audit(sc,s)['valid']);self.assertTrue(worker.verify(sc,copy.deepcopy(s))['valid'])

    def test_wrong_action_and_rule_access_rejected(self):
        env=self.env(self.scenario())
        with self.assertRaises(ValueError):env.advance(0,999999)
        for code in ['import os\ndef construct(): return 0','def construct(): return open("x")','def construct(): return root.__globals__','def construct():\n root=2\n return root']:
            with self.assertRaises((ValueError,SyntaxError)):AlgorithmProgram(code)

    def test_candidate_identity_survives_repeated_generation(self):
        env=self.env(self.scenario())
        a=env.candidates(0);b=env.candidates(0)
        self.assertEqual(a,b)
        env.advance(0,a[0]['id']);env.advance(0,a[1]['id'])
        self.assertEqual(env.stats['branch_switches'],1)

    def test_required_algorithm_api_arguments_fail_before_search(self):
        with self.assertRaisesRegex(ValueError,'waves'):
            AlgorithmProgram('def construct():\n return batches()')

    def test_runtime_error_reports_generated_source_line(self):
        sc=self.scenario(1)
        source='def construct():\n    rows=inspect(root())["order_details"]\n    return rows[{}]\n'
        with tempfile.TemporaryDirectory() as d:
            result=worker.construct_batch(sc,dict(preference='none',seconds=1,configs=[dict(waves=[['A0']],engine='calendar',algorithm_source=source)]),Path(d))
            self.assertFalse(result['valid'])
            self.assertEqual(result['failure']['details']['generated_code_line'],3)
            self.assertEqual(result['failure']['kind'],'program_error')

    def test_generated_dynamic_groups_use_current_orders(self):
        code='''def construct():
    rows=inspect(root())["order_details"]
    waves=[[r["order"]] for r in rows]
    result=batches(waves,policy=0)
    if result is not None and complete(result): return result
    return None
'''
        for n in (1,3):
            sc=self.scenario(n)
            result,error,stats=construct_algorithm(sc,[[o['id'] for o in sc['orders']]],code,seconds=10)
            self.assertIsNone(error)
            self.assertTrue(worker.audit(sc,result)['valid'])
            self.assertTrue(worker.verify(sc,copy.deepcopy(result))['valid'])
            self.assertEqual(stats['algorithm_api_calls']['batches'],1)

    def test_nested_helpers_and_sets_are_algorithm_local_only(self):
        code='''def construct():
    seen=set()
    def build(rows):
        groups=[]
        for row in rows:
            if row["order"] not in seen:
                seen.add(row["order"])
                groups.append([row["order"]])
        return groups
    return batches(build(inspect(root())["order_details"]))
'''
        sc=self.scenario(2)
        result,error,_=construct_algorithm(sc,[['A0','A1']],code,seconds=10)
        self.assertIsNone(error);self.assertTrue(worker.verify(sc,result)['valid'])
        with self.assertRaises(ValueError):AlgorithmProgram('def construct():\n def check(root): return root\n return check(0)')

    def test_explicit_frontier_checkpoint_resume(self):
        code='''def construct():
    saved=restore()
    state=saved["state"] if saved else root()
    if complete(state): return state
    choices=candidates(state)
    if not choices: return None
    state=advance(state,choices[0]["id"])
    checkpoint({"state":state})
    return state if complete(state) else None
'''
        sc=self.scenario(1)
        with tempfile.TemporaryDirectory() as d:
            path=str(Path(d)/'checkpoint.json')
            a,e,_=construct_algorithm(sc,[['A0']],code,checkpoint_path=path)
            self.assertIsNone(a);self.assertTrue(Path(path).exists())
            b,e,_=construct_algorithm(sc,[['A0']],code,checkpoint_path=path)
            self.assertIsNone(e);self.assertTrue(worker.audit(sc,b)['valid'])

    def test_cp_prefix_probe_checks_transport_and_returns_scope(self):
        sc=self.scenario(1);env=self.env(sc)
        sid=env.advance(0,env.candidates(0)[0]['id'])
        r=env.probe(sid,1)
        self.assertIn(r['status'],['OPTIMAL','FEASIBLE','UNKNOWN']);self.assertFalse(r['can_prune'])

    def test_wip_anchors_cannot_move(self):
        from production_state import snapshot,audit_state
        sc=self.scenario(1)
        s,_,_=worker.construct_extended(sc,[['A0']],engine='legacy')
        sc['production_state']=snapshot(sc,s,30*sc['ticks_per_minute'])
        env=self.env(sc);opts=env.candidates(0)
        self.assertTrue(opts);sid=env.advance(0,next(x['id'] for x in opts if x['kind']=='forge'))
        result=env.result(sid)
        self.assertTrue(audit_state(sc,result)['valid']);self.assertTrue(worker.audit(sc,result)['valid'])

    def test_necessary_checks_do_not_reject_known_multifire_witness(self):
        for cars in (1,2):
            sc=self.scenario(3,cars)
            sc['routes'][0]['operations'] += [dict(fire=2,temp=1130,hold_min=60,hold_max=180,forge=10)]
            s,e,_=worker.construct_extended(sc,[[o['id'] for o in sc['orders']]],engine='legacy')
            self.assertIsNotNone(s,e)
            self.assertTrue(worker.audit(sc,s)['valid'])
            env=self.env(sc)
            st=dict(s={k:copy.deepcopy(s[k]) for k in env.initial['s']},tasks=copy.deepcopy(s['transport']),choices=[])
            self.assertIsNone(env.check(st))

    def test_press_exchange_is_rejected_before_final_audit(self):
        sc=self.scenario(2);env=self.env(sc)
        a=env.advance(0,env.candidates(0)[0]['id'])
        b=env.advance(a,next(x['id'] for x in env.candidates(a) if x['kind']=='admit'))
        c=env.advance(b,next(x['id'] for x in env.candidates(b) if x['kind']=='forge' and x['op']==0))
        e=env.states[c]['s']['forge_end'][0]
        self.assertFalse(any(x['kind']=='forge' and x['start']==e for x in env.candidates(c)))

if __name__=='__main__':unittest.main()
