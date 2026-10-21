"""Resource-calendar constructor environment, independent of generated search code.

An action reserves one cold admission or one forge AND its immediate outgoing
trip. Existing future reservations do not advance an artificial global clock.
Unfinished holds occupy at least their mandatory minimum; subsequent choices
must recheck the entire calendar. Passing this optimistic partial check is NOT
a guarantee that all outstanding holds can be closed.
"""
import copy
import hashlib
import json
import time
from collections import defaultdict, OrderedDict
from runtime_metrics import Metrics, measured
from state_store import StateStore
from functools import lru_cache
from longda_cpsat.model import flatten, ticks
from production_state import state_data, ramp_from, temperature
from time_windows import route_windows, contradictions


class SearchBudget(Exception):
    pass


class Calendar:
    def __init__(self, sc, waves, seconds=3, incumbent=None, preference='none', incremental=True, compare_checks=False):
        self.metrics=Metrics();self.incremental=incremental;self.compare_checks=compare_checks
        self.candidate_cache=OrderedDict();self.branch_results={};self.cp_template=None
        self.sc = copy.deepcopy(sc)
        self.ops = flatten(sc)
        self.ramp = lru_cache(maxsize=4096)(lambda a,b: ramp_from(self.sc,a,b))
        self.by = defaultdict(list)
        for q in self.ops:
            self.by[q['order']].append(q['index'])
        self.pending_order = [o for w in waves for o in w]
        if sorted(self.pending_order) != sorted(self.by):
            raise ValueError('Every input order must appear once')
        self.tau = ticks(sc['transport']['travel_minutes'], sc['ticks_per_minute'])
        self.F = sc['furnace_count']; self.press = self.F+1; self.finished = self.F+2
        self.horizon = ticks(sc['horizon_minutes'], sc['ticks_per_minute'])
        self.observed = state_data(sc)
        self.now = self.observed['now'] if self.observed else 0
        self.bounds = route_windows(sc)
        self.deadline = time.monotonic()+seconds
        self.started = time.monotonic()
        n = len(self.ops)
        self.initial = dict(s={k:[None]*n for k in ('heat_start','heat_end','forge_start','forge_end','furnace')}, tasks=[], choices=[])
        if self.observed:
            self.initial['s'] = copy.deepcopy(self.observed['known'])
            self.initial['tasks'] = copy.deepcopy(self.observed['tasks'])
        self.stats = dict(nodes=0, branch_switches=0, pruned=0, candidates=0,
                          window_prunes=0, gap_insertions=0, cp_probes=0,
                          completed_operations=0, failure_context=None)
        self.best = copy.deepcopy(self.initial)
        self.recent_failures = []
        self.states = StateStore(self.proposed,self.initial); self.next_id = 1
        self.action_sets = {}; self.next_action = 0
        self.action_index = {}
        self.last_advanced_sid = 0
        self.incumbent = incumbent; self.preference = preference

    def budget(self):
        if time.monotonic() >= self.deadline:
            raise SearchBudget('construction_budget')

    def remaining(self):
        return max(0, self.deadline-time.monotonic())

    def root(self):
        return 0

    def complete(self, sid):
        st = self.states[sid]
        outs = {t['op'] for t in st['tasks'] if t['kind'].startswith('press_to_')}
        return len(outs) == len(self.ops)

    def inspect(self, sid):
        st = self.states[sid]; s = st['s']
        done = sum(x is not None for x in s['forge_start'])
        active = [q['index'] for q in self.ops if s['heat_start'][q['index']] is not None and s['forge_start'][q['index']] is None]
        return dict(completed_operations=done, total_operations=len(self.ops),
                    pending_orders=[o for o in self.pending_order if s['heat_start'][self.by[o][0]] is None],
                    order_details=[dict(order=o,route=self.ops[self.by[o][0]]['route'],release=self.ops[self.by[o][0]]['release'],due=self.ops[self.by[o][0]]['due'],remaining_min_ticks=sum(q['lower']+q['duration']+2*self.tau for q in [self.ops[i] for i in self.by[o]] if s['forge_start'][q['index']] is None)) for o in self.pending_order],
                    active_operations=active, now=self.now,
                    active_count=len(active), day_end=(self.now//(1440*self.sc['ticks_per_minute'])+1)*1440*self.sc['ticks_per_minute'],
                    remaining_seconds=self.remaining(), recent_choices=copy.deepcopy(st['choices'][-8:]),
                    day_busy_ticks=sum(max(0,min((self.now//(1440*self.sc['ticks_per_minute'])+1)*1440*self.sc['ticks_per_minute'],s['forge_end'][i])-max(self.now//(1440*self.sc['ticks_per_minute'])*1440*self.sc['ticks_per_minute'],s['forge_start'][i])) for i in range(len(self.ops)) if s['forge_start'][i] is not None),
                    has_observed_state=bool(self.observed),furnace_capacity=self.sc['capacity'],units='integer ticks', ticks_per_minute=self.sc['ticks_per_minute'])

    def batches(self, waves, policy=0, seed=0):
        """Reusable cold-start primitive: RECOMPUTE groups from current input.

        Generated code may implement its own grouping/enumeration around this
        fast routine, then compare resulting states or use calendar primitives.
        No saved timetable is loaded. Observed WIP requires the calendar path.
        """
        self.budget()
        if self.observed:raise ValueError('batches is cold-start only; use calendar API for observed WIP')
        if type(policy)!=int or not 0<=policy<=5:raise ValueError('policy must be 0..5')
        if sorted(o for w in waves for o in w)!=sorted(self.by):raise ValueError('batches must cover every current order exactly once')
        from legacy_constructor import construct
        from physical_model import audit
        schedule,error=construct(self.sc,copy.deepcopy(waves),seed=seed,policy=policy)
        self.stats['batch_attempts']=self.stats.get('batch_attempts',0)+1
        if schedule is None:
            self.fail('batch_constructor_deadend',message=error);return None
        checked=audit(self.sc,schedule)
        if not checked['valid']:
            self.fail('batch_audit_failed',errors=checked['errors'][:3]);return None
        st=dict(s={k:copy.deepcopy(schedule[k]) for k in self.initial['s']},tasks=copy.deepcopy(schedule['transport']),choices=[])
        ident=self.next_id;self.next_id+=1;self.states[ident]=st
        self.stats['completed_operations']=len(self.ops);self.best=copy.deepcopy(st)
        return ident

    def fail(self, reason, **details):
        item = dict(reason=reason, **details)
        self.recent_failures.append(item)
        self.recent_failures = self.recent_failures[-24:]
        if reason=='remaining_route_deadline': self.stats['window_prunes'] += 1
        return item

    @measured('check')
    def check(self, st, affected=None):
        """Necessary checks for ALL reservations, not just the most recent fire."""
        s = st['s']; tau = self.tau
        for q in self.ops:
            if affected is not None and q['index'] not in affected['ops']:continue
            i=q['index'];a=s['heat_start'][i];x=s['heat_end'][i]
            b=self.bounds[i]
            # Every newly committed heat start already follows its predecessor
            # exactly. Static suffix bounds therefore expose route lateness
            # without re-flattening and re-deriving the same route per candidate.
            if a is not None and (a>b['latest_heat'] or (s['forge_start'][i] is not None and s['forge_start'][i]>b['latest_forge'])):
                return dict(reason='remaining_route_deadline',op=i,order=q['order'],fire=q['fire'],committed_heat_ticks=a,**b,proof_scope='current_partial_commitments')
            if a is not None and a < q['release']:
                return dict(reason='release', op=i)
            if x is not None and not q['lower'] <= x-a <= q['upper']:
                return dict(reason='hold_window', op=i)
        intervals=sorted((s['forge_start'][i],s['forge_end'][i],i) for i in range(len(self.ops)) if s['forge_start'][i] is not None)
        for x,y in zip(intervals, intervals[1:]):
            if x[1]>y[0]:return dict(reason='press_overlap', operations=[x[2],y[2]])
        # A full car cannot drop a new material onto an occupied press and then
        # pick the old one at that same instant. Different cars may hand over.
        incoming=[t for t in st['tasks'] if t['kind']=='furnace_to_press']
        outgoing=[t for t in st['tasks'] if t['kind'].startswith('press_to_')]
        for a in incoming:
            for b in outgoing:
                if a['end']==b['start'] and a['vehicle']==b['vehicle'] and a['op']!=b['op']:
                    return dict(reason='forbidden_press_exchange',vehicle=a['vehicle'],operations=[a['op'],b['op']],at=a['end'])
        for car in range(self.sc['transport']['vehicle_count']):
            if affected is not None and car not in affected['cars']:continue
            trips=sorted([t for t in st['tasks'] if t['vehicle']==car],key=lambda t:(t['start'],t['end']))
            at,pos=0,0
            for t in trips:
                if t['start']<at+(0 if pos==t['source'] else tau):
                    return dict(reason='vehicle_chain', vehicle=car, op=t['op'], earliest_pickup=at+(0 if pos==t['source'] else tau), requested_pickup=t['start'])
                if self.observed and t['start']>=self.now:
                    row=self.observed['raw']['vehicles'][car]
                    if at<=self.now:
                        at,pos=row['available_at_ticks'],row['location']
                        if t['start']<at+(0 if pos==t['source'] else tau):return dict(reason='observed_vehicle',vehicle=car,op=t['op'])
                at,pos=t['end'],t['destination']
        for f in range(self.F):
            if affected is not None and f not in affected['furnaces']:continue
            holds=[]
            for q in self.ops:
                i=q['index'];a=s['heat_start'][i]
                if a is None or s['furnace'][i]!=f:continue
                x=s['heat_end'][i] if s['heat_end'][i] is not None else a+q['lower']
                holds.append((a,x,q['temp'],i))
                if a<self.ramp(self.sc['initial_temperature'],q['temp']):return dict(reason='initial_ramp',op=i,furnace=f)
                if self.observed and self.observed['known']['heat_start'][i] is None:
                    ready=self.now+self.ramp(temperature(self.observed['raw']['furnaces'][f]),q['temp'])
                    if a<ready:return dict(reason='observed_temperature',op=i,furnace=f,ready=ready)
            holds.sort(); ev=[]
            for j,(a,x,temp,i) in enumerate(holds):
                ev += [(a,1),(x,-1)]
                for b,y,other,k in holds[:j]:
                    if temp!=other and y+self.ramp(other,temp)>a:
                        return dict(reason='furnace_temperature',furnace=f,operations=[k,i],earliest_heat=y+self.ramp(other,temp))
            count=0
            for at,delta in sorted(ev):
                count+=delta
                if count>self.sc['capacity']:return dict(reason='furnace_capacity',furnace=f,at=at)
        return None

    def car_starts(self, st, car, source, destination, lo, hi):
        """Earliest insertion in EACH vehicle gap, including required empty travel."""
        tau=self.tau
        trips=sorted([t for t in st['tasks'] if t['vehicle']==car],key=lambda t:t['start'])
        at,pos=0,0
        if self.observed:
            row=self.observed['raw']['vehicles'][car];at,pos=row['available_at_ticks'],row['location']
            trips=[t for t in trips if t['start']>=self.now]
        starts=[]
        for nxt in trips+[None]:
            earliest=max(lo,at+(0 if pos==source else tau),self.now)
            latest=min(hi,nxt['start']-tau-(0 if destination==nxt['source'] else tau)) if nxt else hi
            if earliest<=latest:starts.append(earliest)
            if nxt:at,pos=nxt['end'],nxt['destination']
        return starts

    def check_action(self, parent, child, action):
        i=action['op'];chain=self.by[self.ops[i]['order']];pos=chain.index(i)
        ops={i}
        if action['kind']!='admit' and pos+1<len(chain):ops.add(chain[pos+1])
        affected=dict(ops=ops,cars={action['car'],action['pickup_car']},
                      furnaces={action['furnace'],action['destination']})
        bad=self.check(child,affected if self.incremental else None)
        if self.compare_checks:
            full=self.check(child)
            if bool(bad)!=bool(full):raise AssertionError('Incremental/full feasibility mismatch')
            self.stats['differential_checks']=self.stats.get('differential_checks',0)+1
        return bad

    def branch_status(self,sid):
        if sid not in self.states:raise ValueError('Unknown state')
        return copy.deepcopy(self.branch_results.get(sid,dict(kind='NOT_EXPANDED',proof_scope='none')))

    @measured('state_copy')
    def proposed(self, st, x):
        out=dict(s={k:list(v) for k,v in st['s'].items()},tasks=list(st['tasks']),choices=list(st['choices']));s=out['s'];i=x['op'];q=self.ops[i];tau=self.tau
        def trip(kind,source,dest,start,car):
            out['tasks'].append(dict(kind=kind,order=q['order'],op=i,source=source,destination=dest,start=start,end=start+tau,vehicle=car,pickup_rank=0,dropoff_rank=0))
        if x['kind']=='admit':
            a=x['start'];f=x['furnace'];s['heat_start'][i]=a;s['furnace'][i]=f
            trip('cold_to_furnace',0,f+1,a-tau,x['car'])
        else:
            start=x['start'];end=start+q['duration'];f=s['furnace'][i]
            if x['kind']!='finish':
                s['forge_start'][i]=start;s['forge_end'][i]=end;s['heat_end'][i]=start-tau
                trip('furnace_to_press',f+1,self.press,start-tau,x['car'])
            chain=self.by[q['order']];pos=chain.index(i);j=chain[pos+1] if pos+1<len(chain) else None
            dest=x['destination']
            trip('press_to_furnace' if j is not None else 'press_to_finished',self.press,dest+1 if j is not None else self.finished,end,x['pickup_car'])
            if j is not None:s['heat_start'][j]=end+tau;s['furnace'][j]=dest
        out['choices'].append({k:v for k,v in x.items() if k!='id'})
        return out

    @measured('candidates')
    def candidates(self, sid, orders=None, delays=None):
        """Expose all generated choices, never a hidden top-four cap.

        Critical-event starts plus caller-selected nonnegative tick delays form
        a heuristic candidate domain, NOT exhaustive continuous-time search.
        """
        self.budget();st=self.states[sid];s=st['s'];tau=self.tau
        # Optimistic daily bound still works when inserting into EARLIER gaps.
        # Never use latest-reservation time as a global elapsed-time cutoff.
        if self.incumbent:
            L=self.now//(1440*self.sc['ticks_per_minute'])*1440*self.sc['ticks_per_minute'];D=L+1440*self.sc['ticks_per_minute']
            bounds=route_windows(self.sc,s,self.ops)
            busy=sum(max(0,min(D,s['forge_end'][i])-max(L,s['forge_start'][i])) for i in range(len(self.ops)) if s['forge_start'][i] is not None)
            possible=sum(max(0,min(q['duration'],D-max(L,bounds[q['index']]['earliest_forge']))) for q in self.ops if s['forge_start'][q['index']] is None)
            upper=busy+min(max(0,D-L-busy),possible)
            best=self.incumbent['score_ticks']
            if upper<best or (upper==best and self.preference=='none'):
                self.stats['pruned']+=1;self.fail('day_score_upper_bound',upper_ticks=upper,incumbent_ticks=best,proof_scope='no_primary_improvement_for_fixed_partial');self.branch_results[sid]=dict(kind='BOUND_PRUNED',upper_ticks=upper,incumbent_ticks=best,proof_scope='fixed_partial');return []
        selected=set(orders) if orders is not None else set(self.by)
        if not selected <= set(self.by):raise ValueError('Unknown candidate order')
        delays=[0] if delays is None else list(delays)
        if len(delays)>128 or any(type(x)!=int or x<0 for x in delays):raise ValueError('delays must be <=128 nonnegative integer ticks')
        cache_key=(sid,tuple(sorted(selected)),tuple(delays))
        if cache_key in self.candidate_cache:
            self.stats['candidate_cache_hits']=self.stats.get('candidate_cache_hits',0)+1
            self.candidate_cache.move_to_end(cache_key)
            return [dict(x) for x in self.candidate_cache[cache_key]]
        result=[];seen=set();rejects=defaultdict(int)
        def accept(x):
            self.budget()
            key=tuple((k,v) for k,v in x.items())
            if key in seen:return
            seen.add(key)
            child=self.proposed(st,x)
            bad=self.check_action(st,child,x)
            if bad:
                rejects[bad['reason']]+=1
                self.fail(**bad);return
            # The same state/action keeps its identity across repeated calls.
            # Model-owned backtracking can therefore keep a reliable tried set.
            stable=(sid,json.dumps(x,sort_keys=True))
            ident=self.action_index.get(stable)
            if ident is None:
                ident=self.next_action;self.next_action+=1
                self.action_index[stable]=ident
            x['id']=ident;self.action_sets[ident]=(sid,copy.deepcopy(x))
            result.append(x)
        # Endpoint candidates include gaps BEFORE existing future reservations.
        events={self.now}
        for t in st['tasks']:events.update((t['start'],t['end'],t['end']+tau))
        for q in self.ops:
            i=q['index'];a=s['heat_start'][i]
            if a is not None:events.update((a,a+q['lower'],a+q['upper']))
        out_ops={t['op'] for t in st['tasks'] if t['kind'].startswith('press_to_')}
        unfinished=[i for i in range(len(self.ops)) if s['forge_start'][i] is not None and i not in out_ops]
        for o in self.pending_order:
            self.budget()
            if o not in selected:continue
            i=self.by[o][0];q=self.ops[i]
            if s['heat_start'][i] is not None:continue
            lo=max(self.now+tau,q['release']+tau,self.bounds[i]['earliest_heat']);hi=self.bounds[i]['latest_heat']
            if lo>hi:
                self.fail('remaining_route_deadline',op=i,order=o,earliest_heat=lo,latest_heat=hi);continue
            # Equivalent cold identities only when input quotient permits it and no observations.
            if not self.observed and self.sc.get('quotient_identical_orders',True):
                prev=[p for p in self.pending_order[:self.pending_order.index(o)] if self.ops[self.by[p][0]]['route']==q['route'] and self.ops[self.by[p][0]]['release']==q['release'] and self.ops[self.by[p][0]]['due']==q['due'] and p in selected and s['heat_start'][self.by[p][0]] is None]
                if prev:continue
            for f in range(self.F):
                ready=self.ramp(self.sc['initial_temperature'],q['temp'])
                points={max(lo,ready)}
                for j,r in enumerate(self.ops):
                    if s['heat_start'][j] is not None and s['furnace'][j]==f:
                        exit=s['heat_end'][j] if s['heat_end'][j] is not None else s['heat_start'][j]+r['lower']
                        points.add(max(lo,exit+self.ramp(r['temp'],q['temp'])))
                if self.observed:points.add(max(lo,self.now+self.ramp(temperature(self.observed['raw']['furnaces'][f]),q['temp'])))
                for car in range(self.sc['transport']['vehicle_count']):
                    for p in sorted(points):
                        for d in delays:
                            for depart in self.car_starts(st,car,0,f+1,p+d-tau,hi-tau):
                                accept(dict(kind='admit',op=i,order=o,route=q['route'],furnace=f,car=car,pickup_car=car,destination=f,start=depart+tau,duration=0,fire=q['fire'],slack=hi-depart-tau,min_hold=q['lower'],temperature=q['temp']))
        for q in self.ops:
            self.budget();i=q['index']
            if q['order'] not in selected or s['heat_start'][i] is None or (s['forge_start'][i] is not None and i not in unfinished):continue
            finish=i in unfinished;f=s['furnace'][i];a=s['heat_start'][i];chain=self.by[q['order']];pos=chain.index(i);j=chain[pos+1] if pos+1<len(chain) else None
            lo=max(self.now+tau,a+q['lower']+tau);hi=min(a+q['upper']+tau,self.bounds[i]['latest_forge'])
            if finish:lo=hi=s['forge_start'][i]
            if lo>hi:
                self.fail('remaining_route_deadline',op=i,order=q['order'],earliest_forge=lo,latest_forge=hi);continue
            points={lo,hi}
            for t in st['tasks']:points.update((max(lo,t['end']+tau),max(lo,t['end']+2*tau-q['duration'])))
            for e in s['forge_end']:
                if e is not None:points.add(max(lo,e))
            for dest in (range(self.F) if j is not None else [-1]):
                local=set(points)
                if j is not None:
                    r=self.ops[j]
                    for k,other in enumerate(self.ops):
                        if s['heat_start'][k] is not None and s['furnace'][k]==dest:
                            exit=s['heat_end'][k] if s['heat_end'][k] is not None else s['heat_start'][k]+other['lower']
                            local.add(max(lo,exit+self.ramp(other['temp'],r['temp'])-q['duration']-tau))
                for car in range(self.sc['transport']['vehicle_count']):
                    for p in sorted(local):
                        if p>hi:continue
                        for delay in delays:
                            starts=[lo] if finish else [x+tau for x in self.car_starts(st,car,f+1,self.press,p+delay-tau,hi-tau)]
                            for start in starts:
                                if not lo<=start<=hi:continue
                                for pickup in range(self.sc['transport']['vehicle_count']):
                                    accept(dict(kind='finish' if finish else 'forge',op=i,order=q['order'],route=q['route'],furnace=f,car=car,pickup_car=pickup,destination=dest,start=start,duration=q['duration'],fire=q['fire'],slack=hi-start,min_hold=q['lower'],temperature=q['temp']))
        self.stats['candidates']+=len(result)
        self.stats['peak_candidates_per_call']=max(self.stats.get('peak_candidates_per_call',0),len(result))
        self.branch_results[sid]=dict(kind='OPEN' if result else 'CANDIDATE_DOMAIN_EMPTY',rejections=dict(rejects),proof_scope='generated_candidate_domain',global_infeasible=False)
        self.candidate_cache[cache_key]=result
        while len(self.candidate_cache)>16:self.candidate_cache.popitem(last=False)
        if not result:
            self.stats['failure_context']=dict(state=self.inspect(sid),rejections=dict(rejects),examples=copy.deepcopy(self.recent_failures),interpretation='No generated candidate; necessary-check failures do not prove global infeasibility.')
        return [dict(x) for x in result]

    @measured('advance')
    def advance(self, sid, action):
        self.budget()
        if type(action)!=int or action not in self.action_sets or self.action_sets[action][0]!=sid:raise ValueError('Action must be an issued candidate for this state')
        x=self.action_sets[action][1];st=self.proposed(self.states[sid],x)
        # Issued actions were checked against this immutable parent. No second full scan.
        self.stats['reused_candidate_checks']=self.stats.get('reused_candidate_checks',0)+1
        if sid != self.last_advanced_sid:self.stats['branch_switches']+=1
        self.stats['nodes']+=1
        if x['start']<max([t['end'] for t in self.states[sid]['tasks']]+[self.now]):self.stats['gap_insertions']+=1
        done=sum(v is not None for v in st['s']['forge_start'])
        if done>self.stats['completed_operations']:
            self.stats['completed_operations']=done;self.best=copy.deepcopy(st)
        ident=self.next_id;self.next_id+=1;self.states.add(ident,sid,x,st)
        self.last_advanced_sid=ident
        return ident

    @measured('cp_probe')
    def probe(self, sid, seconds=.2):
        """A bounded full-model continuation check; UNKNOWN never prunes."""
        from physical_model import build_model
        from production_state import add_state_constraints
        from ortools.sat.python import cp_model
        self.budget();st=self.states[sid]
        # Future cold identities are canonicalized only on final delivery. A
        # prefix must not be falsely rejected merely for interchangeable names.
        if self.cp_template is None:
            started=time.perf_counter()
            m,v,_=build_model({**self.sc,'quotient_identical_orders':False});add_state_constraints(self.sc,m,v)
            self.cp_template=(m,v)
            self.stats['cp_template_build_s']=time.perf_counter()-started
        template,v=self.cp_template
        m=template.clone()  # same variable indexes; no claim of retained learned clauses
        self.stats['cp_template_clones']=self.stats.get('cp_template_clones',0)+1
        for k,values in st['s'].items():
            for i,value in enumerate(values):
                if value is not None:m.add(v[k][i]==value)
        tasks_by={(t['op'],t['kind']):t for t in st['tasks']}
        # Frozen model exposes canonical transports via helper; bind their car and event ranks.
        for t in v['_transport']:
            saved=tasks_by.get((t['op'],t['kind']))
            if saved:
                m.add(t['vehicle']==saved['vehicle'])
        if self.remaining()<=0:return dict(status='UNKNOWN',proof_scope='fixed_partial_times_and_vehicle_assignments',can_prune=False)
        solver=cp_model.CpSolver();solver.parameters.max_time_in_seconds=min(max(.01,float(seconds)),2,self.remaining());solver.parameters.num_search_workers=1
        self.stats['cp_probes']+=1
        status=solver.solve(m)
        return dict(status=solver.status_name(status),proof_scope='fixed_partial_times_and_vehicle_assignments',can_prune=status==cp_model.INFEASIBLE)

    def result(self, sid):
        if sid is None or not self.complete(sid):return None
        from constructor import finalize
        s=copy.deepcopy(self.states[sid]['s']);s['transport']=copy.deepcopy(self.states[sid]['tasks'])
        return finalize(self.sc,s)

    def diagnostics(self):
        return dict(**self.stats, performance=self.metrics.snapshot(), state_storage=dict(total_handles=len(self.states),materialized=len(self.states.cache),reconstructed=self.states.reconstructions,stored_actions=len(self.action_sets)), branch_statuses=copy.deepcopy(self.branch_results), recent_rejections=copy.deepcopy(self.recent_failures),
                    partial=copy.deepcopy(self.best['s']),elapsed_s=time.monotonic()-self.started,
                    proof_scope='none',candidate_domain='critical events plus explicit delays; not exhaustive')
