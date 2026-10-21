"""Optimistic route bounds, matching the frozen vehicle CP model.

Nonempty bounds are necessary, never a certificate of a feasible continuation.
All quantities are integer ticks. Observed/committed times only tighten bounds.
"""
from collections import defaultdict
from longda_cpsat.model import flatten, ticks, ramp_ticks


def route_windows(sc, schedule=None, ops=None):
    ops = flatten(sc) if ops is None else ops
    tau = ticks(sc['transport']['travel_minutes'], sc['ticks_per_minute'])
    horizon = ticks(sc['horizon_minutes'], sc['ticks_per_minute'])
    by = defaultdict(list)
    for q in ops:
        by[q['order']].append(q['index'])
    bounds = [{} for _ in ops]
    for ids in by.values():
        previous_end = 0
        for pos, i in enumerate(ids):
            q = ops[i]
            a = max(q['release'] + tau if pos == 0 else previous_end + tau,
                    ramp_ticks(sc, sc['initial_temperature'], q['temp']))
            if schedule and schedule['heat_start'][i] is not None:
                a = max(a, schedule['heat_start'][i])
            s = a + q['lower'] + tau
            if schedule and schedule['forge_start'][i] is not None:
                s = max(s, schedule['forge_start'][i])
            previous_end = s + q['duration']
            bounds[i].update(earliest_heat=a, earliest_forge=s)
        latest_end = min(ops[ids[-1]]['due'], horizon) - tau
        for i in reversed(ids):
            q = ops[i]
            latest_s = latest_end - q['duration']
            bounds[i].update(latest_forge=latest_s,
                             latest_heat=latest_s - tau - q['lower'])
            latest_end = latest_s - q['lower'] - 2 * tau
    return bounds


def contradictions(sc, schedule, ops=None):
    ops = flatten(sc) if ops is None else ops
    bounds = route_windows(sc, schedule, ops)
    result = []
    for q, b in zip(ops, bounds):
        i = q['index']
        if b['earliest_forge'] > b['latest_forge']:
            result.append(dict(reason='remaining_route_deadline', op=i,
                               order=q['order'], fire=q['fire'], **b,
                               proof_scope='current_partial_commitments',
                               excess_ticks=b['earliest_forge']-b['latest_forge']))
    return result
