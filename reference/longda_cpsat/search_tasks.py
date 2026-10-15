"""Bounded full-model tasks shared by first search and incumbent improvement.

Business directions choose branch guidance (without an incumbent) or a union
of whole order routes to release (with an incumbent). They never reimplement
furnace/press/vehicle legality. Every candidate is independently audited.

Task shape: ``direction``, ``seed``, ``fraction`` in (0,1], optional explicit
``orders`` and ``id``. A task quantum is not a global search deadline. UNKNOWN
means unfinished; only an unrestricted INFEASIBLE result can prove global
infeasibility/no improvement. Restricted-task proofs remain local.
"""
from __future__ import annotations

from collections import defaultdict
import math
import random
import time

from ortools.sat.python import cp_model

from .audit import audit
from .model import build_model, extract_schedule, flatten, ramp_ticks
from .objectives import add_objective, evaluate_schedule, objective_key, validate_preference
from .solver_flow import solve_interruptibly, solver_statistics

DIRECTIONS = ("broad", "press_gap", "vehicle_link", "furnace_batch", "day_boundary")


def select_orders(scenario, schedule, direction="press_gap", fraction=.35, seed=0):
    """Select related *whole routes*, including future supply when available.

    Selection changes only the local neighborhood, not original feasibility.
    Seed rotates among good targets to avoid repeating one identical gap.
    """
    if direction not in DIRECTIONS:
        raise ValueError(f"Unknown direction {direction!r}")
    if not 0 < float(fraction) <= 1:
        raise ValueError("fraction must be in (0, 1]")
    ids = [row["id"] for row in scenario["orders"]]
    if not ids or direction == "broad" or fraction >= 1:
        return ids
    ops = flatten(scenario)
    n = len(ids)
    count = min(n, max(2 if n > 1 else 1, math.ceil(n * fraction)))
    D = 1440 * scenario["ticks_per_minute"]
    rng = random.Random(seed)
    ordered = sorted(ops, key=lambda q: (schedule["forge_start"][q["index"]], q["index"]))
    scores = {order: float("inf") for order in ids}
    target_orders = []
    target = D
    if direction == "press_gap":
        gaps, end, previous = [], 0, None
        for q in ordered:
            s = schedule["forge_start"][q["index"]]
            if s >= D:
                break
            if s > end:
                gaps.append((s - end, (s + end) / 2, previous, q["order"]))
            end, previous = s + q["duration"], q["order"]
        if end < D:
            gaps.append((D - end, (D + end) / 2, previous, None))
        if gaps:
            choices = sorted(gaps, reverse=True, key=lambda row: row[0])[:max(1, min(4, len(gaps)))]
            _, target, left, right = choices[rng.randrange(len(choices))]
            target_orders = [x for x in (left, right) if x is not None]
    elif direction == "vehicle_link":
        tasks = sorted(schedule.get("transport", []), key=lambda row: (row["start"], row.get("pickup_rank", 0)))
        links = []
        by_car = defaultdict(list)
        for row in tasks:
            by_car[row["vehicle"]].append(row)
        for rows in by_car.values():
            for a, b in zip(rows, rows[1:]):
                if b["start"] > D:
                    continue
                if a["destination"] != b["source"]:
                    links.append((b["start"] - a["end"], a, b))
        if links:
            choices = sorted(links, reverse=True, key=lambda row: row[0])[:4]
            _, a, b = choices[rng.randrange(len(choices))]
            target = (a["end"] + b["start"]) / 2
            target_orders = [ops[a["op"]]["order"], ops[b["op"]]["order"]]
    elif direction == "furnace_batch":
        # Prefer a temperature transition / heavily occupied furnace neighborhood.
        by_furnace = defaultdict(list)
        for q in ops:
            by_furnace[schedule["furnace"][q["index"]]].append(q)
        furnace = max(by_furnace, key=lambda f: len(by_furnace[f]))
        rows = sorted(by_furnace[furnace], key=lambda q: schedule["heat_start"][q["index"]])
        transitions = [(a, b) for a, b in zip(rows, rows[1:]) if a["temp"] != b["temp"]]
        if transitions:
            a, b = transitions[rng.randrange(len(transitions))]
            target = schedule["heat_start"][b["index"]]
            target_orders = [a["order"], b["order"]]
        elif rows:
            q = rows[rng.randrange(len(rows))]
            target = schedule["heat_start"][q["index"]]
            target_orders = [q["order"]]
    # Forge and heating proximity capture the physical blockers around a target.
    for q in ops:
        i = q["index"]
        proximity = min(abs(schedule["forge_start"][i] - target),
                        abs(schedule["heat_start"][i] - target))
        if direction == "day_boundary":
            proximity = abs(schedule["forge_start"][i] - D)
        scores[q["order"]] = min(scores[q["order"]], proximity)
    jitter = {order: rng.random() for order in ids}
    ranked = sorted(ids, key=lambda order: (scores[order], jitter[order]))
    selected = list(dict.fromkeys(target_orders))[:count]
    # At least one order with future operations can bring new work into day one.
    future = {q["order"] for q in ops if schedule["forge_start"][q["index"]] >= D}
    future_ranked = [order for order in ranked if order in future]
    if future_ranked and not any(order in future for order in selected):
        if len(selected) == count:
            selected.pop()
        selected.append(future_ranked[0])
    for order in ranked:
        if len(selected) >= count:
            break
        if order not in selected:
            selected.append(order)
    return selected


def _guided_order(ops, direction, seed):
    """Order branching priorities; no precedence relation is added."""
    rng = random.Random(seed)
    tie = {q["index"]: rng.random() for q in ops}
    if direction == "furnace_batch":
        key = lambda q: (q["temp"], q.get("fire", 0), q["due"], tie[q["index"]])
    elif direction == "vehicle_link":
        key = lambda q: (q.get("fire", 0), q["temp"], q["lower"], tie[q["index"]])
    elif direction == "day_boundary":
        key = lambda q: (q["lower"], q["due"], q.get("fire", 0), tie[q["index"]])
    else:
        key = lambda q: (q["due"] - q["release"] - q["lower"] - q["duration"],
                         q["upper"] - q["lower"], tie[q["index"]])
    return sorted(ops, key=key)


def _guidance(model, variables, ops, scenario, task, schedule, preference):
    """Baseline hints or optimistic route construction are repairable hints only."""
    direction = task["direction"]
    seed = int(task.get("seed", 0))
    if schedule is not None:
        for key in ("heat_start", "forge_start", "furnace"):
            for var, value in zip(variables[key], schedule[key]):
                model.add_hint(var, int(value))
    elif direction != "broad":
        # An optimistic route-by-route construction. Conflicts between routes
        # are deliberately left to CP-SAT; this is never published as a plan.
        from .model import ticks
        from .transport import transport_enabled
        tau = (ticks(scenario["transport"].get("travel_minutes", 5), scenario["ticks_per_minute"])
               if transport_enabled(scenario) else 0)
        previous = {}
        temperatures = sorted({q["temp"] for q in ops}, reverse=True)
        for q in ops:
            i = q["index"]
            a = max(previous.get(q["order"], q["release"]) + tau,
                    ramp_ticks(scenario, scenario["initial_temperature"], q["temp"]))
            s = a + q["lower"] + tau
            previous[q["order"]] = s + q["duration"]
            # Values outside original domains are skipped, not constrained.
            for key, value in (("heat_start", a), ("forge_start", s),
                               ("furnace", (temperatures.index(q["temp"]) + seed) % scenario["furnace_count"])):
                var = variables[key][i]
                domain = var.proto.domain
                if domain[0] <= value <= domain[-1]:
                    model.add_hint(var, int(value))
    if direction != "broad":
        order = _guided_order(ops, direction, seed)
        model.add_decision_strategy([variables["forge_start"][q["index"]] for q in order],
                                    cp_model.CHOOSE_FIRST, cp_model.SELECT_MIN_VALUE)
    if preference == "hold-near-min":
        # Existing baseline time hints may imply longer holds; do not install
        # contradictory hints for their derived holding variables.
        if schedule is None:
            for q in ops:
                model.add_hint(variables["heat_duration"][q["index"]], q["lower"])


def run_task(scenario, task, incumbent=None, preference="none", threads=1,
             budget_s=5, stop_event=None, log_path=None):
    """Run one bounded, independently audited first/improvement task.

    Returned global INFEASIBLE is distinguished by ``proof_kind``:
    original_infeasible (first), no_improvement_global (broad improvement), or
    no_improvement_local (restricted neighborhood). A first feasible return is
    not a proof of best score or best holding even when native status OPTIMAL.
    """
    begin = time.monotonic()
    task = dict(task)
    direction = task.setdefault("direction", "broad")
    base = {"task_id": task.get("id"), "direction": direction,
            "scope": "global", "phase": "first" if incumbent is None else "improve",
            "preference": preference, "model_build_s": 0., "solver_wall_s": 0.}
    def result(status, **extra):
        return dict(base, status=status, elapsed_s=time.monotonic() - begin, **extra)
    if budget_s <= 0 or (stop_event is not None and stop_event.is_set()):
        return result("unknown", cp_status="UNKNOWN", reason="budget or stop before build")
    try:
        validate_preference(preference)
        if direction not in DIRECTIONS:
            raise ValueError(f"Unknown direction {direction!r}")
        if isinstance(threads, bool) or int(threads) != threads or threads < 1:
            raise ValueError("threads must be a positive integer")
        schedule = None if incumbent is None else incumbent["schedule"]
        metric = None
        if schedule is not None:
            baseline_audit = audit(scenario, schedule)
            if not baseline_audit["valid"]:
                return result("invalid", reason="incumbent failed complete audit", audit=baseline_audit)
            metric = evaluate_schedule(scenario, schedule)
            base["launch_objective_key"] = list(objective_key(metric, preference))
        build_begin = time.monotonic()
        model, variables, ops = build_model(scenario)
        all_orders = [row["id"] for row in scenario["orders"]]
        selected = all_orders
        if schedule is not None and direction != "broad":
            selected = task.get("orders", task.get("selected_orders"))
            if selected is None:
                selected = select_orders(scenario, schedule, direction,
                                         float(task.get("fraction", .35)), int(task.get("seed", 0)))
            if not selected or len(set(selected)) != len(selected) or not set(selected) <= set(all_orders):
                raise ValueError("A neighborhood must select unique existing whole orders")
            for q in ops:
                if q["order"] not in selected:
                    for key in ("heat_start", "forge_start", "furnace"):
                        model.add(variables[key][q["index"]] == schedule[key][q["index"]])
            if len(selected) < len(all_orders):
                base["scope"] = "local"
        base["selected_orders"] = list(selected)
        base["fixed_orders"] = [order for order in all_orders if order not in selected]
        attached = add_objective(model, variables, ops, scenario, metric,
                                 preference=preference, first=incumbent is None)
        _guidance(model, variables, ops, scenario, task, schedule, preference)
        base["model_build_s"] = time.monotonic() - build_begin
        error = model.validate()
        if error:
            return result("invalid", cp_status="MODEL_INVALID", reason=error)
    except (KeyError, ValueError, TypeError, OverflowError) as exc:
        return result("invalid", cp_status="MODEL_INVALID", reason=str(exc))
    remaining = float(budget_s) - (time.monotonic() - begin)
    if remaining <= 0 or (stop_event is not None and stop_event.is_set()):
        return result("unknown", cp_status="UNKNOWN", reason="budget or stop after build")
    solver = cp_model.CpSolver()
    solver.parameters.num_search_workers = int(threads)
    solver.parameters.random_seed = int(task.get("seed", 0)) % (2**31 - 1)
    solver.parameters.symmetry_level = 0
    solver.parameters.max_time_in_seconds = remaining
    solver.parameters.stop_after_first_solution = True
    if direction != "broad":
        solver.parameters.search_branching = cp_model.PARTIAL_FIXED_SEARCH
    status = solve_interruptibly(solver, model, stop_event, log_path=log_path,
                                 log_label=f"shared:{task.get('id', direction)}")
    stats = solver_statistics(solver)
    base.update(cp_status=solver.status_name(status), solver_stats=stats,
                solver_wall_s=stats["solver_wall_s"])
    if status == cp_model.INFEASIBLE:
        proof = ("original_infeasible" if incumbent is None else
                 "no_improvement_global" if base["scope"] == "global" else "no_improvement_local")
        return result("infeasible", proof_kind=proof)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return result("invalid" if status == cp_model.MODEL_INVALID else "unknown")
    candidate = extract_schedule(solver, variables)
    checked = audit(scenario, candidate)
    candidate_metrics = evaluate_schedule(scenario, candidate)
    if not checked["valid"]:
        return result("invalid", schedule=candidate, audit=checked, reason="candidate failed complete audit")
    if metric is not None and objective_key(candidate_metrics, preference) <= objective_key(metric, preference):
        return result("invalid", schedule=candidate, audit=checked, reason="candidate failed strict objective improvement")
    for key, variable in (("score_ticks", attached["score"]), ("hold_excess_ticks", attached["hold_excess"])):
        if candidate_metrics[key] != solver.value(variable):
            return result("invalid", reason="independent objective mismatch", schedule=candidate, audit=checked)
    return result("feasible", schedule=candidate, audit=checked, **candidate_metrics)
