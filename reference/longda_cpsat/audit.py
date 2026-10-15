#!/usr/bin/env python3
"""Independent, standard-library audit of the simplified Longda experiment.

This module does not import CP-SAT or model.py. Intervals are half open and
simultaneous furnace exits are processed before entries. No door recovery,
transport, splitting, or wrapping is modeled. The model receives material only
after the furnace is at its required temperature. Empty-furnace ramps are
reconstructed canonically, as early as possible after the preceding occupation.

The integer tick lattice is the decision-time domain. The five-minute maximum
replay step is a simulation convention, not a five-minute decision grid. Planned
actions are events and can shorten that replay step.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from fractions import Fraction
import json
from pathlib import Path
from typing import Any


def _fraction(value: Any) -> Fraction:
    if isinstance(value, bool):
        raise ValueError("boolean is not a numerical parameter")
    return Fraction(str(value))


def _ticks(minutes: Any, scale: int) -> int:
    value = _fraction(minutes) * scale
    if value.denominator != 1:
        raise ValueError(f"{minutes} minutes is not representable at {scale} ticks/minute")
    return value.numerator


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def flatten_operations(scenario: dict) -> list[dict]:
    """Use input order order, then route operation order. All time fields ticks."""
    scale = scenario["ticks_per_minute"]
    if not _is_int(scale) or scale <= 0:
        raise ValueError("ticks_per_minute must be a positive integer")
    routes = {route["id"]: route for route in scenario["routes"]}
    if len(routes) != len(scenario["routes"]):
        raise ValueError("duplicate route id")
    if len({order["id"] for order in scenario["orders"]}) != len(scenario["orders"]):
        raise ValueError("duplicate order id")
    result = []
    for order_no, order in enumerate(scenario["orders"]):
        route = routes[order["route"]]
        if not route["operations"]:
            raise ValueError(f"empty route: {route['id']}")
        for stage, raw in enumerate(route["operations"]):
            lo = _ticks(raw["hold_min"], scale)
            hi = _ticks(raw["hold_max"], scale)
            p = _ticks(raw["forge"], scale)
            if not 0 < lo <= hi or p <= 0:
                raise ValueError(f"invalid processing durations: {order['id']} stage {stage}")
            result.append({
                "index": len(result), "order_index": order_no,
                "order": order["id"], "route": route["id"], "stage": stage,
                "fire": raw.get("fire", stage + 1), "temp": raw["temp"],
                "hold_min_ticks": lo, "hold_max_ticks": hi, "forge_ticks": p,
                "release_ticks": _ticks(order.get("release_minutes", 0), scale),
                "due_ticks": _ticks(order.get("due_minutes", scenario["horizon_minutes"]), scale),
            })
    return result


def _ramp_ticks(scenario: dict, source: Any, target: Any) -> int:
    delta = _fraction(target) - _fraction(source)
    rate_key = "ramp_up_deg_per_min" if delta >= 0 else "ramp_down_deg_per_min"
    rate = _fraction(scenario[rate_key])
    if rate <= 0:
        raise ValueError("ramp rates must be positive")
    result = abs(delta) / rate * scenario["ticks_per_minute"]
    if result.denominator != 1:
        raise ValueError(f"ramp {source} -> {target} not exactly representable on tick lattice")
    return result.numerator


def audit(scenario: dict, schedule: dict, *, check_symmetry: bool = True) -> dict:
    if isinstance(scenario.get("transport"), dict) and scenario["transport"].get("enabled", False):
        from .transport_audit import audit_transport
        return audit_transport(scenario, schedule, check_symmetry=check_symmetry)
    errors: list[dict] = []

    def fail(code: str, detail: str, **extra):
        errors.append({"code": code, "detail": detail, **extra})

    try:
        ops = flatten_operations(scenario)
        scale = scenario["ticks_per_minute"]
        horizon = _ticks(scenario["horizon_minutes"], scale)
        furnace_count = scenario["furnace_count"]
        capacity = scenario["capacity"]
        if not _is_int(furnace_count) or furnace_count <= 0:
            raise ValueError("furnace_count must be a positive integer")
        if not _is_int(capacity) or capacity <= 0 or horizon <= 0:
            raise ValueError("capacity and horizon must be positive")
        initial_temperature = scenario["initial_temperature"]
        _fraction(initial_temperature)
        for op in ops:
            _fraction(op["temp"])
            _ramp_ticks(scenario, initial_temperature, op["temp"])
    except (KeyError, TypeError, ValueError, ZeroDivisionError) as exc:
        return {"valid": False, "errors": [{"code": "scenario_schema", "detail": str(exc)}]}

    count = len(ops)
    if count == 0:
        fail("scenario_empty", "At least one operation is required")
    columns = {}
    for name in ("heat_start", "forge_start", "furnace"):
        values = schedule.get(name)
        if not isinstance(values, list) or len(values) != count:
            fail("schedule_shape", f"{name} must be a list of length {count}")
            continue
        if any(not _is_int(value) for value in values):
            fail("integer_ticks", f"{name} must contain integers, without booleans")
            continue
        columns[name] = values
    if len(columns) != 3:
        return {"valid": False, "errors": errors, "operations": count}
    a, s, f = (columns[name] for name in ("heat_start", "forge_start", "furnace"))
    ends = [s[q] + op["forge_ticks"] for q, op in enumerate(ops)]
    if any(not 0 <= furnace < furnace_count for furnace in f):
        fail("furnace_assignment", f"furnace must be in [0, {furnace_count - 1}]")
        return {"valid": False, "errors": errors, "operations": count}

    order_ops: dict[int, list[int]] = defaultdict(list)
    furnace_ops: dict[int, list[int]] = defaultdict(list)
    for q, op in enumerate(ops):
        order_ops[op["order_index"]].append(q)
        furnace_ops[f[q]].append(q)
        holding = s[q] - a[q]
        if not 0 <= a[q] <= s[q] or ends[q] > horizon:
            fail("time_domain", "operation outside planning horizon or negative holding", operation=q)
        if not op["hold_min_ticks"] <= holding <= op["hold_max_ticks"]:
            fail("holding_window", "effective holding outside min/max", operation=q, holding_ticks=holding)
        if a[q] < op["release_ticks"]:
            fail("release", "operation begins before order release", operation=q)
        if ends[q] > op["due_ticks"]:
            fail("due", "operation finishes after order deadline", operation=q)
    for order_no, indices in order_ops.items():
        for previous, current in zip(indices, indices[1:]):
            if a[current] != ends[previous]:
                fail("immediate_return", "next holding must begin exactly at previous forging end",
                     order=order_no, previous=previous, operation=current)

    press_order = sorted(range(count), key=lambda q: (s[q], q))
    for previous, current in zip(press_order, press_order[1:]):
        if ends[previous] > s[current]:
            fail("press_overlap", "two forging operations overlap", previous=previous, operation=current)

    symmetry_groups: dict[tuple, list[int]] = defaultdict(list)
    if check_symmetry:
        for order_no, indices in order_ops.items():
            first = indices[0]
            op = ops[first]
            symmetry_groups[(op["route"], op["release_ticks"], op["due_ticks"])].append(first)
        for indices in symmetry_groups.values():
            for previous, current in zip(indices, indices[1:]):
                if s[previous] >= s[current]:
                    fail("order_symmetry", "same-type first forging starts must follow input order",
                         previous=previous, operation=current)

    max_occupancy = {str(fr): 0 for fr in range(furnace_count)}
    for fr, indices in furnace_ops.items():
        events: dict[int, dict[str, list[int]]] = defaultdict(lambda: {"exit": [], "enter": []})
        for q in indices:
            events[a[q]]["enter"].append(q)
            events[s[q]]["exit"].append(q)
        occupants: set[int] = set()
        for when, events_at_t in sorted(events.items()):
            # Half-open intervals: outgoing pieces release capacity before incoming ones.
            for q in events_at_t["exit"]:
                if q in occupants:
                    occupants.remove(q)
            for q in events_at_t["enter"]:
                if a[q] < s[q]:
                    occupants.add(q)
            max_occupancy[str(fr)] = max(max_occupancy[str(fr)], len(occupants))
            if len(occupants) > capacity:
                fail("furnace_capacity", "too many materials in furnace", furnace=fr, time_ticks=when,
                     occupants=sorted(occupants))
            temperatures = {_fraction(ops[q]["temp"]) for q in occupants}
            if len(temperatures) > 1:
                fail("temperature_overlap", "incompatible temperatures occupy furnace simultaneously",
                     furnace=fr, time_ticks=when, occupants=sorted(occupants))

    blocks: list[dict] = []
    ramps: list[dict] = []
    for fr in range(furnace_count):
        fr_blocks: list[dict] = []
        for q in sorted(furnace_ops.get(fr, []), key=lambda q: (a[q], s[q], q)):
            temp = ops[q]["temp"]
            if fr_blocks and _fraction(fr_blocks[-1]["temperature"]) == _fraction(temp):
                fr_blocks[-1]["end_ticks"] = max(fr_blocks[-1]["end_ticks"], s[q])
                fr_blocks[-1]["operations"].append(q)
            else:
                fr_blocks.append({"furnace": fr, "temperature": temp,
                                  "start_ticks": a[q], "end_ticks": s[q], "operations": [q]})
        previous_temp = initial_temperature
        previous_end = 0
        for block in fr_blocks:
            try:
                duration = _ramp_ticks(scenario, previous_temp, block["temperature"])
            except (ValueError, ZeroDivisionError) as exc:
                fail("ramp_precision", str(exc), furnace=fr)
                duration = 0
            ready = previous_end + duration
            if ready > block["start_ticks"]:
                fail("ramp_gap", "furnace cannot reach required temperature before receiving material",
                     furnace=fr, available_ticks=block["start_ticks"] - previous_end,
                     required_ticks=duration, operations=block["operations"])
            if duration:
                ramps.append({"furnace": fr, "from_temperature": previous_temp,
                              "to_temperature": block["temperature"],
                              "start_ticks": previous_end, "end_ticks": ready})
            previous_end = block["end_ticks"]
            previous_temp = block["temperature"]
        blocks.extend(fr_blocks)

    makespan = max(ends, default=0)
    day_ticks = 24 * 60 * scale
    day_count = (horizon + day_ticks - 1) // day_ticks
    daily = []
    for day in range(day_count):
        day_start = day * day_ticks
        # Last partial horizon still uses the user's fixed 24-hour denominator.
        busy = sum(max(0, min(ends[q], day_start + day_ticks) - max(s[q], day_start))
                   for q in range(count))
        daily.append({"day": day, "busy_ticks": busy, "busy_minutes": busy / scale,
                      "utilization": busy / day_ticks})

    replay = {"complete": False, "semantics": "tick-lattice decisions; planned actions are events; exits before entries"}
    if not errors:
        replay = _replay(scenario, ops, a, s, f, ends, ramps, fail)
    work = sum(op["forge_ticks"] for op in ops)
    return {
        "valid": not errors, "errors": errors, "operations": count, "orders": len(order_ops),
        "ticks_per_minute": scale, "makespan_ticks": makespan, "makespan_minutes": makespan / scale,
        "forge_work_ticks": work, "forge_work_minutes": work / scale,
        "holding_ticks": sum(s[q] - a[q] for q in range(count)),
        "extra_holding_ticks": sum(s[q] - a[q] - ops[q]["hold_min_ticks"] for q in range(count)),
        "planning_horizon_utilization": work / horizon,
        "makespan_utilization": work / makespan if makespan > 0 else 0,
        "daily": daily, "max_furnace_occupancy": max_occupancy,
        "canonical_furnace_blocks": blocks, "canonical_ramps": ramps, "replay": replay,
        "audit_scope": "independent simplified model; no transport, door recovery, splitting, or wrapping",
    }


def _replay(scenario, ops, a, s, f, ends, ramps, fail):
    scale = scenario["ticks_per_minute"]
    step_cap = 5 * scale
    finish = max(ends, default=0)
    event_types: dict[int, set[str]] = defaultdict(set)
    event_types[0].add("start")
    for q, op in enumerate(ops):
        event_types[a[q]].add("planned_heat_start")
        event_types[s[q]].add("planned_forge_start")
        event_types[ends[q]].add("forge_end")
        for field, name in (("hold_min_ticks", "hold_min"), ("hold_max_ticks", "hold_max")):
            event_t = a[q] + op[field]
            if event_t <= s[q]:  # Events are canceled once the material leaves the furnace.
                event_types[event_t].add(name)
    for ramp in ramps:
        event_types[ramp["start_ticks"]].add("planned_ramp_start")
        event_types[ramp["end_ticks"]].add("ramp_ready")
    event_times = sorted(t for t in event_types if 0 <= t <= finish)
    clock = 0
    pointer = 0
    steps = 0
    max_step = 0
    points_checked = 0
    temperature_by_furnace = {f: _fraction(scenario["initial_temperature"])
                              for f in range(scenario["furnace_count"])}
    ramp_lists: dict[int, list[dict]] = defaultdict(list)
    for ramp in ramps:
        ramp_lists[ramp["furnace"]].append(ramp)

    def check_state(t):
        nonlocal points_checked
        points_checked += 1
        heating: dict[int, list[int]] = defaultdict(list)
        pressing = []
        for q, op in enumerate(ops):
            if a[q] <= t < s[q]:
                heating[f[q]].append(q)
                if t - a[q] > op["hold_max_ticks"]:
                    fail("replay_holding", "holding upper limit exceeded during replay", operation=q, time_ticks=t)
            if s[q] <= t < ends[q]:
                pressing.append(q)
        if len(pressing) > 1:
            fail("replay_press", "press overlap during replay", time_ticks=t)
        for fr in range(scenario["furnace_count"]):
            temp = temperature_by_furnace[fr]
            ramping = False
            for ramp in ramp_lists[fr]:
                if ramp["end_ticks"] <= t:
                    temp = _fraction(ramp["to_temperature"])
                elif ramp["start_ticks"] <= t < ramp["end_ticks"]:
                    ramping = True
                    break
                else:
                    break
            occupants = heating.get(fr, [])
            if len(occupants) > scenario["capacity"]:
                fail("replay_capacity", "capacity violation during replay", furnace=fr, time_ticks=t)
            if occupants and ramping:
                fail("replay_loaded_ramp", "furnace changes temperature while occupied", furnace=fr, time_ticks=t)
            if any(_fraction(ops[q]["temp"]) != temp for q in occupants) and not ramping:
                fail("replay_temperature", "holding at wrong temperature during replay", furnace=fr, time_ticks=t)

    check_state(clock)
    while clock < finish:
        while pointer < len(event_times) and event_times[pointer] <= clock:
            pointer += 1
        next_event = event_times[pointer] if pointer < len(event_times) else finish
        next_t = min(clock + step_cap, next_event, finish)
        if not clock < next_t <= clock + step_cap:
            fail("replay_clock", "invalid forward replay step", time_ticks=clock)
            break
        max_step = max(max_step, next_t - clock)
        clock = next_t
        steps += 1
        check_state(clock)
    return {"complete": clock == finish, "end_ticks": clock, "steps": steps,
            "points_checked": points_checked, "events": len(event_times),
            "step_cap_ticks": step_cap, "maximum_actual_step_ticks": max_step,
            "semantics": "tick-lattice decisions; planned actions and physical events shorten the 5-minute replay step; exits before entries"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scenario", type=Path)
    parser.add_argument("schedule", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--no-symmetry-check", action="store_true")
    args = parser.parse_args()
    report = audit(json.loads(args.scenario.read_text()), json.loads(args.schedule.read_text()),
                   check_symmetry=not args.no_symmetry_check)
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out:
        args.out.write_text(rendered + "\n")
    print(json.dumps({"valid": report["valid"], "operations": report.get("operations"),
                      "errors": report["errors"], "makespan_minutes": report.get("makespan_minutes")}, ensure_ascii=False))
    return 0 if report["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
