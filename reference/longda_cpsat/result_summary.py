"""Human-facing metrics from a complete, independently audited schedule.

Reporting only: this module never constructs or solves a scheduling model.
Days are consecutive 24-hour windows from the fixed planning origin (time 0).
The last occupied day keeps the full 24-hour denominator, even if delivery
finishes partway through it. This differs from dividing work by the makespan.
"""
from __future__ import annotations

from fractions import Fraction


def _integer(value, name: str, *, positive: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    if value < (1 if positive else 0):
        raise ValueError(f"{name} must be {'positive' if positive else 'nonnegative'}")
    return value


def build_summary(scenario: dict, audit: dict) -> dict | None:
    """Summarize an accepted audit; return None for an unaudited/invalid plan.

    This is not an audit replacement. The caller must independently audit the
    schedule first. Checks here reject incomplete or inconsistent metric data
    rather than silently manufacturing zero-activity days.
    """
    if audit.get("valid") is not True:
        return None
    scale = _integer(audit["ticks_per_minute"], "ticks_per_minute", positive=True)
    if scenario["ticks_per_minute"] != scale:
        raise ValueError("scenario and audit tick scales differ")
    horizon_value = Fraction(str(scenario["horizon_minutes"])) * scale
    if horizon_value.denominator != 1 or horizon_value <= 0:
        raise ValueError("horizon must be positive and representable in ticks")
    horizon = int(horizon_value)
    finish = _integer(audit["makespan_ticks"], "makespan_ticks")
    work = _integer(audit["forge_work_ticks"], "forge_work_ticks")
    if finish > horizon:
        raise ValueError("completion exceeds the configured horizon")
    day_ticks = 1440 * scale
    occupied_days = (finish + day_ticks - 1) // day_ticks
    horizon_days = (horizon + day_ticks - 1) // day_ticks

    daily_by_index = {}
    for row in audit["daily"]:
        day = _integer(row["day"], "daily.day")
        busy = _integer(row["busy_ticks"], "daily.busy_ticks")
        if day in daily_by_index or day >= horizon_days:
            raise ValueError("duplicate or out-of-range audit day")
        available = max(0, min(day_ticks, finish - day * day_ticks))
        if busy > available:
            raise ValueError("daily forging exceeds the available elapsed time")
        daily_by_index[day] = busy
    if set(daily_by_index) != set(range(horizon_days)):
        raise ValueError("audit does not cover every configured day")
    if sum(daily_by_index.values()) != work:
        raise ValueError("daily forging does not sum to total forging work")

    daily = [
        {"day": day + 1, "start_minutes": 1440 * day,
         "end_minutes": 1440 * (day + 1), "busy_ticks": daily_by_index[day],
         "busy_minutes": daily_by_index[day] / scale,
         "busy_hours": daily_by_index[day] / (60 * scale),
         "utilization_percent": 100 * daily_by_index[day] / day_ticks}
        for day in range(occupied_days)
    ]
    whole_days, day_remainder = divmod(finish, day_ticks)
    whole_hours, minute_remainder = divmod(day_remainder, 60 * scale)
    has_transport = bool((scenario.get("transport") or {}).get("enabled", False))
    return {
        "orders": audit["orders"], "operations": audit["operations"],
        "ticks_per_minute": scale,
        "completion_ticks": finish,
        "completion_elapsed_minutes": finish / scale,
        "completion_elapsed_hours": finish / (60 * scale),
        "completion_elapsed_days": finish / day_ticks,
        "completion_calendar_days": occupied_days,
        "completion_duration": {"days": whole_days, "hours": whole_hours,
                                "minutes": minute_remainder / scale},
        "completion_definition": "final_delivery" if has_transport else "final_forging_end",
        "total_forging_ticks": work, "total_forging_minutes": work / scale,
        "total_forging_hours": work / (60 * scale),
        "daily": daily,
        "average_daily_busy_minutes": work / (scale * occupied_days) if occupied_days else 0.0,
        "average_daily_utilization_percent": 100 * work / (day_ticks * occupied_days) if occupied_days else 0.0,
        "average_daily_denominator_minutes": 1440 * occupied_days,
        "average_daily_scope": "fixed origin through final completion; full 24 hours per occupied day",
        "configured_horizon_minutes": horizon / scale,
        "configured_horizon_days": horizon / day_ticks,
        "configured_horizon_utilization_percent": 100 * work / horizon,
    }


def _number(value: float) -> str:
    return f"{value:.3f}".rstrip("0").rstrip(".")


def format_summary(summary: dict) -> str:
    """Return a concise Chinese terminal table; callers choose output stream."""
    duration = summary["completion_duration"]
    completion_label = ("含最后一支送达成品区" if summary["completion_definition"] == "final_delivery"
                        else "最后一支锻打结束")
    count = summary["completion_calendar_days"]
    lines = [
        "\n排程结果汇总（完整方案已通过独立校验）",
        f"订单：{summary['orders']} 个；锻打工序：{summary['operations']} 道。",
        f"全部完成：{duration['days']} 天 {duration['hours']} 小时 {_number(duration['minutes'])} 分钟"
        f"（{summary['completion_elapsed_days']:.3f} 天；{completion_label}）。",
        f"覆盖 {count} 个统计日；每个统计日从计划起点起连续计算 24 小时。",
        "日期       锻打时间（分钟）   锻压机使用率",
    ]
    for row in summary["daily"]:
        lines.append(f"第 {row['day']:>2} 天     {_number(row['busy_minutes']):>12}       {row['utilization_percent']:>6.2f}%")
    lines.extend([
        f"合计锻打：{_number(summary['total_forging_minutes'])} 分钟"
        f"（{_number(summary['total_forging_hours'])} 小时）。",
        f"上述 {count} 天平均：每天 {_number(summary['average_daily_busy_minutes'])} 分钟，"
        f"使用率 {summary['average_daily_utilization_percent']:.2f}%。",
        "平均口径：最后不足一天仍按 24 小时计算，不是用总锻打时间除以实际完工历时。",
    ])
    if summary["configured_horizon_minutes"] != summary["average_daily_denominator_minutes"]:
        lines.append(
            f"若按配置的完整 {_number(summary['configured_horizon_days'])} 天窗口统计："
            f"平均使用率 {summary['configured_horizon_utilization_percent']:.2f}%"
            "（按配置窗口长度作分母，与上述完整统计日口径不同）。"
        )
    return "\n".join(lines)
