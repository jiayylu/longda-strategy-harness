import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { bootstrap } from "../src/learning.js";
import { StrategyCycles, type StrategyTrial } from "../src/strategy_cycles.js";
const strategy = bootstrap(
  JSON.parse(readFileSync("configs/orders20.json", "utf8")),
)[0];
const trial = (
  id: string,
  action: "construct" | "solve" = "solve",
  overrides: Partial<StrategyTrial> = {},
): StrategyTrial => ({
  id,
  action,
  status: "UNKNOWN",
  valid: false,
  audit_valid: false,
  baseline_before_minutes: 403,
  day_minutes: null,
  gain_minutes: 0,
  wall_s: 30,
  cpu_slot_seconds: 240,
  direction: "vehicle_link",
  fraction: 0.5,
  seed: 1,
  allowance_s: 30,
  proof_scope: "none",
  ...overrides,
});
test("unrelated feedback cannot complete a strategy batch; all proposals must finish and drain", () => {
  const x = new StrategyCycles();
  x.open(1);
  const a = x.add(1, 1, strategy, 403, 0, 6, true),
    b = x.add(1, 2, strategy, 403, 0, 6, true);
  const qa = { campaign_id: a, action: "solve" as const },
    qb = { campaign_id: b, action: "solve" as const };
  for (let i = 0; i < 100; i++) x.record(undefined, trial(`generic-${i}`));
  assert.equal(x.closeIfReady([], [qa, qb], 1, 403), null);
  for (let i = 0; i < 6; i++)
    x.record(a, trial(`a${i}`, i === 0 ? "construct" : "solve"));
  assert.equal(x.allow(qa, [], false), false);
  assert.equal(x.closeIfReady([], [qa, qb], 2, 403), null);
  for (let i = 0; i < 6; i++)
    x.record(b, trial(`b${i}`, i === 0 ? "construct" : "solve"));
  assert.equal(x.closeIfReady([qb], [qa, qb], 3, 403), null);
  const feedback = x.closeIfReady([], [qa, qb], 4, 403)!;
  assert.equal(feedback.status, "batch_evaluated");
  assert.equal(feedback.strategies[0].cp_trials, 5);
  assert.equal(feedback.strategies[0].statuses.UNKNOWN, 6);
  assert.equal(x.allow(qa, [], false), false);
  assert.equal(x.allow(qa, [], true), true); // rounds exhausted: keep searching, not an early-stop rule
  x.record(a, trial("later"));
  assert.equal(feedback.strategies[0].completed_trials, 6); // sent feedback is frozen
  assert.equal(x.summary(a).completed_trials, 7);
});
test("parallel launches cannot overfill a batch; construction has no compulsory CP quota", () => {
  const x = new StrategyCycles();
  x.open(1);
  const id = x.add(1, 1, strategy, 403, 0, 6, true);
  const c = { campaign_id: id, action: "construct" as const },
    s = { campaign_id: id, action: "solve" as const };
  assert.equal(x.allow(c, [c, c, c], false), true);
  assert.equal(x.allow(s, [c, c, c], false), true);
  assert.equal(x.allow(s, [c, c, c, s, s, s], false), false);
  const y = new StrategyCycles();
  y.open(1);
  const one = y.add(1, 1, strategy, null, 0, 1, true);
  assert.equal(
    y.allow({ campaign_id: one, action: "construct" }, [], false),
    true,
  );
});
test("UNKNOWN does not automatically increase CP allowance; statistics credit only published improvement", () => {
  const x = new StrategyCycles();
  x.open(1);
  const id = x.add(1, 1, strategy, 403, 0, 6, true);
  assert.equal(x.cpAllowance(id, 30, 120), 30);
  x.record(id, trial("1"));
  assert.equal(x.cpAllowance(id, 30, 120), 30);
  x.record(id, trial("2"));
  assert.equal(x.cpAllowance(id, 30, 120), 30);
  x.record(id, trial("3"));
  assert.equal(x.cpAllowance(id, 30, 120), 30);
  x.record(
    id,
    trial("4", "solve", {
      status: "FEASIBLE",
      valid: true,
      audit_valid: true,
      baseline_before_minutes: 405,
      day_minutes: 410,
      gain_minutes: 2,
    }),
  );
  assert.equal(x.summary(id).published_gain_minutes, 2);
  assert.equal(x.summary(id).best_candidate_minutes, 410);
  assert.equal(x.cpFailures(id), 0);
  assert.equal(x.summary(id).cpu_slot_seconds, 960);
});
test("checkpoint restores partial campaign identity, quota, seed and evidence", () => {
  const x = new StrategyCycles();
  x.open(1);
  const id = x.add(1, 1, strategy, null, 0, 3, true);
  x.nextSeed(id);
  x.record(id, trial("a", "construct"));
  const state = JSON.parse(JSON.stringify(x));
  const y = new StrategyCycles(state.campaigns, state.cycles);
  assert.equal(y.nextSeed(id), strategy.seeds[0] + 1);
  const q = { campaign_id: id, action: "solve" as const };
  assert.equal(y.closeIfReady([], [q], 1, null), null);
  y.record(id, trial("b"));
  y.record(id, trial("c"));
  assert.equal(y.closeIfReady([], [q], 2, null)!.status, "batch_evaluated");
  assert.deepEqual(y.summary(id).evidence_ids, ["a", "b", "c"]);
});
test("rejected proposal or exhausted work is explicit feedback, not a success or proof", () => {
  const x = new StrategyCycles();
  x.open(1);
  const id = x.add(1, 1, strategy, null, 0, 6, true);
  x.remove(id);
  assert.equal(
    x.closeIfReady([], [], 1, null)!.status,
    "no_executable_strategy",
  );
  x.open(2);
  x.add(2, 1, strategy, null, 0, 6, true);
  assert.equal(x.closeIfReady([], [], 2, null)!.status, "tasks_exhausted");
});
