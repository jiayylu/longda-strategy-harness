"""Day-prefix relaxation and exact fixing of that prefix in the full model.

All times are integer ticks. D is the right-open end of the first day [0,D).
Future starts use sentinel D; this model does NOT assign their actual times.
A prefix can fail to have a full continuation. Every feasible full schedule,
however, projects into this model (subject to its explicit shard/score cut).
"""
from collections import defaultdict
from ortools.sat.python import cp_model
from .model import flatten, ramp_ticks


def _transport_prefix(scenario, D, incumbent_ticks, shard):
    """Project thermal rules with exact finite-car paths for first-day pickups.

    Cold loading, empty repositioning, vehicle capacity and no-swap handovers
    apply before D, including the full tails of trips crossing D. Vehicle
    paths for pickups at/after D still require full continuation. Projection
    fixes heat/forge decisions, not a particular auxiliary car assignment;
    this is an offline feasibility screen, not an execution-state snapshot.
    """
    from .transport import build_transport_model
    m, full, ops = build_transport_model(scenario, shard, vehicle_prefix_end=D)
    count = scenario['furnace_count']
    v = {k: [] for k in ('heat_start', 'forge_start', 'furnace',
                         'heat_present', 'forge_present', 'day_contribution')}
    for i, q in enumerate(ops):
        a = m.new_int_var(0, D, f'projected_a{i}')
        s = m.new_int_var(0, D, f'projected_s{i}')
        f = m.new_int_var(-1, count-1, f'projected_f{i}')
        hp = m.new_bool_var(f'projected_heat_present{i}')
        sp = m.new_bool_var(f'projected_forge_present{i}')
        m.add_min_equality(a, [D, full['heat_start'][i]])
        m.add_min_equality(s, [D, full['forge_start'][i]])
        m.add(a < D).only_enforce_if(hp)
        m.add(a == D).only_enforce_if(hp.Not())
        m.add(s < D).only_enforce_if(sp)
        m.add(s == D).only_enforce_if(sp.Not())
        m.add(f == full['furnace'][i]).only_enforce_if(hp)
        m.add(f == -1).only_enforce_if(hp.Not())
        b = m.new_int_var(0, min(D, q['duration']), f'projected_work{i}')
        m.add_min_equality(b, [q['duration'], D-s])
        for key, value in zip(v, (a, s, f, hp, sp, b)):
            v[key].append(value)
    total = m.new_int_var(0, min(D, sum(q['duration'] for q in ops)), 'day_total')
    m.add(total == sum(v['day_contribution']))
    m.add(total > int(incumbent_ticks))
    v['day_total'] = total
    if shard:
        first = next(q['index'] for q in ops if q['order'] == shard['first_order'])
        m.add(v['forge_present'][first] == 1)
        m.add(total + v['forge_start'][first] <= D)
    error = m.validate()
    if error:
        raise ValueError(error)
    # Expose the physical witness for diagnostics/tests without changing the
    # canonical prefix tuple, its exact nogoods, or continuation semantics.
    v['_transport_model'] = full
    return m, v, ops


def _indices(scenario, ops):
    by_order, by_type = defaultdict(list), defaultdict(list)
    for q in ops:
        by_order[q['order']].append(q['index'])
    for order in scenario['orders']:
        key = (order['route'], order.get('release_minutes', 0),
               order.get('due_minutes', scenario['horizon_minutes']))
        by_type[key].append(by_order[order['id']][0])
    return by_order, by_type


def build_prefix_model(scenario, day_end_ticks, incumbent_ticks=0, shard=None):
    """Return (CpModel, variables, operations) for prefixes with Bday > incumbent.

    Use incumbent_ticks=-1 to include the empty/zero-score prefix in tests.
    Variables a,s are min(actual_start,D). Furnace is -1 exactly when a==D.
    A furnace interval is [a,s), hence still-heating materials occupy up to D.
    Forge durations crossing D remain complete intervals for press conflicts;
    only their overlap with [0,D) contributes to the score.
    """
    D = int(day_end_ticks)
    if D <= 0:
        raise ValueError('day_end_ticks must be positive')
    ops = flatten(scenario)
    horizon = int(scenario['horizon_minutes'] * scenario['ticks_per_minute'])
    if D > horizon:
        raise ValueError('The day boundary cannot exceed the full horizon')
    from .transport import transport_enabled
    if transport_enabled(scenario):
        return _transport_prefix(scenario, D, incumbent_ticks, shard)
    count = scenario['furnace_count']
    by_order, by_type = _indices(scenario, ops)
    earliest_a, earliest_s, latest_s = {}, {}, {}
    for ids in by_order.values():
        previous_end = 0
        for j, i in enumerate(ids):
            q = ops[i]
            earliest_a[i] = max(q['release'] if j == 0 else previous_end,
                ramp_ticks(scenario, scenario['initial_temperature'], q['temp']))
            earliest_s[i] = earliest_a[i] + q['lower']
            previous_end = earliest_s[i] + q['duration']
        latest = min(ops[ids[-1]]['due'], horizon)
        for i in reversed(ids):
            latest_s[i] = latest - ops[i]['duration']
            latest = latest_s[i] - ops[i]['lower']

    m = cp_model.CpModel()
    v = {k: [] for k in ('heat_start', 'forge_start', 'furnace',
                         'heat_present', 'forge_present', 'day_contribution')}
    furnace_intervals = [[] for _ in range(count)]
    press_intervals = []
    for i, q in enumerate(ops):
        if earliest_s[i] > latest_s[i]:
            m.add_bool_or([])
        a = m.new_int_var(min(D, earliest_a[i]), D, f'a{i}')
        s = m.new_int_var(min(D, earliest_s[i]), D, f's{i}')
        f = m.new_int_var(-1, count-1, f'f{i}')
        hp = m.new_bool_var(f'heat_present{i}')
        sp = m.new_bool_var(f'forge_present{i}')
        m.add(a < D).only_enforce_if(hp)
        m.add(a == D).only_enforce_if(hp.Not())
        m.add(s < D).only_enforce_if(sp)
        m.add(s == D).only_enforce_if(sp.Not())
        m.add(sp <= hp)
        m.add(f >= 0).only_enforce_if(hp)
        m.add(f == -1).only_enforce_if(hp.Not())
        m.add(s >= a)
        m.add(a <= latest_s[i] - q['lower']).only_enforce_if(hp)
        m.add(s <= latest_s[i]).only_enforce_if(sp)
        if latest_s[i] - q['lower'] < D:
            m.add(hp == 1)
        if latest_s[i] < D:
            m.add(sp == 1)
        h = m.new_int_var(0, D, f'elapsed_heat{i}')
        m.add(h == s-a)
        m.add(h <= q['upper'])
        m.add(h >= q['lower']).only_enforce_if(sp)
        e = m.new_int_var(q['duration'], D+q['duration'], f'forge_end{i}')
        m.add(e == s + q['duration'])
        press_intervals.append(m.new_optional_interval_var(s, q['duration'], e, sp, f'press{i}'))
        b = m.new_int_var(0, min(D, q['duration']), f'day_work{i}')
        m.add_min_equality(b, [q['duration'], D-s])
        # Redundant integer/linear links strengthen propagation and the LP.
        # A start in [0,D) contributes at least one representable tick.
        m.add(b <= q['duration']*sp)
        if q['duration'] > 0:
            m.add(b >= sp)
        for furnace in range(count):
            present = m.new_bool_var(f'in{i}_{furnace}')
            m.add(f == furnace).only_enforce_if(present)
            m.add(f != furnace).only_enforce_if(present.Not())
            furnace_intervals[furnace].append(m.new_optional_interval_var(a, h, s, present, f'heat{i}_{furnace}'))
        for key, value in zip(v, (a,s,f,hp,sp,b)):
            v[key].append(value)
    m.add_no_overlap(press_intervals)
    for row in furnace_intervals:
        m.add_cumulative(row, [1]*len(row), scenario['capacity'])
    for ids in by_order.values():
        for i,j in zip(ids, ids[1:]):
            # Immediate return if forging finishes before midnight; otherwise
            # the next heat has not begun in the prefix and uses sentinel D.
            m.add_min_equality(v['heat_start'][j], [D, v['forge_start'][i]+ops[i]['duration']])
            m.add(v['heat_present'][j] <= v['forge_present'][i])
    if scenario.get('quotient_identical_orders', True):
        for ids in by_type.values():
            for i,j in zip(ids,ids[1:]):
                m.add(v['forge_start'][i] <= v['forge_start'][j])
                m.add(v['forge_start'][i] < v['forge_start'][j]).only_enforce_if(v['forge_present'][j])
    for i,qi in enumerate(ops):
        for j in range(i+1,len(ops)):
            qj=ops[j]
            if qi['temp'] == qj['temp']:
                continue
            same=m.new_bool_var(f'same{i}_{j}')
            m.add(v['furnace'][i] == v['furnace'][j]).only_enforce_if(same)
            m.add(v['furnace'][i] != v['furnace'][j]).only_enforce_if(same.Not())
            both=[same,v['heat_present'][i],v['heat_present'][j]]
            dij=ramp_ticks(scenario,qi['temp'],qj['temp'])
            dji=ramp_ticks(scenario,qj['temp'],qi['temp'])
            if qi['order'] == qj['order']:
                m.add(v['heat_start'][j] >= v['forge_start'][i]+dij).only_enforce_if(both)
            else:
                before=m.new_bool_var(f'before{i}_{j}')
                m.add(v['heat_start'][i] <= v['heat_start'][j]).only_enforce_if(before)
                m.add(v['heat_start'][i] > v['heat_start'][j]).only_enforce_if(before.Not())
                m.add(v['heat_start'][j] >= v['forge_start'][i]+dij).only_enforce_if(both+[before])
                m.add(v['heat_start'][i] >= v['forge_start'][j]+dji).only_enforce_if(both+[before.Not()])
    upper_bound=max(0,D-min(earliest_s.values(),default=D))
    total=m.new_int_var(0,upper_bound,'day_total')
    m.add(total == sum(v['day_contribution']))
    m.add(total > int(incumbent_ticks))
    v['day_total']=total
    if shard:
        first=by_order[shard['first_order']][0]
        m.add(v['forge_present'][first] == 1)
        for ids in by_order.values():
            if ids[0] != first:
                m.add(v['forge_start'][first] < v['forge_start'][ids[0]])
        m.add(v['furnace'][first] == shard['first_furnace'])
        # The sole press cannot accumulate more work than the remaining day
        # after this shard's globally first forging starts.
        m.add(total + v['forge_start'][first] <= D)
        if 'first_start_ticks' in shard:
            lo,hi=shard['first_start_ticks']
            m.add(v['forge_start'][first] >= lo)
            m.add(v['forge_start'][first] <= min(hi,D-1))
    error=m.validate()
    if error:
        raise ValueError(error)
    return m,v,ops


def extract_prefix(solver, variables, day_end_ticks):
    return {**{k:[int(solver.value(x)) for x in variables[k]]
               for k in ('heat_start','forge_start','furnace')},
            'day_end_ticks':int(day_end_ticks),
            'day_score_ticks':int(solver.value(variables['day_total']))}


def project_schedule(scenario_or_operations, schedule, day_end_ticks):
    """Canonical day projection of a complete schedule (no solver required)."""
    operations=flatten(scenario_or_operations) if isinstance(scenario_or_operations,dict) else scenario_or_operations
    D=int(day_end_ticks)
    a=[min(D,int(x)) for x in schedule['heat_start']]
    s=[min(D,int(x)) for x in schedule['forge_start']]
    f=[int(f) if start<D else -1 for start,f in zip(a,schedule['furnace'])]
    score=sum(min(q['duration'],D-start) for q,start in zip(operations,s))
    return {'heat_start':a,'forge_start':s,'furnace':f,
            'day_end_ticks':D,'day_score_ticks':int(score)}


def fix_prefix(full_model, full_variables, prefix, day_end_ticks=None):
    """Fix the projected heat/forge starts and furnaces, not all device states.

    For ongoing heats a and furnace are fixed but s>=D remains a suffix choice.
    A forging starting exactly D is a future decision with zero first-day work.
    The full model retains its ordinary 4-day feasibility constraints.
    With transport enabled vehicle assignments, empty movements and logical
    handover order remain decisions of the complete continuation model.
    """
    D=int(prefix['day_end_ticks'] if day_end_ticks is None else day_end_ticks)
    n=len(full_variables['heat_start'])
    if any(len(prefix[k])!=n for k in ('heat_start','forge_start','furnace')):
        raise ValueError('Prefix operation count differs from full model')
    for i in range(n):
        a,s,f=(int(prefix[k][i]) for k in ('heat_start','forge_start','furnace'))
        if not 0<=a<=s<=D or (a==D and f!=-1) or (a<D and f<0):
            raise ValueError(f'Invalid prefix at operation {i}')
        for key,value in (('heat_start',a),('forge_start',s)):
            if value<D:
                full_model.add(full_variables[key][i] == value)
            else:
                full_model.add(full_variables[key][i] >= D)
        if a<D:
            full_model.add(full_variables['furnace'][i] == f)


def exclude_prefix(model, variables, prefix):
    """Exclude exactly one canonical prefix after checking its continuation."""
    flat_vars=[x for k in ('heat_start','forge_start','furnace') for x in variables[k]]
    flat_values=[int(x) for k in ('heat_start','forge_start','furnace') for x in prefix[k]]
    model.add_forbidden_assignments(flat_vars, [flat_values])


def make_prefix_shards(scenario):
    """Disjoint positive-score partition: first order class x first furnace."""
    candidates={}
    for order in scenario['orders']:
        key=(order['route'],order.get('release_minutes',0),order.get('due_minutes',scenario['horizon_minutes']))
        if scenario.get('quotient_identical_orders', True):
            candidates.setdefault(key,order['id'])
        else:
            candidates[order['id']]=order['id']
    return [{'id':f'shard-{j*scenario["furnace_count"]+f:03d}',
             'first_order':order,'first_furnace':f}
            for j,order in enumerate(candidates.values())
            for f in range(scenario['furnace_count'])]
