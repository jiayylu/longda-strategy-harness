"""CP-SAT enumeration model. No objective; all auxiliaries functionally determined."""
from fractions import Fraction
from collections import defaultdict
from ortools.sat.python import cp_model


def ticks(value, scale):
    value=Fraction(str(value))*scale
    if value.denominator != 1:
        raise ValueError('Input time is not exactly representable on the tick lattice')
    return int(value)


def flatten(scenario):
    routes = {r['id']: r for r in scenario['routes']}
    scale = scenario['ticks_per_minute']
    ops = []
    for order in scenario['orders']:
        for stage in routes[order['route']]['operations']:
            ops.append(dict(stage, index=len(ops), order=order['id'], route=order['route'],
                            release=ticks(order.get('release_minutes', 0),scale),
                            due=ticks(order.get('due_minutes', scenario['horizon_minutes']),scale),
                            lower=ticks(stage['hold_min'],scale), upper=ticks(stage['hold_max'],scale),
                            duration=ticks(stage['forge'],scale)))
    return ops


def ramp_ticks(scenario, source, target):
    rate = scenario['ramp_up_deg_per_min'] if target >= source else scenario['ramp_down_deg_per_min']
    value = Fraction(str(abs(target-source))) * scenario['ticks_per_minute'] / Fraction(str(rate))
    if value.denominator != 1:
        raise ValueError('Temperature ramp is not exactly representable by the internal tick unit')
    return int(value)


def build_model(scenario, shard=None):
    from .transport import transport_enabled, build_transport_model
    if transport_enabled(scenario):
        return build_transport_model(scenario, shard)
    ops = flatten(scenario)
    scale = scenario['ticks_per_minute']
    horizon = int(scenario['horizon_minutes']*scale)
    count = scenario['furnace_count']
    m = cp_model.CpModel()
    v = {k: [] for k in ('heat_start','forge_start','furnace','heat_duration','forge_end')}
    by_order, by_type = defaultdict(list), defaultdict(list)
    for q in ops:
        by_order[q['order']].append(q['index'])
    for order in scenario['orders']:
        key = (order['route'], order.get('release_minutes',0), order.get('due_minutes',scenario['horizon_minutes']))
        by_type[key].append(by_order[order['id']][0])
    ea, es, ls = {}, {}, {}
    for ids in by_order.values():
        previous_end = 0
        for j, i in enumerate(ids):
            q = ops[i]
            ea[i] = max(q['release'] if j == 0 else previous_end,
                        ramp_ticks(scenario,scenario['initial_temperature'],q['temp']))
            es[i] = ea[i]+q['lower']
            previous_end = es[i]+q['duration']
        latest = min(ops[ids[-1]]['due'],horizon)
        for i in reversed(ids):
            q = ops[i]
            ls[i] = latest-q['duration']
            latest = ls[i]-q['lower']
    intervals = [[] for _ in range(count)]
    press = []
    for i,q in enumerate(ops):
        if es[i] > ls[i]:
            # Keep a valid model whose contradiction is explicit.
            m.add_bool_or([])
        latest_s = max(es[i],ls[i])
        a = m.new_int_var(ea[i], max(ea[i],latest_s-q['lower']), f'a{i}')
        s = m.new_int_var(es[i], latest_s, f's{i}')
        h = m.new_int_var(q['lower'],q['upper'],f'h{i}')
        f = m.new_int_var(0,count-1,f'f{i}')
        e = m.new_int_var(es[i]+q['duration'],latest_s+q['duration'],f'e{i}')
        for key,value in zip(v,(a,s,f,h,e)):
            v[key].append(value)
        m.add(s == a+h)
        m.add(e == s+q['duration'])
        m.add(e <= min(q['due'],horizon))
        press.append(m.new_interval_var(s,q['duration'],e,f'press{i}'))
        for furnace in range(count):
            present=m.new_bool_var(f'in{i}_{furnace}')
            m.add(f == furnace).only_enforce_if(present)
            m.add(f != furnace).only_enforce_if(present.Not())
            intervals[furnace].append(m.new_optional_interval_var(a,h,s,present,f'heat{i}_{furnace}'))
    m.add_no_overlap(press)
    for row in intervals:
        m.add_cumulative(row,[1]*len(row),scenario['capacity'])
    for ids in by_order.values():
        for i,j in zip(ids,ids[1:]):
            m.add(v['heat_start'][j] == v['forge_end'][i])
    if scenario.get('quotient_identical_orders',True):
        for ids in by_type.values():
            for i,j in zip(ids,ids[1:]):
                m.add(v['forge_start'][i] < v['forge_start'][j])
    # All different-temperature pairs on a shared furnace need an empty ramp gap.
    # Directed ramp distances obey triangle inequality, so pairwise gaps are exact.
    for i,qi in enumerate(ops):
        for j in range(i+1,len(ops)):
            qj=ops[j]
            if qi['temp'] == qj['temp']:
                continue
            dij=ramp_ticks(scenario,qi['temp'],qj['temp'])
            dji=ramp_ticks(scenario,qj['temp'],qi['temp'])
            if ls[i]+dij <= ea[j] or ls[j]+dji <= ea[i]:
                continue
            same=m.new_bool_var(f'same{i}_{j}')
            m.add(v['furnace'][i] == v['furnace'][j]).only_enforce_if(same)
            m.add(v['furnace'][i] != v['furnace'][j]).only_enforce_if(same.Not())
            if qi['order'] == qj['order']:
                m.add(v['heat_start'][j] >= v['forge_start'][i]+dij).only_enforce_if(same)
            else:
                before=m.new_bool_var(f'before{i}_{j}')
                m.add(v['heat_start'][i] <= v['heat_start'][j]).only_enforce_if(before)
                m.add(v['heat_start'][i] > v['heat_start'][j]).only_enforce_if(before.Not())
                m.add(v['heat_start'][j] >= v['forge_start'][i]+dij).only_enforce_if([same,before])
                m.add(v['heat_start'][i] >= v['forge_start'][j]+dji).only_enforce_if([same,before.Not()])
    if shard:
        first=by_order[shard['first_order']][0]
        for ids in by_order.values():
            if ids[0] != first:
                m.add(v['forge_start'][first] < v['forge_start'][ids[0]])
        m.add(v['furnace'][first] == shard['first_furnace'])
        lo,hi=shard['first_start_ticks']
        m.add(v['forge_start'][first] >= lo)
        m.add(v['forge_start'][first] <= hi)
    error=m.validate()
    if error:
        raise ValueError(error)
    return m,v,ops


def extract_schedule(solver, variables):
    if '_transport' in variables:
        from .transport import extract_transport_schedule
        return extract_transport_schedule(solver, variables)
    return {k:[int(solver.value(x)) for x in variables[k]] for k in ('heat_start','forge_start','furnace')}


def make_shards(scenario):
    # For a cold start first forging must be the canonical first order of one type.
    candidates={}
    for order in scenario['orders']:
        key=(order['route'],order.get('release_minutes',0),order.get('due_minutes',scenario['horizon_minutes']))
        if scenario.get('quotient_identical_orders',True):
            candidates.setdefault(key,order['id'])
        else:
            candidates[order['id']]=order['id']
    horizon=int(scenario['horizon_minutes']*scenario['ticks_per_minute'])
    split=horizon//2
    shards=[]
    for order in candidates.values():
        for furnace in range(scenario['furnace_count']):
            for lo,hi in ((0,split-1),(split,horizon)):
                shards.append({'id':f'shard-{len(shards):03d}','first_order':order,
                               'first_furnace':furnace,'first_start_ticks':[lo,hi]})
    return shards
