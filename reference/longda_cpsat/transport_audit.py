"""Independent finite-vehicle validation; imports no solver or model builder.

The material route determines every mandatory loaded move. Vehicle journeys,
empty repositioning, thermal occupancy and zero-duration handling order are
reconstructed from those moves, rather than trusted from reported task labels.
"""
from collections import defaultdict

from .audit import flatten_operations, _fraction, _ticks, _is_int, _ramp_ticks


def audit_transport(scenario, schedule, *, check_symmetry=True):
    errors = []

    def fail(code, detail, **extra):
        errors.append(dict(code=code, detail=detail, **extra))

    def rejected(**extra):
        return dict(valid=False, errors=errors, replay={"complete": False}, **extra)

    try:
        ops = flatten_operations(scenario)
        scale = scenario["ticks_per_minute"]
        horizon = _ticks(scenario["horizon_minutes"], scale)
        furnace_count = scenario["furnace_count"]
        capacity = scenario["capacity"]
        config = scenario["transport"]
        vehicles = config.get("vehicle_count", 1)
        tau = _ticks(config.get("travel_minutes", 5), scale)
        if config.get("enabled") is not True:
            raise ValueError("finite transport must be explicitly enabled")
        if config.get("initial_location", "cold") != "cold":
            raise ValueError("this model requires all empty vehicles initially at cold area")
        if any(not _is_int(x) or x <= 0 for x in (furnace_count, capacity, vehicles, horizon, tau)):
            raise ValueError("furnaces, capacity, vehicles, horizon and travel duration must be positive integers")
        if not ops:
            raise ValueError("at least one operation is required")
        initial_temperature = scenario["initial_temperature"]
        _fraction(initial_temperature)
        if _fraction(scenario["ramp_up_deg_per_min"]) <= 0 or _fraction(scenario["ramp_down_deg_per_min"]) <= 0:
            raise ValueError("ramp rates must be positive")
        for q in ops:
            _fraction(q["temp"])
            _ramp_ticks(scenario, initial_temperature, q["temp"])
    except (KeyError, TypeError, ValueError, ZeroDivisionError) as exc:
        fail("scenario_schema", str(exc))
        return rejected()
    n = len(ops)
    if not isinstance(schedule, dict):
        fail("schedule_shape", "schedule must be an object")
        return rejected(operations=n)
    columns = {}
    for key in ("heat_start", "heat_end", "forge_start", "forge_end", "furnace"):
        values = schedule.get(key)
        if not isinstance(values, list) or len(values) != n:
            fail("schedule_shape", f"{key} must be a list of length {n}")
        elif any(not _is_int(x) for x in values):
            fail("integer_ticks", f"{key} must contain integers without booleans")
        else:
            columns[key] = values
    if len(columns) != 5:
        return rejected(operations=n)
    a, x, s, e, f = (columns[key] for key in ("heat_start", "heat_end", "forge_start", "forge_end", "furnace"))
    if any(not 0 <= fr < furnace_count for fr in f):
        fail("furnace_assignment", "furnace outside configured range")
        return rejected(operations=n)
    cold, press, finished = 0, furnace_count + 1, furnace_count + 2
    by_order = defaultdict(list)
    for i, q in enumerate(ops):
        by_order[q["order_index"]].append(i)
        if not 0 <= a[i] <= x[i] <= s[i] <= e[i] <= horizon:
            fail("time_domain", "operation times must be ordered within horizon", operation=i)
        if not q["hold_min_ticks"] <= x[i] - a[i] <= q["hold_max_ticks"]:
            fail("holding_window", "effective hold excludes transport and must meet min/max", operation=i)
        if s[i] != x[i] + tau:
            fail("forge_arrival", "forging must start exactly at furnace-to-press arrival", operation=i)
        if e[i] != s[i] + q["forge_ticks"]:
            fail("forge_duration", "forge_end must equal forge_start plus prescribed duration", operation=i)

    expected = []
    incoming, outgoing, to_press, from_press = {}, {}, {}, {}
    final_deliveries = {}

    def move(kind, i, source, destination, start, end):
        task = dict(id=len(expected), kind=kind, order=ops[i]["order"], op=i,
                    source=source, destination=destination, start=start, end=end)
        expected.append(task)
        return task["id"]

    for order_no, ids in by_order.items():
        first = ids[0]
        incoming[first] = move("cold_to_furnace", first, cold, f[first] + 1,
                               a[first] - tau, a[first])
        if a[first] - tau < ops[first]["release_ticks"] or a[first] - tau < 0:
            fail("release", "cold pickup must occur after material release and time zero", operation=first)
        for position, i in enumerate(ids):
            outgoing[i] = to_press[i] = move("furnace_to_press", i, f[i] + 1, press, x[i], s[i])
            if position + 1 < len(ids):
                j = ids[position + 1]
                from_press[i] = incoming[j] = move("press_to_furnace", i, press, f[j] + 1, e[i], a[j])
                if a[j] != e[i] + tau:
                    fail("immediate_return", "empty vehicle must collect at forge end and return in one trip",
                         previous=i, operation=j)
            else:
                from_press[i] = move("press_to_finished", i, press, finished, e[i], e[i] + tau)
                final_deliveries[ops[i]["order"]] = e[i] + tau
                if e[i] + tau > min(ops[i]["due_ticks"], horizon):
                    fail("due", "finished product must reach finished area by due/horizon", operation=i)
    total = len(expected)
    E, base = 2 * total, 2 * total + 1
    raw = schedule.get("transport")
    if not isinstance(raw, list) or len(raw) != total:
        fail("transport_shape", f"transport must contain exactly {total} mandatory loaded tasks")
        return rejected(operations=n)
    actual = {}
    integer_keys = ("id", "op", "source", "destination", "start", "end", "vehicle", "pickup_rank", "dropoff_rank")
    for row in raw:
        if not isinstance(row, dict) or any(not _is_int(row.get(k)) for k in integer_keys):
            fail("transport_shape", "transport integer fields are missing, noninteger or boolean")
            continue
        task_id = row["id"]
        if not 0 <= task_id < total or task_id in actual:
            fail("transport_identity", "task IDs must be unique and cover the expected route tasks", task=task_id)
            continue
        actual[task_id] = row
    if len(actual) != total:
        return rejected(operations=n)
    for expected_task in expected:
        row = actual[expected_task["id"]]
        for key, required in expected_task.items():
            if row.get(key) != required:
                fail("transport_route", "reported task differs from route and operation times",
                     task=expected_task["id"], field=key, expected=required, actual=row.get(key))
        if not 0 <= row["vehicle"] < vehicles:
            fail("vehicle_assignment", "vehicle index outside configured range", task=row["id"])
        if not 0 <= row["pickup_rank"] < E or not 0 <= row["dropoff_rank"] < E:
            fail("event_rank", "handling ranks outside finite logical order domain", task=row["id"])
        if row["end"] - row["start"] != tau or not 0 <= row["start"] < row["end"] <= horizon:
            fail("transport_duration", "each mandatory loaded trip must last exactly travel duration inside horizon",
                 task=row["id"])
    if any(error["code"] in ("vehicle_assignment", "event_rank", "transport_route", "transport_duration") for error in errors):
        return rejected(operations=n, loaded_tasks=total)

    def pickup(task_id):
        row = actual[task_id]
        return row["start"] * base + row["pickup_rank"]

    def dropoff(task_id):
        row = actual[task_id]
        return row["end"] * base + row["dropoff_rank"]

    # Consecutive tasks determine the actual vehicle's location, unlike a bare
    # cumulative resource bound which could teleport it between two furnaces.
    empty_moves = []
    by_vehicle = defaultdict(list)
    for row in actual.values():
        by_vehicle[row["vehicle"]].append(row)
    for vehicle in range(vehicles):
        route = sorted(by_vehicle[vehicle], key=lambda row: (pickup(row["id"]), row["id"]))
        previous = None
        for row in route:
            origin = cold if previous is None else previous["destination"]
            available = 0 if previous is None else previous["end"]
            empty_duration = 0 if origin == row["source"] else tau
            if previous is None:
                if row["start"] < empty_duration:
                    fail("vehicle_initial_location", "vehicle cannot reach initial pickup from cold area", vehicle=vehicle, task=row["id"])
            else:
                if row["start"] < previous["end"]:
                    fail("vehicle_overlap", "single-capacity vehicle has overlapping loaded trips", vehicle=vehicle,
                         previous=previous["id"], task=row["id"])
                if pickup(row["id"]) < dropoff(previous["id"]) + empty_duration * base + 1:
                    fail("vehicle_empty_travel", "next pickup lacks empty travel time or drop-before-pickup order",
                         vehicle=vehicle, previous=previous["id"], task=row["id"])
            if empty_duration:
                empty_moves.append(dict(vehicle=vehicle, source=origin, destination=row["source"],
                                        start=available, end=available + empty_duration,
                                        next_task=row["id"]))
            previous = row

    press_order = sorted(range(n), key=lambda i: (s[i], i))
    for prior, current in zip(press_order, press_order[1:]):
        if e[prior] > s[current]:
            fail("press_overlap", "two physical forging intervals overlap", previous=prior, operation=current)
        if pickup(from_press[prior]) > dropoff(to_press[current]):
            fail("press_exchange", "finished material must be collected before next material is deposited",
                 previous=prior, operation=current)

    if check_symmetry and scenario.get("quotient_identical_orders", True):
        groups = defaultdict(list)
        for ids in by_order.values():
            i = ids[0]
            groups[(ops[i]["route"], ops[i]["release_ticks"], ops[i]["due_ticks"])].append(i)
        for ids in groups.values():
            for prior, current in zip(ids, ids[1:]):
                if s[prior] >= s[current]:
                    fail("order_symmetry", "identical orders must have canonical first forging order", previous=prior, operation=current)

    max_occupancy = {str(fr): 0 for fr in range(furnace_count)}
    furnace_ops = defaultdict(list)
    for i in range(n):
        furnace_ops[f[i]].append(i)
    for furnace, ids in furnace_ops.items():
        events = defaultdict(lambda: {"exit": [], "enter": []})
        for i in ids:
            events[dropoff(incoming[i])]["enter"].append(i)
            events[pickup(outgoing[i])]["exit"].append(i)
        occupants = set()
        for event, changes in sorted(events.items()):
            for i in changes["exit"]:
                occupants.discard(i)
            for i in changes["enter"]:
                occupants.add(i)
            max_occupancy[str(furnace)] = max(max_occupancy[str(furnace)], len(occupants))
            if len(occupants) > capacity:
                fail("furnace_capacity", "including zero-time handling order, capacity is exceeded",
                     furnace=furnace, event=event, occupants=sorted(occupants))
            if len({_fraction(ops[i]["temp"]) for i in occupants}) > 1:
                fail("temperature_overlap", "incompatible temperatures coexist during furnace occupancy",
                     furnace=furnace, event=event, occupants=sorted(occupants))

    blocks, ramps = [], []
    for furnace in range(furnace_count):
        furnace_blocks = []
        for i in sorted(furnace_ops[furnace], key=lambda i: (a[i], x[i], i)):
            temp = ops[i]["temp"]
            if furnace_blocks and _fraction(furnace_blocks[-1]["temperature"]) == _fraction(temp):
                furnace_blocks[-1]["end_ticks"] = max(furnace_blocks[-1]["end_ticks"], x[i])
                furnace_blocks[-1]["operations"].append(i)
            else:
                furnace_blocks.append(dict(furnace=furnace, temperature=temp,
                                           start_ticks=a[i], end_ticks=x[i], operations=[i]))
        previous_temperature, previous_end = initial_temperature, 0
        for block in furnace_blocks:
            try:
                duration = _ramp_ticks(scenario, previous_temperature, block["temperature"])
            except (TypeError, ValueError, ZeroDivisionError) as exc:
                fail("ramp_precision", str(exc), furnace=furnace)
                duration = 0
            ready = previous_end + duration
            if ready > block["start_ticks"]:
                fail("ramp_gap", "empty furnace has insufficient time to reach next required temperature",
                     furnace=furnace, operations=block["operations"], required_ticks=duration,
                     available_ticks=block["start_ticks"] - previous_end)
            if duration:
                ramps.append(dict(furnace=furnace, from_temperature=previous_temperature,
                                  to_temperature=block["temperature"], start_ticks=previous_end, end_ticks=ready))
            previous_temperature, previous_end = block["temperature"], block["end_ticks"]
        blocks.extend(furnace_blocks)

    # Explicit material/cargo replay at ranked handling events rejects a vehicle
    # carrying A arriving at an occupied press and claiming a simultaneous swap.
    replay = _replay_handling(ops, expected, actual, furnace_count, capacity, vehicles, base, fail)
    finish = max(final_deliveries.values())
    physical_times = {0, finish}
    for row in actual.values():
        physical_times.update((row["start"], row["end"]))
    for ramp in ramps:
        physical_times.update((ramp["start_ticks"], ramp["end_ticks"]))
    for i in range(n):
        physical_times.update((a[i], x[i], s[i], e[i]))
        physical_times.update(t for t in (a[i] + ops[i]["hold_min_ticks"], a[i] + ops[i]["hold_max_ticks"]) if t <= x[i])
    ticks = sorted(t for t in physical_times if 0 <= t <= finish)
    clock, steps, maximum_step = 0, 0, 0
    pointer = 0
    while clock < finish:
        while pointer < len(ticks) and ticks[pointer] <= clock:
            pointer += 1
        target = ticks[pointer] if pointer < len(ticks) else finish
        following = min(clock + 5 * scale, target, finish)
        if following <= clock:
            fail("replay_clock", "non-advancing replay time")
            break
        maximum_step = max(maximum_step, following - clock)
        clock = following
        steps += 1
    replay.update(complete=not errors and clock == finish, end_ticks=clock, steps=steps,
                  step_cap_ticks=5 * scale, maximum_actual_step_ticks=maximum_step,
                  physical_events=len(ticks))
    work = sum(op["forge_ticks"] for op in ops)
    day_ticks = 1440 * scale
    daily = []
    for day in range((horizon + day_ticks - 1) // day_ticks):
        start = day * day_ticks
        busy = sum(max(0, min(e[i], start + day_ticks) - max(s[i], start)) for i in range(n))
        daily.append(dict(day=day, busy_ticks=busy, busy_minutes=busy / scale, utilization=busy / day_ticks))
    return dict(valid=not errors, errors=errors, operations=n, orders=len(by_order),
                ticks_per_minute=scale, makespan_ticks=finish, makespan_minutes=finish / scale,
                forge_work_ticks=work, forge_work_minutes=work / scale,
                holding_ticks=sum(x[i] - a[i] for i in range(n)),
                extra_holding_ticks=sum(x[i] - a[i] - ops[i]["hold_min_ticks"] for i in range(n)),
                planning_horizon_utilization=work / horizon, makespan_utilization=work / finish if finish else 0,
                daily=daily, max_furnace_occupancy=max_occupancy,
                canonical_furnace_blocks=blocks, canonical_ramps=ramps, replay=replay,
                loaded_tasks=total, vehicle_count=vehicles, canonical_empty_moves=empty_moves,
                final_delivery_ticks=final_deliveries,
                audit_scope="independent finite one-piece vehicles; exact empty travel and handling order; no door recovery, splitting or wrapping")


def _replay_handling(ops, expected, actual, furnace_count, capacity, vehicles, base, fail):
    cold, press, finished = 0, furnace_count + 1, furnace_count + 2
    position = {op["order"]: cold for op in ops}
    cargo = {vehicle: None for vehicle in range(vehicles)}
    furnaces = {f: set() for f in range(1, furnace_count + 1)}
    press_material = None
    events = []
    for row in actual.values():
        events.append((row["start"] * base + row["pickup_rank"], 0, row["id"], "pickup"))
        events.append((row["end"] * base + row["dropoff_rank"], 1, row["id"], "dropoff"))
    for event, _priority, task_id, action in sorted(events):
        row = actual[task_id]
        order, vehicle = row["order"], row["vehicle"]
        if action == "pickup":
            if cargo[vehicle] is not None:
                fail("replay_vehicle_capacity", "pickup requires an empty single-piece vehicle", task=task_id, event=event)
            if position[order] != row["source"]:
                fail("replay_material_location", "pickup source differs from material's current location", task=task_id, event=event)
            if row["source"] == press:
                if press_material != order:
                    fail("replay_press_pickup", "press does not contain the claimed finished material", task=task_id, event=event)
                press_material = None
            elif 1 <= row["source"] <= furnace_count:
                if order not in furnaces[row["source"]]:
                    fail("replay_furnace_pickup", "furnace does not contain the claimed material", task=task_id, event=event)
                furnaces[row["source"]].discard(order)
            cargo[vehicle] = task_id
            position[order] = ("vehicle", vehicle)
        else:
            if cargo[vehicle] != task_id or position[order] != ("vehicle", vehicle):
                fail("replay_vehicle_cargo", "vehicle did not carry this task's material", task=task_id, event=event)
            if row["destination"] == press:
                if press_material is not None:
                    fail("replay_press_exchange", "incoming material cannot be unloaded onto an occupied press", task=task_id, event=event)
                press_material = order
            elif 1 <= row["destination"] <= furnace_count:
                furnace = row["destination"]
                furnaces[furnace].add(order)
                if len(furnaces[furnace]) > capacity:
                    fail("replay_furnace_capacity", "drop-before-pickup handling exceeded furnace capacity", task=task_id, event=event)
            cargo[vehicle] = None
            position[order] = row["destination"]
    complete = (all(where == finished for where in position.values()) and
                all(value is None for value in cargo.values()) and
                press_material is None and not any(furnaces.values()))
    if not complete:
        fail("replay_incomplete", "all materials must reach finished area and leave devices/vehicles empty")
    return dict(complete=complete, handling_events=len(events),
                semantics="ranked zero-time handling; departure before arrival at equal logical event; empty-car travel independently checked")
