"""Observed execution prefixes, not saved future schedules.

All original physical constraints remain in reference/.  This adapter only fixes
observed actions, prevents backdating and anchors equipment at the observation.
The cut is BEFORE events at now; loaded trips already started retain their end.
"""
import copy
import math
from collections import defaultdict
from fractions import Fraction
from longda_cpsat.model import flatten, ramp_ticks, ticks

FIELDS = ('heat_start', 'heat_end', 'forge_start', 'forge_end', 'furnace')


def key(q):
    return (q['order'], q['fire'])


def temperature(row):
    return Fraction(str(row['temperature']))


def ramp_from(sc, source, target):
    source, target = Fraction(str(source)), Fraction(str(target))
    rate = sc['ramp_up_deg_per_min'] if target >= source else sc['ramp_down_deg_per_min']
    return math.ceil(abs(target-source)*sc['ticks_per_minute']/Fraction(str(rate)))


def state_data(sc):
    st = sc.get('production_state')
    if st is None:
        return None
    if st.get('schema_version') != 1 or st.get('cut') != 'before_events':
        raise ValueError('State requires schema_version=1 and cut=before_events')
    now = st.get('now_ticks')
    horizon = ticks(sc['horizon_minutes'], sc['ticks_per_minute'])
    if type(now) is not int or not 0 <= now < horizon:
        raise ValueError('State now_ticks outside horizon')
    ops = flatten(sc); ids = {key(q): q['index'] for q in ops}
    if len(ids) != len(ops): raise ValueError('Nonunique order/fire identity')
    known = {k: [None]*len(ops) for k in FIELDS}
    for row in st['operations']:
        ident = (row['order'], row['fire'])
        if ident not in ids: raise ValueError('State references unknown order/fire')
        i = ids[ident]
        if any(known[k][i] is not None for k in FIELDS):
            raise ValueError('Duplicate state operation')
        if set(row)-{'order','fire',*FIELDS}: raise ValueError('Unknown observed operation field')
        for k in FIELDS:
            if k in row:
                x = row[k]
                if type(x) is not int or x < 0: raise ValueError('Invalid observed '+k)
                known[k][i] = x
    tasks = []
    seen = set()
    tau = ticks(sc['transport']['travel_minutes'], sc['ticks_per_minute'])
    for raw in st['transports']:
        t = copy.deepcopy(raw); ident = (t['order'], t.pop('fire'))
        if ident not in ids: raise ValueError('Unknown transport identity')
        t['op'] = ids[ident]; tk = (t['op'], t['kind'])
        if tk in seen: raise ValueError('Duplicate observed transport')
        seen.add(tk)
        if not 0 <= t['start'] < now or t['end'] != t['start']+tau:
            raise ValueError('Only already-started loaded trips belong in observations')
        if not 0 <= t['vehicle'] < sc['transport']['vehicle_count']:
            raise ValueError('Unknown observed vehicle')
        tasks.append(t)
    # Derive commitments solely from started trips and started forging.
    derived = {k: [None]*len(ops) for k in FIELDS}
    chains = defaultdict(list)
    for q in ops: chains[q['order']].append(q['index'])
    def setv(k, i, x):
        if derived[k][i] not in (None,x): raise ValueError('Contradictory observation')
        derived[k][i] = x
    for t in tasks:
        i=t['op']; q=ops[i]; kind=t['kind']
        if kind == 'cold_to_furnace':
            if i != chains[q['order']][0] or t['source'] != 0: raise ValueError('Invalid cold trip')
            setv('heat_start',i,t['end']);setv('furnace',i,t['destination']-1)
        elif kind == 'furnace_to_press':
            if t['destination'] != sc['furnace_count']+1: raise ValueError('Invalid press delivery')
            setv('heat_end',i,t['start']);setv('forge_start',i,t['end']);setv('forge_end',i,t['end']+q['duration']);setv('furnace',i,t['source']-1)
        elif kind in ('press_to_furnace','press_to_finished'):
            if t['source'] != sc['furnace_count']+1: raise ValueError('Invalid press pickup')
            setv('forge_end',i,t['start']);setv('forge_start',i,t['start']-q['duration'])
            chain=chains[q['order']]; ix=chain.index(i)
            if kind=='press_to_furnace':
                if ix+1==len(chain): raise ValueError('Last fire cannot return')
                j=chain[ix+1];setv('heat_start',j,t['end']);setv('furnace',j,t['destination']-1)
            elif ix+1!=len(chain) or t['destination']!=sc['furnace_count']+2:
                raise ValueError('Invalid finished delivery')
        else: raise ValueError('Unknown trip kind')
    if known != derived:
        raise ValueError('Operations must equal commitments derived from observed transport history; future answers are forbidden')
    for q in ops:
        i=q['index'];a=known['heat_start'][i];x=known['heat_end'][i];e=known['forge_end'][i]
        if a is not None and not 0<=known['furnace'][i]<sc['furnace_count']: raise ValueError('Unknown furnace')
        if x is not None and (a is None or not q['lower']<=x-a<=q['upper']): raise ValueError('Observed holding violates process window')
        if a is not None and x is None and now-a>q['upper']: raise ValueError('Observed holding already exceeds max')
        if e is not None and e<now and not any(t['op']==i and t['kind'].startswith('press_to_') for t in tasks):
            raise ValueError('Finished forging cannot wait for pickup')
    if len(st['furnaces'])!=sc['furnace_count'] or [r['id'] for r in st['furnaces']]!=list(range(sc['furnace_count'])):
        raise ValueError('Must observe every furnace, indexed from zero')
    if len(st['vehicles'])!=sc['transport']['vehicle_count'] or [r['id'] for r in st['vehicles']]!=list(range(sc['transport']['vehicle_count'])):
        raise ValueError('Must observe every vehicle')
    for f,row in enumerate(st['furnaces']):
        temp=temperature(row)
        if temp<=0: raise ValueError('Invalid furnace temperature')
        active=[q for q in ops if known['furnace'][q['index']]==f and known['heat_start'][q['index']] is not None and known['heat_start'][q['index']]<now and (known['heat_end'][q['index']] is None or known['heat_end'][q['index']]>=now)]
        if len(active)>sc['capacity'] or any(q['temp']!=temp for q in active): raise ValueError('Furnace observation conflicts with occupants')
        past=[q for q in ops if known['furnace'][q['index']]==f and known['heat_end'][q['index']] is not None and known['heat_end'][q['index']]<now]
        last=max(past,key=lambda q:known['heat_end'][q['index']],default=None)
        from_temp=last['temp'] if last else sc['initial_temperature'];at=known['heat_end'][last['index']] if last else 0
        if not active and at+ramp_from(sc,from_temp,temp)>now: raise ValueError('Observed furnace temperature unreachable')
    for car,row in enumerate(st['vehicles']):
        trips=sorted([t for t in tasks if t['vehicle']==car],key=lambda x:x['start'])
        at,pos=0,0
        for t in trips:
            if t['start']<at+(tau if pos!=t['source'] else 0): raise ValueError('Observed vehicle chain impossible')
            at,pos=t['end'],t['destination']
        if type(row['location']) is not int or not 0<=row['location']<=sc['furnace_count']+2: raise ValueError('Unknown vehicle location')
        # Report loaded cars by their fixed arrival location and availability.
        if at>now:
            if row['available_at_ticks']!=at or row['location']!=pos: raise ValueError('In-flight car observation mismatch')
        else:
            empty=row.get('empty_move')
            if empty:
                if not (empty['source']==pos and empty['destination']==row['location'] and empty['start']>=at and empty['start']<now<empty['end'] and empty['end']==empty['start']+tau and row['available_at_ticks']==empty['end']):
                    raise ValueError('Observed empty movement inconsistent')
            elif row['available_at_ticks']!=now or at+(tau if pos!=row['location'] else 0)>now: raise ValueError('Idle car location unreachable')
    return dict(raw=st,now=now,known=known,tasks=tasks,ops=ops)


def add_state_constraints(sc, model, v):
    data=state_data(sc)
    if not data: return
    now=data['now'];known=data['known'];ops=data['ops'];st=data['raw']
    for k in FIELDS:
        for i,value in enumerate(known[k]):
            if value is not None: model.add(v[k][i]==value)
    observed={(t['op'],t['kind']):t for t in data['tasks']}
    tau=ticks(sc['transport']['travel_minutes'],sc['ticks_per_minute'])
    for t in v['_transport']:
        old=observed.get((t['op'],t['kind']))
        if old:
            for k in ('start','end','source','destination','vehicle'):
                model.add(t[k]==old[k])
        else:
            model.add(t['start']>=now)
            for car,row in enumerate(st['vehicles']):
                uses=model.new_bool_var(f'observed_car_{t["id"]}_{car}')
                model.add(t['vehicle']==car).only_enforce_if(uses)
                model.add(t['vehicle']!=car).only_enforce_if(uses.Not())
                same=model.new_bool_var(f'observed_loc_{t["id"]}_{car}')
                model.add(t['source']==row['location']).only_enforce_if(same)
                model.add(t['source']!=row['location']).only_enforce_if(same.Not())
                model.add(t['start']>=row['available_at_ticks']+tau*(1-same)).only_enforce_if(uses)
    for q in ops:
        i=q['index']
        if known['heat_start'][i] is None or known['heat_start'][i]>=now:
            for f,row in enumerate(st['furnaces']):
                inside=model.new_bool_var(f'observed_heat_{i}_{f}')
                model.add(v['furnace'][i]==f).only_enforce_if(inside)
                model.add(v['furnace'][i]!=f).only_enforce_if(inside.Not())
                model.add(v['heat_start'][i]>=now+ramp_from(sc,temperature(row),q['temp'])).only_enforce_if(inside)


def audit_state(sc,s):
    """Independent comparison to observations; no CP calls and no relaxed rules."""
    data=state_data(sc)
    if not data:return dict(valid=True,observed=False,errors=[])
    errors=[];now=data['now'];st=data['raw'];tau=ticks(sc['transport']['travel_minutes'],sc['ticks_per_minute'])
    for k in FIELDS:
        for i,x in enumerate(data['known'][k]):
            if x is not None and s[k][i]!=x:errors.append(f'observed {k}[{i}] changed')
    old={(t['op'],t['kind']):t for t in data['tasks']}
    actual={(t['op'],t['kind']):t for t in s['transport']}
    for ident,t in old.items():
        if ident not in actual or any(t[k]!=actual[ident][k] for k in ('start','end','source','destination','vehicle')):errors.append(f'observed trip changed {ident}')
    for ident,t in actual.items():
        if ident in old:continue
        car=st['vehicles'][t['vehicle']]
        if t['start']<now:errors.append('unobserved action backdated')
        if t['start']<car['available_at_ticks']+(tau if t['source']!=car['location'] else 0):errors.append('vehicle observation violated')
    for q in data['ops']:
        i=q['index'];a=s['heat_start'][i]
        if data['known']['heat_start'][i] is None or data['known']['heat_start'][i]>=now:
            row=st['furnaces'][s['furnace'][i]]
            if a<now+ramp_from(sc,temperature(row),q['temp']):errors.append('furnace observation violated')
    return dict(valid=not errors,observed=True,now_ticks=now,errors=errors)


def features(sc):
    data=state_data(sc);ops=flatten(sc);scale=sc['ticks_per_minute'];now=data['now'] if data else 0
    known=data['known'] if data else {k:[None]*len(ops) for k in FIELDS}
    counts=defaultdict(int);remaining=defaultdict(int);hold=defaultdict(int);route_work=defaultdict(int);started=set();completed=set();slacks=[];mins=[]
    chains=defaultdict(list)
    for q in ops:
        i=q['index'];chains[q['order']].append(i)
        if known['heat_start'][i] is not None:started.add(q['order'])
        if known['forge_end'][i] is None or known['forge_end'][i]>now:
            remaining[q['route']]+=1
            end=known['forge_end'][i];start=known['forge_start'][i]
            route_work[q['route']]+=(max(0,end-max(now,start)) if end is not None else q['duration'])/scale
        if known['heat_start'][i] is not None and known['heat_start'][i]<now and (known['heat_end'][i] is None or known['heat_end'][i]>=now):
            elapsed=now-known['heat_start'][i];mins.append(max(0,q['lower']-elapsed)/scale);slacks.append((q['upper']-elapsed)/scale);hold[str(q['temp'])]+=1
    for o in sc['orders']:
        counts[o['route']]+=1
        i=chains[o['id']][-1]
        if data and any(t['op']==i and t['kind']=='press_to_finished' and t['end']<=now for t in data['tasks']):completed.add(o['id'])
    due=[o['due_minutes']-now/scale for o in sc['orders'] if o['id'] not in completed]
    rows=data['raw']['furnaces'] if data else [dict(id=f,temperature=sc['initial_temperature']) for f in range(sc['furnace_count'])]
    temp_counts=defaultdict(int)
    for f in rows:temp_counts[str(temperature(f))]+=1
    return dict(now_minutes=now/scale,production_day=now//(1440*scale)+1,day_remaining_minutes=1440-(now/scale)%1440,
      horizon_remaining_minutes=sc['horizon_minutes']-now/scale,order_counts=dict(counts),remaining_operations=dict(remaining),remaining_forge_minutes=dict(route_work),
      cold_orders=len(sc['orders'])-len(started),active_orders=len(started-completed),completed_orders=len(completed),heating_by_temperature=dict(hold),
      min_remaining_hold_minutes=min(mins,default=0),min_upper_slack_minutes=min(slacks,default=1440),urgent_heating=sum(x<30 for x in slacks),
      due_slack_min_minutes=min(due,default=0),furnace_temperature_counts=dict(temp_counts),free_slots=sc['capacity']*sc['furnace_count']-sum(hold.values()),
      press_busy=any(known['forge_start'][q['index']] is not None and known['forge_start'][q['index']]<now<known['forge_end'][q['index']] for q in ops),
      vehicles=[dict(location=r['location'],busy_minutes=max(0,r['available_at_ticks']-now)/scale) for r in data['raw']['vehicles']] if data else [dict(location=0,busy_minutes=0) for _ in range(sc['transport']['vehicle_count'])])


def snapshot(sc,s,now):
    """Test/import helper: discard ALL unstarted actions from a full audited trace."""
    from physical_model import audit
    if not audit(sc,s)['valid']:raise ValueError('Snapshot source must pass original physical audit')
    ops=flatten(sc);tau=ticks(sc['transport']['travel_minutes'],sc['ticks_per_minute'])
    tasks=[{k:v for k,v in t.items() if k in ('kind','order','source','destination','start','end','vehicle')}|{'fire':ops[t['op']]['fire']} for t in s['transport'] if t['start']<now]
    included={(t['order'],t['fire'],t['kind']) for t in tasks};chains=defaultdict(list)
    for q in ops:chains[q['order']].append(q['index'])
    observations=[]
    for q in ops:
        i=q['index'];row={'order':q['order'],'fire':q['fire']};chain=chains[q['order']];ix=chain.index(i)
        incoming=(q['order'],q['fire'],'cold_to_furnace') if ix==0 else (q['order'],ops[chain[ix-1]]['fire'],'press_to_furnace')
        if incoming in included:row.update(heat_start=s['heat_start'][i],furnace=s['furnace'][i])
        if (q['order'],q['fire'],'furnace_to_press') in included:row.update(heat_end=s['heat_end'][i],forge_start=s['forge_start'][i],forge_end=s['forge_end'][i],furnace=s['furnace'][i])
        if len(row)>2:observations.append(row)
    furn=[]
    for f in range(sc['furnace_count']):
        temp=Fraction(sc['initial_temperature'])
        for q in sorted([q for q in ops if s['furnace'][q['index']]==f],key=lambda q:s['heat_start'][q['index']]):
            a=s['heat_start'][q['index']];target=Fraction(q['temp'])
            if target!=temp:
                ramp=ramp_from(sc,temp,target);start=a-ramp
                if start<now<a:
                    temp=temp+(target-temp)*Fraction(now-start,ramp);break
                if a<=now:temp=target
            if a>=now:break
        furn.append(dict(id=f,temperature=str(temp)))
    cars=[]
    for car in range(sc['transport']['vehicle_count']):
        done=sorted([t for t in tasks if t['vehicle']==car],key=lambda t:t['start'])
        last=done[-1] if done else None
        pos=last['destination'] if last else 0;at=last['end'] if last else 0
        row=dict(id=car,location=pos,available_at_ticks=max(now,at))
        # Derive a possible just-in-time EMPTY trajectory, then export only the
        # already-started movement. No future loaded-task choice is retained.
        future=sorted([t for t in s['transport'] if t['vehicle']==car and t['start']>=now],key=lambda t:t['start'])
        if at<=now and future and future[0]['source']!=pos:
            end=future[0]['start'];start=end-tau
            if start<now<end:
                row.update(location=future[0]['source'],available_at_ticks=end,empty_move=dict(source=pos,destination=future[0]['source'],start=start,end=end))
            elif end==now and start>=at:
                row.update(location=future[0]['source'])
        cars.append(row)
    result=dict(schema_version=1,cut='before_events',now_ticks=now,operations=observations,transports=tasks,furnaces=furn,vehicles=cars)
    check=copy.deepcopy(sc);check['production_state']=result;state_data(check)
    return result
