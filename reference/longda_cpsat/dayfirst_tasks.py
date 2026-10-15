"""Continuous CP-SAT kernels for first solution and day-prefix improvement.

Each function creates exactly one native solver and calls it once. There is no
attempt quantum, portfolio, restart loop or decision-hint construction here.
The optional deadline is an absolute ``time.monotonic()`` value and includes
model construction. First solution defaults to the original feasibility
model. Optional holding preference adds a holding objective, never a new
physical rule; improvement uses strict lexicographic (day work, -extra hold).

A prefix fixes heat/forge starts before the day boundary and chosen furnaces.
It does not fix all auxiliary vehicle assignments or future furnace states.
Those remain decisions of the independently audited full continuation.
"""
from __future__ import annotations

import math
import time

from ortools.sat.python import cp_model

from .audit import audit
from .model import build_model, extract_schedule, flatten
from .objectives import add_objective, evaluate_schedule, objective_key, validate_preference
from .prefix_model import (build_prefix_model, exclude_prefix, extract_prefix,
                           fix_prefix, project_schedule)
from .solver_flow import solve_interruptibly, solver_statistics


def _begin(phase, preference, threads, deadline, stop_event, on_stage):
    begin = time.monotonic()
    base = {"phase": phase, "preference": preference, "model_build_s": 0.,
            "solver_wall_s": 0., "optimal": False}

    def result(status, **extra):
        return dict(base, status=status, elapsed_s=time.monotonic() - begin, **extra)

    def stage(name, **extra):
        if on_stage is not None:
            on_stage(name, dict(base, **extra))

    def stopped():
        return ((stop_event is not None and stop_event.is_set()) or
                (deadline is not None and time.monotonic() >= deadline))

    validate_preference(preference)
    if isinstance(threads, bool) or not isinstance(threads, int) or threads < 1:
        raise ValueError("threads must be a positive integer")
    if deadline is not None and not math.isfinite(float(deadline)):
        raise ValueError("deadline must be a finite monotonic timestamp or None")
    return base, result, stage, stopped


def _native_solve(model, base, result, stage, stopped, threads, stop_event,
                  deadline, log_path):
    error = model.validate()
    if error:
        return None, None, result("invalid", cp_status="MODEL_INVALID", reason=error)
    if stopped():
        return None, None, result("unknown", cp_status="UNKNOWN", reason="deadline or user stop before solve")
    solver = cp_model.CpSolver()
    solver.parameters.num_search_workers = threads
    solver.parameters.symmetry_level = 0
    solver.parameters.stop_after_first_solution = True
    # Deliberately no max_time if omitted: this native search remains alive.
    if deadline is not None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None, None, result("unknown", cp_status="UNKNOWN", reason="deadline before solve")
        solver.parameters.max_time_in_seconds = remaining
    stage("solve")
    status = solve_interruptibly(solver, model, stop_event, log_path=log_path,
                                 log_label=f"dayfirst:{base['phase']}")
    stats = solver_statistics(solver)
    base.update(cp_status=solver.status_name(status), solver_stats=stats,
                solver_wall_s=stats["solver_wall_s"])
    if status == cp_model.MODEL_INVALID:
        return solver, status, result("invalid", reason="native model invalid")
    if status not in (cp_model.FEASIBLE, cp_model.OPTIMAL, cp_model.INFEASIBLE):
        return solver, status, result("unknown")
    return solver, status, None


def _baseline(scenario, best):
    """Never trust stored score fields or a previously asserted audit flag."""
    if not isinstance(best, dict) or not isinstance(best.get("schedule"), dict):
        raise ValueError("A complete incumbent schedule is required")
    checked = audit(scenario, best["schedule"])
    if not checked["valid"]:
        raise ValueError("Incumbent failed complete independent audit")
    return evaluate_schedule(scenario, best["schedule"])


def _checked_full(scenario, solver, variables, result, stage, preference,
                  baseline=None, expected_prefix=None, attached=None):
    stage("audit")
    schedule = extract_schedule(solver, variables)
    checked = audit(scenario, schedule)
    if not checked["valid"]:
        return result("invalid", schedule=schedule, audit=checked,
                      reason="candidate failed complete independent audit")
    metrics = evaluate_schedule(scenario, schedule)
    if baseline is not None and objective_key(metrics, preference) <= objective_key(baseline, preference):
        return result("invalid", schedule=schedule, audit=checked,
                      reason="candidate did not strictly improve the audited baseline")
    if expected_prefix is not None:
        projection = project_schedule(scenario, schedule, expected_prefix["day_end_ticks"])
        if any(projection[key] != expected_prefix[key] for key in
               ("heat_start", "forge_start", "furnace", "day_score_ticks")):
            return result("invalid", schedule=schedule, audit=checked,
                          reason="complete schedule changed the fixed prefix")
    if attached is not None:
        for metric, variable in (("score_ticks", "score"), ("hold_excess_ticks", "hold_excess")):
            if metrics[metric] != solver.value(attached[variable]):
                return result("invalid", schedule=schedule, audit=checked,
                              reason="independent objective calculation disagrees with solver")
    return result("feasible", schedule=schedule, audit=checked, **metrics)


def first(scenario, threads=48, stop_event=None, deadline=None, on_stage=None,
          log_path=None, preference="none"):
    """One uninterrupted solve, stopping at the first complete witness.

    ``none`` leaves the original model exactly unchanged. ``hold-near-min``
    explicitly adds a min-extra-holding objective but no route timing hints.
    Both stop at the first solution, so holding optimality is never claimed.
    """
    try:
        base, result, stage, stopped = _begin("first", preference, threads, deadline, stop_event, on_stage)
    except (ValueError, TypeError) as exc:
        return {"phase": "first", "status": "invalid", "reason": str(exc)}
    if stopped():
        return result("unknown", cp_status="UNKNOWN", reason="deadline or user stop before build")
    stage("build")
    begin = time.monotonic()
    try:
        model, variables, ops = build_model(scenario)
        attached = None
        base["first_preference_applied"] = preference == "hold-near-min"
        if base["first_preference_applied"]:
            attached = add_objective(model, variables, ops, scenario,
                                     preference=preference, first=True)
    except (ValueError, KeyError, TypeError, OverflowError) as exc:
        return result("invalid", cp_status="MODEL_INVALID", reason=str(exc))
    base["model_build_s"] = time.monotonic() - begin
    solver, status, answer = _native_solve(model, base, result, stage, stopped, threads,
                                           stop_event, deadline, log_path)
    if answer is not None:
        return answer
    if status == cp_model.INFEASIBLE:
        return result("infeasible", proof_kind="original_infeasible")
    return _checked_full(scenario, solver, variables, result, stage, preference, attached=attached)


def prefix(scenario, best, excluded=(), threads=48, stop_event=None, deadline=None,
           on_stage=None, log_path=None, preference="none"):
    """Find a better-day prefix (or equal-day prefix for holding tie-break).

    ``excluded`` must contain only prefixes whose improving continuations were
    proved impossible for an incumbent no better than the current incumbent.
    UNKNOWN is never an exclusion proof. This function does not add exclusions
    on its own. First-day finite-vehicle paths remain in the existing prefix
    model without altering any physical rule. If the entire planning horizon
    is shorter than one day, its complete model is the prefix model: all work
    lies inside that day and the only possible improvement may be holding.
    """
    try:
        base, result, stage, stopped = _begin("prefix", preference, threads, deadline, stop_event, on_stage)
    except (ValueError, TypeError) as exc:
        return {"phase": "prefix", "status": "invalid", "reason": str(exc)}
    if stopped():
        return result("unknown", cp_status="UNKNOWN", reason="deadline or user stop before build")
    stage("build")
    begin = time.monotonic()
    try:
        metrics = _baseline(scenario, best)
        D = 1440 * scenario["ticks_per_minute"]
        # Extra holding is nonnegative under the unchanged min/max rules.
        # At zero extra holding, an equal-day candidate cannot improve the
        # secondary objective, so retaining it would only waste suffix work.
        allow_tie = preference == "hold-near-min" and metrics["hold_excess_ticks"] > 0
        cutoff = metrics["score_ticks"] - (1 if allow_tie else 0)
        short_horizon = int(scenario["horizon_minutes"] * scenario["ticks_per_minute"]) < D
        attached = None
        if short_horizon:
            # There cannot be any post-day operation in this case. Keep the
            # 24-hour metric boundary while solving the original full model.
            # Adding its strict objective also proves equal-day/no-better-X
            # exhaustion without enumerating every unchanged full prefix.
            model, variables, ops = build_model(scenario)
            attached = add_objective(model, variables, ops, scenario, incumbent=metrics,
                                     preference=preference, first=False)
        else:
            model, variables, _ops = build_prefix_model(scenario, D, incumbent_ticks=cutoff)
            model.maximize(variables["day_total"])
        for old in excluded:
            if old["day_end_ticks"] != D:
                raise ValueError("Excluded prefix belongs to a different day boundary")
            exclude_prefix(model, variables, old)
    except (ValueError, KeyError, TypeError, OverflowError) as exc:
        return result("invalid", cp_status="MODEL_INVALID", reason=str(exc))
    base.update(model_build_s=time.monotonic() - begin,
                launch_objective_key=list(objective_key(metrics, preference)))
    solver, status, answer = _native_solve(model, base, result, stage, stopped, threads,
                                           stop_event, deadline, log_path)
    if answer is not None:
        return answer
    if status == cp_model.INFEASIBLE:
        return result("infeasible", proof_kind="no_improvement_prefix")
    if short_horizon:
        checked = _checked_full(scenario, solver, variables, result, stage, preference,
                                baseline=metrics, attached=attached)
        if checked["status"] != "feasible":
            return checked
        candidate = project_schedule(scenario, checked["schedule"], D)
    else:
        candidate = extract_prefix(solver, variables, D)
    return result("feasible", prefix=candidate, score_ticks=candidate["day_score_ticks"],
                  day_end_ticks=D)


def continuation(scenario, prefix, best, threads=48, stop_event=None, deadline=None,
                 on_stage=None, log_path=None, preference="none"):
    """Seek a strictly improving full continuation of exactly one prefix.

    With holding preference, equal first-day work may pass, but only if total
    extra holding strictly decreases. A proof here is local to this prefix;
    a time/user interruption returns UNKNOWN and must not create a nogood.
    """
    try:
        base, result, stage, stopped = _begin("continuation", preference, threads, deadline, stop_event, on_stage)
    except (ValueError, TypeError) as exc:
        return {"phase": "continuation", "status": "invalid", "reason": str(exc)}
    if stopped():
        return result("unknown", cp_status="UNKNOWN", reason="deadline or user stop before build")
    stage("build")
    begin = time.monotonic()
    try:
        metrics = _baseline(scenario, best)
        D = 1440 * scenario["ticks_per_minute"]
        if prefix["day_end_ticks"] != D:
            raise ValueError("Prefix must use the first 24-hour day boundary")
        ops = flatten(scenario)
        for key in ("heat_start", "forge_start", "furnace"):
            if len(prefix[key]) != len(ops) or any(type(x) is not int for x in prefix[key]):
                raise ValueError("Prefix requires exactly one integer value per operation")
        if any(not 0 <= s <= D for s in prefix["forge_start"]):
            raise ValueError("Prefix forge starts outside day range")
        score = sum(min(q["duration"], D-s) for q, s in zip(ops, prefix["forge_start"]))
        if score != prefix["day_score_ticks"]:
            raise ValueError("Prefix day score does not match its starts")
        allow_tie = preference == "hold-near-min" and metrics["hold_excess_ticks"] > 0
        if score < metrics["score_ticks"] or (score == metrics["score_ticks"] and not allow_tie):
            return result("filtered", reason="prefix cannot improve the day/holding objective")
        model, variables, ops = build_model(scenario)
        fix_prefix(model, variables, prefix, D)
        attached = add_objective(model, variables, ops, scenario, incumbent=metrics,
                                 preference=preference, first=False)
    except (ValueError, KeyError, TypeError, OverflowError) as exc:
        return result("invalid", cp_status="MODEL_INVALID", reason=str(exc))
    base.update(model_build_s=time.monotonic() - begin,
                launch_objective_key=list(objective_key(metrics, preference)))
    solver, status, answer = _native_solve(model, base, result, stage, stopped, threads,
                                           stop_event, deadline, log_path)
    if answer is not None:
        return answer
    if status == cp_model.INFEASIBLE:
        return result("infeasible", proof_kind="no_improvement_for_prefix")
    return _checked_full(scenario, solver, variables, result, stage, preference,
                         baseline=metrics, expected_prefix=prefix, attached=attached)
