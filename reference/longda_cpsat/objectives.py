"""Shared exact scoring; holding preference never changes process feasibility.

Times and scores use the scenario's integer ticks.  First-day forging time is
primary.  With ``hold-near-min``, *equal* first-day scores are ordered by smaller
total excess effective holding over every prescribed operation of every order.
Transport is not holding, and all min/max constraints remain in the base model.
"""
from __future__ import annotations

from .model import flatten, ticks
from .transport import transport_enabled

PREFERENCES = ("none", "hold-near-min")


def validate_preference(preference):
    if preference not in PREFERENCES:
        raise ValueError(f"Unknown preference {preference!r}; choose {PREFERENCES}")


def evaluate_schedule(scenario, schedule):
    """Recompute physical metrics, not stored/candidate objective values.

    This does not replace the independent hard-constraint audit. In particular,
    ``heat_duration`` in an imported schedule is not trusted for scoring.
    """
    ops = flatten(scenario)
    for key in ("heat_start", "forge_start", "furnace"):
        if len(schedule.get(key, [])) != len(ops):
            raise ValueError(f"Schedule {key} length differs from operations")
    D = 1440 * scenario["ticks_per_minute"]
    tau = (ticks(scenario["transport"].get("travel_minutes", 5),
                 scenario["ticks_per_minute"]) if transport_enabled(scenario) else 0)
    score = 0
    excess = 0
    for q, a, s in zip(ops, schedule["heat_start"], schedule["forge_start"]):
        if type(a) is not int or type(s) is not int:
            raise ValueError("Scoring requires integer physical ticks")
        score += max(0, min(s + q["duration"], D) - max(s, 0))
        excess += s - a - tau - q["lower"]
    return {"score_ticks": int(score), "hold_excess_ticks": int(excess),
            "day_end_ticks": int(D)}


def objective_key(record, preference="none"):
    """Larger tuples are better; no day-one minute is traded for holding."""
    validate_preference(preference)
    score = int(record["score_ticks"])
    if preference == "hold-near-min":
        return score, -int(record["hold_excess_ticks"])
    return (score,)


def add_objective(model, variables, ops, scenario, incumbent=None,
                  preference="none", first=False):
    """Attach score expressions and an exact improvement cut to a full model.

    In first mode there is no baseline cut. A holding preference guides the
    first feasible solution through a min-X objective, but the caller stops at
    the first solution and must not claim X optimality. In improvement mode
    the cut excludes every non-improving schedule using the stated ordering.
    Integer lexicographic encoding is checked conservatively against int64.
    """
    validate_preference(preference)
    D = 1440 * scenario["ticks_per_minute"]
    contribution = []
    for q, s in zip(ops, variables["forge_start"]):
        clipped = model.new_int_var(0, D, f"shared_clip_{q['index']}")
        model.add_min_equality(clipped, [D, s])
        b = model.new_int_var(0, min(D, q["duration"]), f"shared_busy_{q['index']}")
        model.add_min_equality(b, [q["duration"], D - clipped])
        contribution.append(b)
    score_max = min(D, sum(q["duration"] for q in ops))
    score = model.new_int_var(0, score_max, "shared_day_score")
    model.add(score == sum(contribution))
    excess_max = sum(q["upper"] - q["lower"] for q in ops)
    excess = model.new_int_var(0, excess_max, "shared_hold_excess")
    model.add(excess == sum(h - q["lower"]
                           for h, q in zip(variables["heat_duration"], ops)))
    weight = excess_max + 1
    encoded = None
    if preference == "hold-near-min":
        # Keep ample headroom for CP-SAT's intermediate linear expressions.
        if weight * score_max + excess_max >= (1 << 62):
            raise ValueError("Lexicographic objective exceeds safe integer range")
        encoded = weight * score - excess
    if first:
        if preference == "hold-near-min":
            model.minimize(excess)
    else:
        if incumbent is None:
            raise ValueError("Improvement requires an audited complete incumbent")
        if preference == "none":
            model.add(score > int(incumbent["score_ticks"]))
            model.maximize(score)
        else:
            baseline = weight * int(incumbent["score_ticks"]) - int(incumbent["hold_excess_ticks"])
            model.add(encoded > baseline)
            model.maximize(encoded)
    return {"score": score, "hold_excess": excess, "lex_weight": weight,
            "day_end_ticks": D, "score_upper_bound": score_max,
            "hold_excess_upper_bound": excess_max}
