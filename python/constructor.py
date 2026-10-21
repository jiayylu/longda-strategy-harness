"""Construct legal witnesses using bounded action search; rules stay frozen.

Legacy batch, rolling admission, independent pickup cars, bounded backtracking,
and generated pure ranking programs share the same full-model audit boundary.
"""
import copy
import random
import time
from collections import defaultdict
from longda_cpsat.model import flatten, ramp_ticks, ticks
from scoring import evaluate_schedule, window
from production_state import state_data, ramp_from, temperature
from legacy_constructor import construct as legacy
from program import Program


def finalize(sc, s):
    tasks=s['transport']; count=sc['furnace_count']; press=count+1; finished=count+2
    order={'cold_to_furnace':0,'furnace_to_press':1,'press_to_furnace':2,'press_to_finished':2}
    tasks.sort(key=lambda x:(x['op'],order[x['kind']]))
    events=defaultdict(list)
    for i,t in enumerate(tasks):
        t['id']=i;events[t['start']].append((True,t));events[t['end']].append((False,t))
    occupants={i:set() for i in range(1,finished+1)}
    loaded=[None]*sc['transport']['vehicle_count']; rank=0
    for at,ev in sorted(events.items()):
        while ev:
            progressed=False
            for k,(pickup,t) in enumerate(ev):
                car=t['vehicle'];material=t['order'];loc=t['source'] if pickup else t['destination']
                ok=(loaded[car] is None and (loc==0 or material in occupants[loc])) if pickup else (loaded[car]==material and (loc==finished or len(occupants[loc])<(1 if loc==press else sc['capacity'])))
                if not ok:continue
                t['pickup_rank' if pickup else 'dropoff_rank']=(1 if pickup else 0) if len(loaded)==1 else rank
                rank+=1
                if pickup:
                    loaded[car]=material
                    if loc:occupants[loc].remove(material)
                else:
                    loaded[car]=None;occupants[loc].add(material)
                ev.pop(k);progressed=True;break
            if not progressed:raise ValueError('Instantaneous handover deadlock')
    # Same-route identity canonicalization is the original model's symmetry quotient.
    ops=flatten(sc);by=defaultdict(list);groups=defaultdict(list)
    for q in ops:by[q['order']].append(q['index'])
    for o in sc['orders']:groups[(o['route'],o.get('release_minutes',0),o['due_minutes'])].append(o['id'])
    mapping={}
    for names in groups.values():
        for old,new in zip(names if sc.get('production_state') else sorted(names,key=lambda x:s['forge_start'][by[x][0]]),names):mapping[old]=new
    result={k:[None]*len(ops) for k in ['heat_start','forge_start','furnace','heat_end','forge_end']};im={}
    for old,new in mapping.items():
        for i,j in zip(by[old],by[new]):
            im[i]=j
            for k in result:result[k][j]=s[k][i]
    for t in tasks:t['order']=mapping[t['order']];t['op']=im[t['op']]
    tasks.sort(key=lambda x:(x['op'],order[x['kind']]))
    for i,t in enumerate(tasks):t['id']=i
    result['transport']=tasks
    return result


def construct(sc, waves, seed=0, policy=0, engine='legacy', admission='batch',
              vehicle_policy='same', backtrack_budget=0, wip_limit=10,
              program_source=None, seconds=10, incumbent=None, preference='none',
              algorithm_source=None, checkpoint_path=None):
    if engine=='calendar':
        from algorithm import construct_algorithm
        return construct_algorithm(sc,waves,algorithm_source,seconds,checkpoint_path,
                                   incumbent=incumbent,preference=preference)
    if algorithm_source:raise ValueError('algorithm_source requires calendar engine')
    started=time.monotonic()
    observed=state_data(sc)
    if observed and engine=='legacy':
        raise ValueError('Observed-state continuation requires engine=extended; legacy remains a cold-start control')
    if engine=='legacy':
        captured={}
        result,err=legacy(sc,waves,seed=seed,policy=policy,capture=captured)
        return result,err,dict(engine=engine,nodes=0,backtracks=0,pruned=0,**captured)
    ops=flatten(sc); n=len(ops); tau=ticks(sc['transport']['travel_minutes'],sc['ticks_per_minute'])
    count=sc['furnace_count']; press=count+1; finished=count+2; L,D=window(sc)
    by=defaultdict(list)
    for q in ops:by[q['order']].append(q['index'])
    flat=[o for w in waves for o in w]
    if sorted(flat)!=sorted(by):raise ValueError('Construction must cover each order exactly once')
    groups={o:k for k,w in enumerate(waves) for o in w}
    rng=random.Random(seed); prog=Program(program_source) if program_source else None
    initial=dict(s={k:[None]*n for k in ['heat_start','forge_start','furnace','heat_end','forge_end']},
      cars=[[0,0] for _ in range(sc['transport']['vehicle_count'])],
      furn=[dict(temp=sc['initial_temperature'],exit=0,res=set()) for _ in range(count)],
      active=set(),pending=flat.copy(),frontier=0,tasks=[],done_orders=set(),finishing=[],choices=[])
    if observed:
        initial['s']=copy.deepcopy(observed['known']);initial['tasks']=copy.deepcopy(observed['tasks']);initial['frontier']=observed['now']
        initial['cars']=[[r['available_at_ticks'],r['location']] for r in observed['raw']['vehicles']]
        initial['furn']=[dict(temp=temperature(r),exit=observed['now'],res=set()) for r in observed['raw']['furnaces']]
        s=initial['s'];past_return={t['op'] for t in initial['tasks'] if t['kind'].startswith('press_to_')}
        for o,chain in by.items():
            if any(s['heat_start'][i] is not None for i in chain):initial['pending'].remove(o)
            for i in chain:
                if s['heat_start'][i] is not None and s['forge_start'][i] is None:
                    f=s['furnace'][i];initial['active'].add(i);initial['furn'][f]['res'].add(i);initial['furn'][f]['temp']=ops[i]['temp']
                if s['forge_start'][i] is not None and i not in past_return:initial['finishing'].append(i)
            if chain[-1] in past_return:initial['done_orders'].add(o)
        initial['finishing'].sort(key=lambda i:s['forge_end'][i])
    stats=dict(engine=engine,nodes=0,backtracks=0,pruned=0,program_hash=prog.hash if prog else None)
    best_partial=None; best_count=-1; failure_context=None

    def ready(st,f,temp,res=None):
        row=st['furn'][f];res=row['res'] if res is None else res
        if res:return 0 if row['temp']==temp and len(res)<sc['capacity'] else None
        return row['exit']+ramp_from(sc,row['temp'],temp)

    def observed_ready(f,temp):
        return observed['now']+ramp_from(sc,temperature(observed['raw']['furnaces'][f]),temp) if observed else 0

    def candidates(st, diagnostic=None):
        def reject(code, **detail):
            if diagnostic is not None:
                diagnostic['rejections'][code]=diagnostic['rejections'].get(code,0)+1
                if len(diagnostic['examples'])<16:
                    diagnostic['examples'].append(dict(reason=code,**detail))
        s=st['s'];actions=[]
        # Complete already-started forging / delivery to press without moving it.
        # The outgoing car and receiver are new decisions, not imported answers.
        if st['finishing']:
            i=st['finishing'][0];q=ops[i];end=s['forge_end'][i];chain=by[q['order']];ix=chain.index(i);j=chain[ix+1] if ix+1<len(chain) else None
            for pickup,(at,pos) in enumerate(st['cars']):
                if at+(tau if pos!=press else 0)>end:
                    reject('pickup_car_late',op=i,car=pickup,arrival_ticks=at+(tau if pos!=press else 0),required_ticks=end);continue
                for dest in (range(count) if j is not None else [-1]):
                    if dest>=0:
                        r=ready(st,dest,ops[j]['temp'])
                        if r is None or max(r,observed_ready(dest,ops[j]['temp']))>end+tau:
                            reject('receiver_unavailable_at_return',op=i,furnace=dest,return_ticks=end+tau,ready_ticks=r);continue
                    actions.append(dict(kind='finish',op=i,next=j,order=q['order'],furnace=s['furnace'][i],car=pickup,pickup_car=pickup,destination=dest,start=s['forge_start'][i],duration=q['duration'],slack=0,empty_receiver=int(dest>=0 and not st['furn'][dest]['res']),fire=q['fire'],temperature=q['temp'],min_hold=q['lower'],route=q['route']))
            return actions
        active_orders={ops[i]['order'] for i in st['active']}
        can_load=st['pending'] and len(active_orders)<wip_limit
        if st['pending'] and not can_load:reject('construction_wip_limit',limit=wip_limit,active_orders=len(active_orders))
        if can_load and admission=='batch' and active_orders:
            can_load=all(groups[o]==groups[st['pending'][0]] for o in active_orders)
            if not can_load:reject('construction_batch_gate',next_order=st['pending'][0])
        if can_load:
            order=st['pending'][0];i=by[order][0];q=ops[i]
            for car,(at,pos) in enumerate(st['cars']):
                for f in range(count):
                    r=ready(st,f,q['temp'])
                    if r is None:
                        reject('admission_furnace_incompatible_or_full',op=i,furnace=f,required_temperature=q['temp']);continue
                    a=max(st['frontier']+tau,at+(0 if pos==0 else tau)+tau,q['release']+tau,r,observed_ready(f,q['temp']))
                    actions.append(dict(kind='admit',op=i,order=order,furnace=f,car=car,pickup_car=car,destination=f,start=a,duration=0,slack=q['upper']-q['lower'],empty_receiver=int(not st['furn'][f]['res']),fire=q['fire'],temperature=q['temp'],min_hold=q['lower'],route=q['route']))
        for i in sorted(st['active']):
            q=ops[i];f=s['furnace'][i];a=s['heat_start'][i];chain=by[q['order']];ix=chain.index(i);j=chain[ix+1] if ix+1<len(chain) else None
            for car,(at,pos) in enumerate(st['cars']):
                first=max(st['frontier'],a+q['lower']+tau,at+(0 if pos==f+1 else tau)+tau)
                for pickup in ([car] if vehicle_policy=='same' else range(len(st['cars']))):
                    p_at,p_pos=st['cars'][pickup]
                    st0=max(first,p_at+(0 if p_pos==press else tau)-q['duration']) if pickup!=car else first
                    for dest in (range(count) if j is not None else [-1]):
                        cur=st0
                        if dest>=0:
                            cur=max(cur,observed_ready(dest,ops[j]['temp'])-q['duration']-tau)
                            res=st['furn'][dest]['res']-{i}
                            if res:
                                if st['furn'][dest]['temp']!=ops[j]['temp'] or len(res)>=sc['capacity']:
                                    reject('receiver_temperature_or_capacity',op=i,furnace=dest,next_temperature=ops[j]['temp'],reserved=len(res));continue
                            elif dest==f:
                                if ramp_ticks(sc,q['temp'],ops[j]['temp'])>q['duration']+2*tau:
                                    reject('same_furnace_ramp_too_long',op=i,ramp_ticks=ramp_ticks(sc,q['temp'],ops[j]['temp']),available_ticks=q['duration']+2*tau);continue
                            else:
                                r=ready(st,dest,ops[j]['temp'],res)
                                if r is None:
                                    reject('receiver_unavailable',op=i,furnace=dest);continue
                                cur=max(cur,r-q['duration']-tau)
                        if cur-tau>a+q['upper']:
                            reject('hold_upper_exceeded',op=i,order=q['order'],earliest_pickup_ticks=cur-tau,latest_pickup_ticks=a+q['upper'],car=car,destination=dest);continue
                        end=cur+q['duration']
                        if end+(tau if j is None else 0)>min(q['due'],ticks(sc['horizon_minutes'],sc['ticks_per_minute'])):
                            reject('deadline_exceeded',op=i,completion_ticks=end+(tau if j is None else 0));continue
                        blocked=[x for x in sorted(st['active']) if x!=i and end-tau>s['heat_start'][x]+ops[x]['upper']]
                        if blocked:
                            reject('other_material_hold_upper',op=i,blocked_operations=blocked);continue
                        actions.append(dict(kind='forge',op=i,next=j,order=q['order'],furnace=f,car=car,pickup_car=pickup,destination=dest,start=cur,duration=q['duration'],slack=a+q['upper']+tau-cur,empty_receiver=int(dest>=0 and not(st['furn'][dest]['res']-{i})),fire=q['fire'],temperature=q['temp'],min_hold=q['lower'],route=q['route']))
        return actions

    def apply(st,x):
        st=copy.deepcopy(st);st['choices']=(st['choices']+[dict(x)])[-12:];s=st['s'];i=x['op'];q=ops[i];f=x['furnace'];car=x['car'];a=x['start']
        def move(kind,src,dst,start,v):
            st['tasks'].append(dict(kind=kind,order=q['order'],op=i,source=src,destination=dst,start=start,end=start+tau,vehicle=v,pickup_rank=0,dropoff_rank=0))
        if x['kind']=='admit':
            move('cold_to_furnace',0,f+1,a-tau,car);st['cars'][car]=[a,f+1]
            s['heat_start'][i]=a;s['furnace'][i]=f;st['furn'][f]['res'].add(i);st['furn'][f]['temp']=q['temp'];st['active'].add(i);st['pending'].pop(0)
        else:
            end=a+q['duration'];dest=x['destination'];j=x['next'];pickup=x['pickup_car']
            if x['kind']=='finish':st['finishing'].pop(0)
            else:
                s['forge_start'][i]=a;s['forge_end'][i]=end;s['heat_end'][i]=a-tau
                move('furnace_to_press',f+1,press,a-tau,car)
                st['cars'][car]=[a,press]
                st['furn'][f]['res'].remove(i);st['furn'][f]['exit']=max(st['furn'][f]['exit'],a-tau);st['active'].remove(i)
            move('press_to_furnace' if j is not None else 'press_to_finished',press,dest+1 if j is not None else finished,end,pickup)
            st['cars'][pickup]=[end+tau,dest+1 if j is not None else finished]
            if j is not None:
                s['heat_start'][j]=end+tau;s['furnace'][j]=dest;st['furn'][dest]['res'].add(j);st['furn'][dest]['temp']=ops[j]['temp'];st['active'].add(j)
            else:st['done_orders'].add(q['order'])
            st['frontier']=max(st['frontier'],end)
        return st

    def visit(st):
        nonlocal best_partial,best_count,failure_context
        s=st['s']
        stats['nodes']+=1
        if time.monotonic()-started>seconds:return None,'construction_budget'
        done=sum(x is not None for x in st['s']['forge_start'])
        if done>best_count:best_count=done;best_partial=copy.deepcopy(st['s'])
        if not st['active'] and not st['pending'] and not st['finishing']:
            s=st['s'];s['transport']=st['tasks']
            try:return finalize(sc,s),None
            except ValueError as e:return None,str(e)
        busy=sum(max(0,min(st['s']['forge_end'][q['index']],D)-max(L,st['s']['forge_start'][q['index']])) for q in ops if st['s']['forge_start'][q['index']] is not None)
        remaining=sum(q['duration'] for q in ops if st['s']['forge_start'][q['index']] is None)
        upper=busy+min(max(0,D-max(L,st['frontier'])),remaining)
        if incumbent and (upper<incumbent['score_ticks'] or (preference=='none' and upper==incumbent['score_ticks'])):
            stats['pruned']+=1;return None,'day1_bound'
        diagnostic=dict(rejections={},examples=[])
        opts=candidates(st,diagnostic)
        if not opts:
            if failure_context is None or done>=failure_context['completed_operations']:
                failure_context=dict(
                    interpretation='Projected construction reservations, not a measured physical snapshot. Rejection counts describe attempted choices, not a proof of global infeasibility.',
                    ticks_per_minute=sc['ticks_per_minute'],frontier_ticks=st['frontier'],completed_operations=done,
                    active=[dict(op=i,order=ops[i]['order'],fire=ops[i]['fire'],furnace=s['furnace'][i],temperature=ops[i]['temp'],heat_start_ticks=s['heat_start'][i],earliest_pickup_ticks=s['heat_start'][i]+ops[i]['lower'],latest_pickup_ticks=s['heat_start'][i]+ops[i]['upper']) for i in sorted(st['active'])],
                    vehicles=[dict(vehicle=i,available_at_ticks=v[0],location=v[1]) for i,v in enumerate(st['cars'])],
                    furnaces=[dict(furnace=i,temperature=f['temp'],reserved_ops=sorted(f['res']),last_exit_ticks=f['exit']) for i,f in enumerate(st['furn'])],
                    pending_orders=st['pending'],recent_choices=st['choices'],**diagnostic)
            return None,'no_legal_constructive_action' 
        state=dict(now=st['frontier'],day_end=D,active_count=len(st['active']),pending_count=len(st['pending']),completed_operations=done,day1_busy=busy,vehicle_count=len(st['cars']),capacity=sc['capacity'],slots_per_furnace=sc['capacity'],furnace_count=count,total_slots=count*sc['capacity'],route_counts={r:sum(ops[i]['route']==r for i in st['active']) for r in {q['route'] for q in ops}})
        if prog:scores=prog.scores(opts,state)
        else:
            scores=[]
            for x in opts:
                if x['kind']=='admit':score=x['start']+x['min_hold']*.15
                else:
                    a=x['start'];sl=x['slack'];idle=a-st['frontier'];j=x['fire']-1
                    score=[a+.15*sl,a+.5*sl,a+sl+.1*idle,a+.05*sl-.5*x['duration'],a+.25*sl-.5*j,a+.3*sl+rng.uniform(-60,60)][policy]+2*x['empty_receiver']
                scores.append(score)
        ranked=sorted(range(len(opts)),key=lambda i:(scores[i],opts[i]['start'],i))
        for ix,index in enumerate(ranked[:4]):
            if ix:
                if stats['backtracks']>=backtrack_budget:break
                stats['backtracks']+=1
            result,err=visit(apply(st,opts[index]))
            if result:return result,None
            if err=='construction_budget':return None,err
        return None,'bounded_deadend'
    result,err=visit(initial)
    stats.update(completed_operations=best_count,partial=best_partial,failure_context=failure_context,backtrack_budget=backtrack_budget)
    return result,err,stats
