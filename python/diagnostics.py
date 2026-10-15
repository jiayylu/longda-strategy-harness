"""Observable schedule facts for planning; never infer a failure's root cause."""

from collections import defaultdict


def summarize(sc, schedule, ops):
    scale = sc["ticks_per_minute"]
    day = 1440 * scale
    forging = sorted(
        (
            schedule["forge_start"][q["index"]],
            schedule["forge_end"][q["index"]],
            q["order"],
            q["fire"],
        )
        for q in ops
    )
    gaps = []
    cursor = 0
    previous = None
    route_minutes = defaultdict(float)
    for start, end, order, fire in forging:
        if start >= day:
            continue
        if start > cursor:
            gaps.append(
                dict(
                    start_minutes=cursor / scale,
                    end_minutes=min(start, day) / scale,
                    minutes=(min(start, day) - cursor) / scale,
                    before_order=previous,
                    after_order=order,
                )
            )
        cursor = max(cursor, min(end, day))
        previous = order
    if cursor < day:
        gaps.append(
            dict(
                start_minutes=cursor / scale,
                end_minutes=1440,
                minutes=(day - cursor) / scale,
                before_order=previous,
                after_order=None,
            )
        )
    for q in ops:
        i = q["index"]
        route_minutes[q["route"]] += (
            max(
                0,
                min(schedule["forge_end"][i], day) - max(schedule["forge_start"][i], 0),
            )
            / scale
        )
    near = [
        dict(
            order=order,
            fire=fire,
            start_minutes=start / scale,
            duration_minutes=(end - start) / scale,
        )
        for start, end, order, fire in forging
        if day <= start < day + 360 * scale
    ][:12]
    excess = sorted(
        [
            dict(
                order=q["order"],
                fire=q["fire"],
                minutes=(
                    schedule["heat_end"][q["index"]]
                    - schedule["heat_start"][q["index"]]
                    - q["lower"]
                )
                / scale,
            )
            for q in ops
        ],
        key=lambda x: x["minutes"],
        reverse=True,
    )[:8]
    return dict(
        day1_by_route=dict(route_minutes),
        largest_press_gaps=sorted(gaps, key=lambda x: x["minutes"], reverse=True)[:8],
        early_day2_operations=near,
        largest_hold_excess=excess,
        interpretation="Measured facts only; gaps do not identify a unique cause.",
    )
