"""Exact one-piece vehicle movements and zero-duration event ordering.

Physical times remain integer ticks. Ranks order instantaneous handovers at
the same physical time; they do not add travel or processing duration.
"""
from collections import defaultdict

from ortools.sat.python import cp_model


def transport_enabled(scenario):
    config = scenario.get('transport', {})
    if not isinstance(config, dict):
        raise ValueError('transport must be an object')
    enabled = config.get('enabled', False)
    if not isinstance(enabled, bool):
        raise ValueError('transport.enabled must be a boolean')
    return enabled


def build_transport_model(scenario, shard=None, enforce_vehicle=True, *, vehicle_prefix_end=None):
    """Build full thermal rules and either full or day-prefix vehicle rules.

    With vehicle_prefix_end=D, enforce shared vehicles for every loaded trip
    whose pickup is strictly before D. Such trips retain their entire duration,
    including arrivals after D. Future vehicle paths remain suffix decisions.
    The default builds exactly the ordinary complete transport model.
    """
    # Imported lazily to keep model.build_model's optional dispatch acyclic.
    from .model import flatten, ramp_ticks, ticks

    config = scenario['transport']
    count = scenario['furnace_count']
    cars = config.get('vehicle_count', 1)
    if isinstance(cars, bool) or not isinstance(cars, int) or cars < 1:
        raise ValueError('transport.vehicle_count must be a positive integer')
    if config.get('initial_location', 'cold') != 'cold':
        raise ValueError('All vehicles must initially be empty at the cold area')
    tau = ticks(config.get('travel_minutes', 5), scenario['ticks_per_minute'])
    if tau <= 0:
        raise ValueError('Enabled transport requires a positive travel time')
    horizon = ticks(scenario['horizon_minutes'], scenario['ticks_per_minute'])
    if vehicle_prefix_end is not None:
        if (not enforce_vehicle or isinstance(vehicle_prefix_end, bool)
                or not isinstance(vehicle_prefix_end, int)
                or not 0 < vehicle_prefix_end <= horizon):
            raise ValueError('vehicle_prefix_end requires enforced vehicles and integer ticks in (0, horizon]')
    ops = flatten(scenario)
    by_order, by_type = defaultdict(list), defaultdict(list)
    for q in ops:
        by_order[q['order']].append(q['index'])
    for order in scenario['orders']:
        key = (order['route'], order.get('release_minutes', 0),
               order.get('due_minutes', scenario['horizon_minutes']))
        by_type[key].append(by_order[order['id']][0])
    model = cp_model.CpModel()
    variables = {key: [] for key in ('heat_start', 'forge_start', 'furnace',
                                     'heat_duration', 'forge_end', 'heat_end')}
    ea, es, ls = {}, {}, {}
    for ids in by_order.values():
        previous_end = 0
        for position, i in enumerate(ids):
            q = ops[i]
            ready = q['release'] + tau if position == 0 else previous_end + tau
            ea[i] = max(ready, ramp_ticks(scenario, scenario['initial_temperature'], q['temp']))
            es[i] = ea[i] + q['lower'] + tau
            previous_end = es[i] + q['duration']
        latest_end = min(ops[ids[-1]]['due'], horizon) - tau
        for i in reversed(ids):
            q = ops[i]
            ls[i] = latest_end - q['duration']
            latest_end = ls[i] - q['lower'] - 2 * tau

    physical_press = []
    max_time = horizon
    for i, q in enumerate(ops):
        if es[i] > ls[i]:
            model.add_bool_or([])
        latest_s = max(es[i], ls[i])
        a = model.new_int_var(ea[i], max(ea[i], latest_s - tau - q['lower']), f'a{i}')
        s = model.new_int_var(es[i], latest_s, f's{i}')
        h = model.new_int_var(q['lower'], q['upper'], f'h{i}')
        f = model.new_int_var(0, count - 1, f'f{i}')
        e = model.new_int_var(es[i] + q['duration'], latest_s + q['duration'], f'e{i}')
        x = model.new_int_var(ea[i] + q['lower'], latest_s - tau, f'x{i}')
        for key, value in zip(variables, (a, s, f, h, e, x)):
            variables[key].append(value)
        model.add(x == a + h)
        model.add(s == x + tau)
        model.add(e == s + q['duration'])
        model.add(e <= min(q['due'], horizon))
        physical_press.append(model.new_interval_var(s, q['duration'], e, f'press{i}'))
        max_time = max(max_time, latest_s + q['duration'] + tau)
    model.add_no_overlap(physical_press)
    single_car = enforce_vehicle and cars == 1
    if single_car:
        # The sole empty car must collect at forge end, drive out (tau),
        # then bring the next piece to the press (at least tau). The virtual
        # tail is a propagation device and may extend beyond the horizon.
        model.add_no_overlap([
            model.new_interval_var(variables['forge_start'][i], q['duration'] + 2*tau,
                                   variables['forge_end'][i] + 2*tau, f'press_with_single_car_gap{i}')
            for i, q in enumerate(ops)
        ])
    for ids in by_order.values():
        for i, j in zip(ids, ids[1:]):
            model.add(variables['heat_start'][j] == variables['forge_end'][i] + tau)
        last = ids[-1]
        model.add(variables['forge_end'][last] + tau <= min(ops[last]['due'], horizon))
    if scenario.get('quotient_identical_orders', True):
        for ids in by_type.values():
            for i, j in zip(ids, ids[1:]):
                model.add(variables['forge_start'][i] < variables['forge_start'][j])

    # Reuse the same mathematical furnace-equality predicate across thermal
    # compatibility and empty travel; do not build it anew for every trip pair.
    furnace_equalities = {}

    def same_furnace(i, j):
        if i == j:
            return 1
        key = (min(i, j), max(i, j))
        if key not in furnace_equalities:
            equal = model.new_bool_var(f'same_furnace{key[0]}_{key[1]}')
            model.add(variables['furnace'][i] == variables['furnace'][j]).only_enforce_if(equal)
            model.add(variables['furnace'][i] != variables['furnace'][j]).only_enforce_if(equal.Not())
            furnace_equalities[key] = equal
        return furnace_equalities[key]

    # Different temperatures require an empty furnace for its whole ramp.
    for i, qi in enumerate(ops):
        for j in range(i + 1, len(ops)):
            qj = ops[j]
            if qi['temp'] == qj['temp']:
                continue
            dij = ramp_ticks(scenario, qi['temp'], qj['temp'])
            dji = ramp_ticks(scenario, qj['temp'], qi['temp'])
            if ls[i] - tau + dij <= ea[j] or ls[j] - tau + dji <= ea[i]:
                continue
            same = same_furnace(i, j)
            if qi['order'] == qj['order']:
                model.add(variables['heat_start'][j] >= variables['heat_end'][i] + dij).only_enforce_if(same)
            else:
                before = model.new_bool_var(f'before{i}_{j}')
                model.add(variables['heat_start'][i] <= variables['heat_start'][j]).only_enforce_if(before)
                model.add(variables['heat_start'][i] > variables['heat_start'][j]).only_enforce_if(before.Not())
                model.add(variables['heat_start'][j] >= variables['heat_end'][i] + dij).only_enforce_if([same, before])
                model.add(variables['heat_start'][i] >= variables['heat_end'][j] + dji).only_enforce_if([same, before.Not()])

    # One cold delivery per order, one furnace->press and one press->next
    # destination for each fire. Every loaded move changes location.
    task_count = len(by_order) + 2 * len(ops)
    event_count = 2 * task_count
    # With one car, any coincident handovers must unload before picking up.
    # Fixed ranks represent every physically possible one-car schedule. With
    # multiple cars the opposite order can be necessary, so keep free ranks.
    event_base = 2 if single_car else event_count + 1
    cold, press, finished = 0, count + 1, count + 2
    tasks = []
    incoming, outgoing, press_in, press_out = {}, {}, {}, {}

    def task(kind, i, source, destination, start, end, source_furnace=None, destination_furnace=None):
        number = len(tasks)
        if single_car:
            pickup_rank, dropoff_rank = 1, 0
            pickup_event, dropoff_event = start * 2 + 1, end * 2
        else:
            pickup_rank = model.new_int_var(0, event_count - 1, f'pickup_rank{number}')
            dropoff_rank = model.new_int_var(0, event_count - 1, f'dropoff_rank{number}')
            pickup_event = model.new_int_var(0, max_time * event_base + event_count, f'pickup_event{number}')
            dropoff_event = model.new_int_var(0, max_time * event_base + event_count, f'dropoff_event{number}')
            model.add(pickup_event == start * event_base + pickup_rank)
            model.add(dropoff_event == end * event_base + dropoff_rank)
        model.add(end == start + tau)
        vehicle = 0 if single_car else model.new_int_var(0, cars - 1, f'vehicle{number}')
        item = dict(id=number, kind=kind, order=ops[i]['order'], op=i,
                    source=source, destination=destination, start=start, end=end,
                    vehicle=vehicle, pickup_rank=pickup_rank, dropoff_rank=dropoff_rank,
                    pickup_event=pickup_event, dropoff_event=dropoff_event,
                    _source_furnace=source_furnace, _destination_furnace=destination_furnace)
        tasks.append(item)
        return item

    for ids in by_order.values():
        first = ids[0]
        a = variables['heat_start'][first]
        pickup = model.new_int_var(0, max_time, f'cold_pickup{first}')
        model.add(pickup == a - tau)
        model.add(pickup >= ops[first]['release'])
        initial = task('cold_to_furnace', first, cold, variables['furnace'][first] + 1, pickup, a,
                       destination_furnace=first)
        incoming[first] = initial
        for position, i in enumerate(ids):
            to_press = task('furnace_to_press', i, variables['furnace'][i] + 1, press,
                            variables['heat_end'][i], variables['forge_start'][i], source_furnace=i)
            outgoing[i] = press_in[i] = to_press
            if position + 1 < len(ids):
                j = ids[position + 1]
                returned = task('press_to_furnace', i, press, variables['furnace'][j] + 1,
                                variables['forge_end'][i], variables['heat_start'][j], destination_furnace=j)
                incoming[j] = returned
            else:
                delivered = model.new_int_var(0, max_time, f'finished_delivery{i}')
                model.add(delivered == variables['forge_end'][i] + tau)
                model.add(delivered <= min(ops[i]['due'], horizon))
                returned = task('press_to_finished', i, press, finished,
                                variables['forge_end'][i], delivered)
            press_out[i] = returned

    # Physical half-open intervals alone permit impossible zero-time swaps.
    # Logical event intervals also keep furnace/press occupancy until pickup
    # and forbid unloading before a slot has actually been freed.
    furnace_intervals = [[] for _ in range(count)]
    physical_furnace_intervals = [[] for _ in range(count)]
    press_intervals = []
    for i, q in enumerate(ops):
        entry = incoming[i]['dropoff_event']
        exit_ = outgoing[i]['pickup_event']
        duration = model.new_int_var(0, max_time * event_base + event_count, f'heat_event_duration{i}')
        model.add(duration == exit_ - entry)
        for f in range(count):
            present = model.new_bool_var(f'in{i}_{f}')
            model.add(variables['furnace'][i] == f).only_enforce_if(present)
            model.add(variables['furnace'][i] != f).only_enforce_if(present.Not())
            furnace_intervals[f].append(model.new_optional_interval_var(entry, duration, exit_, present, f'heat_events{i}_{f}'))
            physical_furnace_intervals[f].append(model.new_optional_interval_var(
                variables['heat_start'][i], variables['heat_duration'][i], variables['heat_end'][i],
                present, f'physical_heat{i}_{f}'))
        begin = press_in[i]['dropoff_event']
        end = press_out[i]['pickup_event']
        span = model.new_int_var(0, max_time * event_base + event_count, f'press_event_duration{i}')
        model.add(span == end - begin)
        press_intervals.append(model.new_interval_var(begin, span, end, f'press_events{i}'))
    for row in furnace_intervals:
        model.add_cumulative(row, [1] * len(row), scenario['capacity'])
    for row in physical_furnace_intervals:
        model.add_cumulative(row, [1] * len(row), scenario['capacity'])
    model.add_no_overlap(press_intervals)

    if enforce_vehicle:
        # Presence depends on pickup, not arrival. Never truncate a loaded
        # trip at midnight or reset the vehicle's location at the boundary.
        active = []
        loaded = []
        for item in tasks:
            if vehicle_prefix_end is None:
                loaded.append(model.new_interval_var(
                    item['start'], tau, item['end'], f'loaded_move{item["id"]}'))
            else:
                present = model.new_bool_var(f'day_vehicle_move{item["id"]}')
                model.add(item['start'] < vehicle_prefix_end).only_enforce_if(present)
                model.add(item['start'] >= vehicle_prefix_end).only_enforce_if(present.Not())
                active.append(present)
                loaded.append(model.new_optional_interval_var(
                    item['start'], tau, item['end'], present, f'loaded_move{item["id"]}'))
        if single_car:
            model.add_no_overlap(loaded)
        else:
            model.add_cumulative(loaded, [1] * len(loaded), cars)

        def empty_delay(first, second):
            destination_furnace = first['_destination_furnace']
            source_furnace = second['_source_furnace']
            if destination_furnace is not None and source_furnace is not None:
                return tau * (1 - same_furnace(destination_furnace, source_furnace))
            if destination_furnace is not None or source_furnace is not None:
                return tau
            return 0 if first['destination'] == second['source'] else tau

        def precedes(first, second, condition):
            delay = empty_delay(first, second)
            # Fixed one-car ranks cancel exactly; compare physical times.
            if single_car:
                constraint = model.add(second['start'] >= first['end'] + delay)
            else:
                constraint = model.add(second['pickup_event'] >= first['dropoff_event'] + delay*event_base + 1)
            if condition:
                constraint.only_enforce_if(condition)

        for item in tasks:
            # All cars are empty at cold at time zero. An initial trip to
            # any other pickup point takes tau, even if it has no prior task.
            model.add(item['start'] >= (0 if item['kind'] == 'cold_to_furnace' else tau))
        for i, first in enumerate(tasks):
            for j in range(i + 1, len(tasks)):
                second = tasks[j]
                if single_car:
                    shared = []
                else:
                    same = model.new_bool_var(f'same_car{i}_{j}')
                    model.add(first['vehicle'] == second['vehicle']).only_enforce_if(same)
                    model.add(first['vehicle'] != second['vehicle']).only_enforce_if(same.Not())
                    shared = [same]
                if vehicle_prefix_end is not None:
                    shared = shared + [active[i], active[j]]
                # Tasks of one order were appended in their mandatory route
                # order; no binary sequencing choice exists for this pair.
                if first['order'] == second['order']:
                    precedes(first, second, shared)
                    continue
                before = model.new_bool_var(f'car_before{i}_{j}')
                precedes(first, second, shared + [before])
                precedes(second, first, shared + [before.Not()])
        if vehicle_prefix_end is not None:
            variables['_vehicle_prefix_end'] = vehicle_prefix_end
            variables['_vehicle_active'] = active

    if shard:
        first = by_order[shard['first_order']][0]
        for ids in by_order.values():
            if ids[0] != first:
                model.add(variables['forge_start'][first] < variables['forge_start'][ids[0]])
        model.add(variables['furnace'][first] == shard['first_furnace'])
        if 'first_start_ticks' in shard:
            lo, hi = shard['first_start_ticks']
            model.add(variables['forge_start'][first] >= lo)
            model.add(variables['forge_start'][first] <= hi)
    variables['_transport'] = tasks
    variables['_event_base'] = event_base
    variables['_enforce_vehicle'] = bool(enforce_vehicle)
    error = model.validate()
    if error:
        raise ValueError(error)
    return model, variables, ops


def extract_transport_schedule(solver, variables):
    result = {key: [int(solver.value(x)) for x in variables[key]]
              for key in ('heat_start', 'forge_start', 'furnace', 'heat_end', 'forge_end')}
    result['transport'] = []
    scalar_fields = ('source', 'destination', 'start', 'end', 'vehicle',
                     'pickup_rank', 'dropoff_rank')
    for task in variables['_transport']:
        row = {key: task[key] for key in ('id', 'kind', 'order', 'op')}
        row.update({key: int(solver.value(task[key])) for key in scalar_fields})
        result['transport'].append(row)
    return result
