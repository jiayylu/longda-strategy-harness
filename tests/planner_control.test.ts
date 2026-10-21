import { test } from "node:test";
import assert from "node:assert/strict";
import {
  mayLaunchTrial,
  mayPublishTarget,
  plannerWaitSeconds,
  solverGuidanceKey,
} from "../src/planner_control.js";
test("longer API allowance never silently overrides the total deadline", () => {
  assert.equal(plannerWaitSeconds(900, 1800), 900);
  assert.equal(plannerWaitSeconds(900, 600), 600);
  assert.equal(plannerWaitSeconds(900, -1), 0);
});
test("required planner must actually be evaluated before generic target ends the run", () => {
  assert.equal(mayPublishTarget(true, 0), false);
  assert.equal(mayPublishTarget(true, 1), true);
  assert.equal(mayPublishTarget(false, 0), true);
});
test("fast generic trials leave room for a late model strategy without exceeding total quota", () => {
  // Three completed plus one running of five trials: fifth is reserved for model.
  assert.equal(mayLaunchTrial(3, 1, 5, true, 0, false), false);
  assert.equal(mayLaunchTrial(3, 1, 5, true, 0, true), true);
  assert.equal(mayLaunchTrial(3, 2, 5, true, 0, true), false);
  assert.equal(mayLaunchTrial(3, 1, 5, true, 1, false), true);
  assert.equal(mayLaunchTrial(0, 0, 1, true, 0, false), false);
  assert.equal(mayLaunchTrial(0, 0, 1, true, 0, true), true);
});

test("new solver neighborhood is distinct even when the constructor is identical", () => {
  const a = {
    direction: "vehicle_link",
    fraction: 0.4,
    focus_orders: [],
    seeds: [0],
  };
  const old = solverGuidanceKey(a, "baseline", "hint");
  assert.notEqual(
    old,
    solverGuidanceKey({ ...a, fraction: 0.5 }, "baseline", "hint"),
  );
  assert.notEqual(
    old,
    solverGuidanceKey({ ...a, direction: "press_gap" }, "baseline", "hint"),
  );
  assert.notEqual(
    old,
    solverGuidanceKey({ ...a, focus_orders: ["A"] }, "baseline", "hint"),
  );
  assert.notEqual(old, solverGuidanceKey(a, "improved-baseline", "hint"));
  assert.equal(
    solverGuidanceKey({ ...a, focus_orders: ["A", "B"] }),
    solverGuidanceKey({ ...a, focus_orders: ["B", "A"] }),
  );
});

test("unlimited trials continue beyond the former 160 cap even while waiting for planner", () => {
  for (const completed of [159, 160, 1000000]) {
    assert.equal(mayLaunchTrial(completed, 6, Infinity, true, 0, false), true);
    assert.equal(mayLaunchTrial(completed, 6, Infinity, true, 0, true), true);
    assert.equal(mayLaunchTrial(completed, 6, Infinity, false, 1, false), true);
  }
});
