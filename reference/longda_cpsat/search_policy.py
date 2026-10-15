"""Shared, model-independent allocation and stopping for bounded search trials.

This module never decides schedule feasibility.  The coordinator supplies rewards
only for independently audited complete schedules, accounts for duplicate tasks,
and publishes an incumbent only against the latest shared incumbent.  First-solution
and improvement phases must use separate policy instances: feasibility success is
the first phase's reward, whereas verified objective gain rewards improvement.

Stopping here means recent search returns were insufficient, never a proof of
infeasibility or optimality.  Interrupted/UNKNOWN solver trials may be accounted
for as attempts but must not be represented as infeasibility certificates.
"""

from __future__ import annotations

from collections import deque
import math
import random
from typing import Any, Iterable, Mapping


def _directions(values: Iterable[str]) -> tuple[str, ...]:
    result = tuple(values)
    if not result or len(set(result)) != len(result):
        raise ValueError("directions must be nonempty and unique")
    if any(not isinstance(value, str) or not value for value in result):
        raise ValueError("directions must contain nonempty strings")
    return result


def _tuple_tree(value: Any) -> Any:
    """Restore random.Random's nested tuple state after JSON round-tripping."""
    return tuple(_tuple_tree(item) for item in value) if isinstance(value, (list, tuple)) else value


class AdaptivePolicy:
    """Allocate trials using recent audited reward per wall-clock second.

    During warmup, each available direction receives one launch before a repeated
    launch.  Thereafter each receives an exploration floor of ``exploration / m``;
    the rest follows discounted reward divided by discounted cost.  Both failed
    and successful attempts contribute cost.  A direction is never permanently
    excluded simply because its recent trials did not improve the incumbent.

    ``choose`` records a launch, so simultaneous workers do not all select the
    same untried direction.  Call it only when a trial can actually be launched.
    Persisted statistics describe attempts, not resumable CP-SAT search trees.
    """

    def __init__(self, directions: Iterable[str], seed: int = 0,
                 exploration: float = 0.2, window: int = 20):
        self.directions = _directions(directions)
        if not math.isfinite(exploration) or not 0 <= exploration <= 1:
            raise ValueError("exploration must be between zero and one")
        if int(window) != window or window < 1:
            raise ValueError("window must be a positive integer")
        self.exploration = float(exploration)
        self.window = int(window)
        self.seed = int(seed)
        self._rng = random.Random(seed)
        self.launches = {direction: 0 for direction in self.directions}
        self.observations = {direction: 0 for direction in self.directions}
        self.history = {direction: deque(maxlen=self.window) for direction in self.directions}

    def _available(self, excluded: Iterable[str]) -> tuple[str, ...]:
        excluded = set(excluded)
        unknown = excluded.difference(self.directions)
        if unknown:
            raise ValueError(f"unknown excluded directions: {sorted(unknown)}")
        available = tuple(direction for direction in self.directions if direction not in excluded)
        if not available:
            raise ValueError("no available search direction")
        return available

    def rates(self) -> dict[str, float]:
        result = {}
        for direction in self.directions:
            # Half-life is half the retained observation window.  This discounts
            # both reward and cost, rather than erasing unsuccessful trial cost.
            samples = list(self.history[direction])
            decay = math.exp(math.log(0.5) / max(1.0, self.window / 2))
            weighted_reward = weighted_cost = 0.0
            for age, sample in enumerate(reversed(samples)):
                weight = decay ** age
                weighted_reward += weight * sample["reward"]
                weighted_cost += weight * sample["cost"]
            result[direction] = weighted_reward / weighted_cost if weighted_cost else 0.0
        return result

    def probabilities(self, excluded: Iterable[str] = ()) -> dict[str, float]:
        """Return allocation probabilities, including deterministic warmup."""
        available = self._available(excluded)
        probabilities = {direction: 0.0 for direction in self.directions}
        untried = [direction for direction in available if self.launches[direction] == 0]
        if untried:
            probabilities[untried[0]] = 1.0
            return probabilities
        rates = self.rates()
        total = sum(rates[direction] for direction in available)
        count = len(available)
        for direction in available:
            exploitation = rates[direction] / total if total else 1.0 / count
            probabilities[direction] = self.exploration / count + (1 - self.exploration) * exploitation
        return probabilities

    def choose(self, excluded: Iterable[str] = ()) -> str:
        probabilities = self.probabilities(excluded)
        roll = self._rng.random()
        cumulative = 0.0
        positive = [direction for direction in self.directions if probabilities[direction] > 0]
        for direction in positive:
            cumulative += probabilities[direction]
            if roll < cumulative:
                break
        else:
            direction = positive[-1]  # Protect against final floating-point rounding.
        self.launches[direction] += 1
        return direction

    def observe(self, direction: str, reward: float, cost: float,
                generation: int | None = None, status: str | None = None) -> None:
        if direction not in self.history:
            raise ValueError(f"unknown direction: {direction}")
        if not math.isfinite(reward) or reward < 0:
            raise ValueError("reward must be finite and nonnegative")
        if not math.isfinite(cost) or cost <= 0:
            raise ValueError("cost must be finite and strictly positive")
        self.history[direction].append({
            "reward": float(reward), "cost": float(cost),
            "generation": generation, "status": status,
        })
        self.observations[direction] += 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": 1, "directions": list(self.directions), "seed": self.seed,
            "exploration": self.exploration, "window": self.window,
            "launches": dict(self.launches), "observations": dict(self.observations),
            "history": {direction: list(samples) for direction, samples in self.history.items()},
            "random_state": self._rng.getstate(),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AdaptivePolicy":
        result = cls(data["directions"], data.get("seed", 0), data.get("exploration", 0.2), data.get("window", 20))
        for direction in result.directions:
            result.launches[direction] = int(data.get("launches", {}).get(direction, 0))
            result.observations[direction] = int(data.get("observations", {}).get(direction, 0))
            for sample in data.get("history", {}).get(direction, []):
                reward, cost = sample["reward"], sample["cost"]
                if not math.isfinite(reward) or reward < 0 or not math.isfinite(cost) or cost <= 0:
                    raise ValueError("invalid policy history")
                result.history[direction].append(dict(sample))
        if "random_state" in data:
            result._rng.setstate(_tuple_tree(data["random_state"]))
        return result


class NeighborhoodSizer:
    """Grow a direction's whole-route neighborhood after consecutive misses."""

    def __init__(self, initial_fraction: float = 0.2, min_fraction: float = 0.1,
                 max_fraction: float = 1.0, misses_before_growth: int = 2,
                 growth_factor: float = 1.6):
        if not 0 < min_fraction <= initial_fraction <= max_fraction <= 1:
            raise ValueError("fractions must satisfy 0 < min <= initial <= max <= 1")
        if int(misses_before_growth) != misses_before_growth or misses_before_growth < 1:
            raise ValueError("misses_before_growth must be a positive integer")
        if not math.isfinite(growth_factor) or growth_factor <= 1:
            raise ValueError("growth_factor must exceed one")
        self.initial_fraction = float(initial_fraction)
        self.min_fraction = float(min_fraction)
        self.max_fraction = float(max_fraction)
        self.misses_before_growth = int(misses_before_growth)
        self.growth_factor = float(growth_factor)
        self.current_fraction = self.initial_fraction
        self.consecutive_misses = 0

    def next_count(self, total: int) -> int:
        if int(total) != total or total < 0:
            raise ValueError("total must be a nonnegative integer")
        return min(total, max(1, math.ceil(total * self.current_fraction))) if total else 0

    def observe(self, success: bool) -> float:
        if success:
            self.current_fraction = self.initial_fraction
            self.consecutive_misses = 0
        else:
            self.consecutive_misses += 1
            if self.consecutive_misses >= self.misses_before_growth:
                self.current_fraction = min(self.max_fraction, self.current_fraction * self.growth_factor)
                self.consecutive_misses = 0
        return self.current_fraction

    def to_dict(self) -> dict[str, Any]:
        return dict(vars(self))

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "NeighborhoodSizer":
        result = cls(**{key: data[key] for key in (
            "initial_fraction", "min_fraction", "max_fraction", "misses_before_growth", "growth_factor")})
        result.current_fraction = float(data.get("current_fraction", result.initial_fraction))
        if not result.min_fraction <= result.current_fraction <= result.max_fraction:
            raise ValueError("restored fraction outside limits")
        result.consecutive_misses = int(data.get("consecutive_misses", 0))
        return result


class PlateauStop:
    """Stop after sufficient direction coverage and insufficient cumulative gain.

    Each round requires at least one completed trial from every direction.  A
    window consists of ``small_gain_rounds`` such rounds.  Improvements accumulate
    relative to the window's original anchor; an incumbent update does not reset
    the anchor, which preserves a sequence of many individually small gains.

    ``generation`` supplied to observe is the trial's *launch* generation.  A
    trial older than this window's anchor cannot count toward coverage or stop
    it.  The anchor advances only when a whole window has completed without
    stopping, not after each incumbent update.  Therefore a coordinator should
    not call start again on every improvement.  The coordinator must separately
    prevent stale results from overwriting the latest shared incumbent.

    A restricted trial's INFEASIBLE status says nothing about global optimality;
    this class uses only measured gains and never inspects solver proof statuses.
    """

    def __init__(self, directions: Iterable[str], no_gain_rounds: int = 3,
                 small_gain_rounds: int = 3, min_gain_ticks: int = 1,
                 hold_min_gain_ticks: int = 1, preference: str = "none",
                 enabled: bool = True):
        self.directions = _directions(directions)
        for name, value in (("no_gain_rounds", no_gain_rounds), ("small_gain_rounds", small_gain_rounds),
                            ("min_gain_ticks", min_gain_ticks), ("hold_min_gain_ticks", hold_min_gain_ticks)):
            if int(value) != value or value < 0:
                raise ValueError(f"{name} must be a nonnegative integer")
        if preference not in ("none", "hold-near-min"):
            raise ValueError("unknown preference")
        self.no_gain_rounds = int(no_gain_rounds)
        self.small_gain_rounds = int(small_gain_rounds)
        self.min_gain_ticks = int(min_gain_ticks)
        self.hold_min_gain_ticks = int(hold_min_gain_ticks)
        self.preference = preference
        self.enabled = bool(enabled)
        self.started = False
        self.reason = None

    def start(self, anchor_score: int, anchor_hold: int, generation: int = 0) -> None:
        self.anchor_score = self.best_score = self.round_score = int(anchor_score)
        self.anchor_hold = self.best_hold = self.round_hold = int(anchor_hold)
        self.anchor_generation = self.latest_generation = int(generation)
        self.coverage: set[str] = set()
        self.window_rounds = self.completed_rounds = self.no_gain_count = 0
        self.stale_trials = 0
        self.started = True
        self.reason = None

    def _better(self, score: int, hold: int, other_score: int, other_hold: int) -> bool:
        return score > other_score or (score == other_score and self.preference == "hold-near-min" and hold < other_hold)

    def observe(self, direction: str, score: int, hold: int, generation: int,
                improved: bool = False) -> str | None:
        """Account for a completed eligible trial and return a stop reason.

        ``score`` and ``hold`` are the latest audited incumbent's metrics.
        ``improved`` is accepted for coordinator convenience; numerical incumbent
        comparison, rather than that flag, determines cumulative improvement.
        """
        if not self.started:
            raise RuntimeError("call start before observe")
        if direction not in self.directions:
            raise ValueError(f"unknown direction: {direction}")
        if self.reason:
            return self.reason
        if generation < self.anchor_generation:
            self.stale_trials += 1
            return None
        self.latest_generation = max(self.latest_generation, int(generation))
        if self._better(score, hold, self.best_score, self.best_hold):
            self.best_score, self.best_hold = int(score), int(hold)
        if not self.enabled:
            return None
        self.coverage.add(direction)
        if self.coverage != set(self.directions):
            return None
        self.coverage.clear()
        self.completed_rounds += 1
        self.window_rounds += 1
        gained = self._better(self.best_score, self.best_hold, self.round_score, self.round_hold)
        self.no_gain_count = 0 if gained else self.no_gain_count + 1
        self.round_score, self.round_hold = self.best_score, self.best_hold
        if self.no_gain_rounds and self.no_gain_count >= self.no_gain_rounds:
            self.reason = "stagnation_no_gain"
            return self.reason
        if self.small_gain_rounds and self.window_rounds >= self.small_gain_rounds:
            primary_gain = self.best_score - self.anchor_score
            secondary_gain = self.anchor_hold - self.best_hold
            enough_primary = self.min_gain_ticks > 0 and primary_gain >= self.min_gain_ticks
            enough_secondary = (primary_gain == 0 and self.preference == "hold-near-min"
                                and self.hold_min_gain_ticks > 0 and secondary_gain >= self.hold_min_gain_ticks)
            primary_active = self.min_gain_ticks > 0
            secondary_active = self.preference == "hold-near-min" and self.hold_min_gain_ticks > 0
            if (primary_active or secondary_active) and not (enough_primary or enough_secondary):
                self.reason = "stagnation_small_gain"
                return self.reason
            self.anchor_score, self.anchor_hold = self.best_score, self.best_hold
            self.anchor_generation = self.latest_generation
            self.window_rounds = 0
        return None

    def to_dict(self) -> dict[str, Any]:
        result = dict(vars(self))
        result["version"] = 1
        result["directions"] = list(self.directions)
        if self.started:
            result["coverage"] = sorted(self.coverage)
        return result

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "PlateauStop":
        result = cls(**{key: data[key] for key in (
            "directions", "no_gain_rounds", "small_gain_rounds", "min_gain_ticks",
            "hold_min_gain_ticks", "preference", "enabled")})
        if data.get("started"):
            result.start(data["anchor_score"], data["anchor_hold"], data["anchor_generation"])
            for key in ("best_score", "best_hold", "round_score", "round_hold", "latest_generation",
                        "window_rounds", "completed_rounds", "no_gain_count", "stale_trials", "reason"):
                if key in data:
                    setattr(result, key, data[key])
            result.coverage = set(data.get("coverage", []))
            if not result.coverage.issubset(result.directions):
                raise ValueError("unknown direction in saved coverage")
        return result
