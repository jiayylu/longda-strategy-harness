"""Heuristic progress stopping for continuous, audited day-first search.

The caller polls this class while one native CP-SAT solve keeps running.  An
observation window never restarts the solver.  Only the best independently
validated *complete* schedule may be supplied; a promising prefix is not a gain.
The returned reasons are resource/return-on-search decisions, never proofs.
"""

from __future__ import annotations

import math
from typing import Any


def _positive_integer(name: str, value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or int(value) != value or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return int(value)


def _nonnegative_number(name: str, value: float) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) or value < 0:
        raise ValueError(f"{name} must be finite and nonnegative")
    return float(value)


def _ticks(name: str, value: int) -> int:
    _nonnegative_number(name, value)
    if int(value) != value:
        raise ValueError(f"{name} must contain integer ticks")
    return int(value)


class DayfirstStop:
    """Stop after observed windows with no or too little verified improvement.

    ``tick(now, best_score, best_hold)`` uses a monotonic clock in seconds and
    objective values in integer scenario ticks.  There is deliberately no real
    clock dependency, so the coordinator and tests can inject time.  The first
    non-None score starts observation; searching for the first solution cannot
    trigger this stopper.  A None score on later calls simply polls the stored
    incumbent.

    A round closes after at least ``observation_seconds`` since its preceding
    observation.  Poll regularly (for example, once per second).  A delayed
    sample closes only ONE round: the class does not invent incumbent history
    for unobserved intervening boundaries.  Thus missed polling delays a
    heuristic stop rather than causing an unjustified early stop.

    ``small_gain_rounds`` rounds form one cumulative gain window.  Its anchor
    is not reset on incumbent improvement, so many small improvements count
    together.  If the primary score increases, only that positive primary gain
    is compared with its threshold; a simultaneous increase in holding cost
    cannot negate it.  At equal primary score, holding reduction is considered
    only when ``preference='hold-near-min'``.

    Resume a user-stopped run with a new instance and its audited baseline.
    ``snapshot`` is diagnostic data, not a native CP-SAT checkpoint.
    """

    def __init__(self, *, observation_seconds: float = 300.0,
                 no_gain_rounds: int = 3, small_gain_rounds: int = 3,
                 min_gain_minutes: float = 5.0,
                 min_hold_gain_minutes: float = 5.0,
                 scale: int = 3, preference: str = "none",
                 enabled: bool = True):
        self.observation_seconds = _nonnegative_number("observation_seconds", observation_seconds)
        if self.observation_seconds == 0:
            raise ValueError("observation_seconds must exceed zero")
        self.no_gain_rounds = _positive_integer("no_gain_rounds", no_gain_rounds)
        self.small_gain_rounds = _positive_integer("small_gain_rounds", small_gain_rounds)
        self.scale = _positive_integer("scale", scale)
        self.min_gain_minutes = _nonnegative_number("min_gain_minutes", min_gain_minutes)
        self.min_hold_gain_minutes = _nonnegative_number("min_hold_gain_minutes", min_hold_gain_minutes)
        if preference not in ("none", "hold-near-min"):
            raise ValueError("unknown preference")
        self.preference = preference
        self.enabled = bool(enabled)
        self.started = False
        self.reason: str | None = None
        self.started_at: float | None = None
        self.last_tick: float | None = None
        self.round_started_at: float | None = None
        self.best_score: int | None = None
        self.best_hold: int | None = None
        self.round_score: int | None = None
        self.round_hold: int | None = None
        self.anchor_score: int | None = None
        self.anchor_hold: int | None = None
        self.completed_rounds = 0
        self.window_rounds = 0
        self.no_gain_count = 0

    def _better(self, score: int, hold: int, previous_score: int, previous_hold: int) -> bool:
        return score > previous_score or (
            score == previous_score and self.preference == "hold-near-min" and hold < previous_hold)

    def tick(self, now: float, best_score: int | None = None,
             best_hold: int | None = None) -> str | None:
        """Observe an audited incumbent and return a sticky heuristic stop reason."""
        now = _nonnegative_number("now", now)
        if self.last_tick is not None and now < self.last_tick:
            raise ValueError("now must be monotonic")
        if best_score is None and best_hold is not None:
            raise ValueError("best_hold requires best_score")
        if best_score is not None:
            best_score = _ticks("best_score", best_score)
            if best_hold is None and self.preference == "hold-near-min":
                raise ValueError("hold-near-min requires an audited holding value")
            best_hold = _ticks("best_hold", 0 if best_hold is None else best_hold)
        self.last_tick = now
        if self.reason is not None:
            return self.reason
        if not self.started:
            if best_score is None:
                return None
            self.started = True
            self.started_at = self.round_started_at = now
            self.best_score = self.round_score = self.anchor_score = best_score
            self.best_hold = self.round_hold = self.anchor_hold = best_hold
            return None

        assert self.best_score is not None and self.best_hold is not None
        assert self.round_started_at is not None
        if best_score is not None and self._better(best_score, best_hold, self.best_score, self.best_hold):
            self.best_score, self.best_hold = best_score, best_hold
        # Ignore stale/non-improving candidates rather than degrading the anchor.
        if now - self.round_started_at < self.observation_seconds:
            return None

        assert self.round_score is not None and self.round_hold is not None
        assert self.anchor_score is not None and self.anchor_hold is not None
        self.no_gain_count = 0 if self._better(
            self.best_score, self.best_hold, self.round_score, self.round_hold) else self.no_gain_count + 1
        self.completed_rounds += 1
        self.window_rounds += 1
        self.round_started_at = now
        self.round_score, self.round_hold = self.best_score, self.best_hold

        if self.enabled and self.no_gain_count >= self.no_gain_rounds:
            self.reason = "stagnation_no_gain"
            return self.reason
        if self.window_rounds >= self.small_gain_rounds:
            primary_gain = self.best_score - self.anchor_score
            if primary_gain > 0 or self.preference == "none":
                enough_gain = primary_gain / self.scale >= self.min_gain_minutes
            else:
                enough_gain = (self.anchor_hold - self.best_hold) / self.scale >= self.min_hold_gain_minutes
            if self.enabled and not enough_gain:
                self.reason = "stagnation_small_gain"
                return self.reason
            self.anchor_score, self.anchor_hold = self.best_score, self.best_hold
            self.window_rounds = 0
        return None

    def snapshot(self) -> dict[str, Any]:
        """Return JSON-safe configuration and observation state; never a proof."""
        return {
            "enabled": self.enabled, "started": self.started,
            "reason": self.reason, "is_proof": False,
            "observation_seconds": self.observation_seconds,
            "no_gain_rounds": self.no_gain_rounds,
            "small_gain_rounds": self.small_gain_rounds,
            "min_gain_minutes": self.min_gain_minutes,
            "min_hold_gain_minutes": self.min_hold_gain_minutes,
            "scale": self.scale, "preference": self.preference,
            "started_at": self.started_at, "last_tick": self.last_tick,
            "round_started_at": self.round_started_at,
            "next_observation_at": None if self.round_started_at is None else self.round_started_at + self.observation_seconds,
            "completed_rounds": self.completed_rounds,
            "window_rounds": self.window_rounds,
            "no_gain_count": self.no_gain_count,
            "best_score_ticks": self.best_score,
            "best_hold_ticks": self.best_hold,
            "anchor_score_ticks": self.anchor_score,
            "anchor_hold_ticks": self.anchor_hold,
            "cumulative_primary_gain_minutes": None if not self.started else (self.best_score - self.anchor_score) / self.scale,
            "cumulative_hold_gain_minutes": None if not self.started or self.best_score != self.anchor_score else (self.anchor_hold - self.best_hold) / self.scale,
        }
