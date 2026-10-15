import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { parseProposal, expand, type Scenario } from "../src/contracts.js";
import { Memory, profile, memoryFacts } from "../src/memory.js";
import { Events } from "../src/events.js";
const sc = JSON.parse(
  readFileSync("configs/orders20.json", "utf8"),
) as Scenario;
const candidate = {
  name: "test",
  hypothesis: "testing only",
  evidence_ids: [],
  groups: sc.routes.map((r) => [
    { route: r.id, count: sc.orders.filter((o) => o.route === r.id).length },
  ]),
  loading: "grouped",
  policies: [0],
  seeds: [0],
  direction: "broad",
  fraction: 1,
  focus_orders: [],
};
test("strategy conserves every order; rejects process edits and unknown fields", () => {
  const p = parseProposal(
    JSON.stringify({ summary: "test", candidates: [candidate] }),
    sc,
  );
  assert.equal(expand(p.candidates[0], sc)[0].waves.flat().length, 20);
  assert.throws(() =>
    parseProposal(
      JSON.stringify({
        summary: "test",
        candidates: [{ ...candidate, hold_max: 999 }],
      }),
      sc,
    ),
  );
  assert.throws(() =>
    parseProposal(
      JSON.stringify({
        summary: "test",
        candidates: [{ ...candidate, groups: candidate.groups.slice(1) }],
      }),
      sc,
    ),
  );
});
test("memory requires complete audit; exact context preferred; cars/rules incompatible", () => {
  const dir = mkdtempSync(join(tmpdir(), "ld-memory-"));
  const m = new Memory(join(dir, "memory.sqlite"));
  try {
    const c = parseProposal(
      JSON.stringify({ summary: "t", candidates: [candidate] }),
      sc,
    ).candidates[0];
    const p = profile(sc, "rules", "none");
    assert.throws(() => m.record("bad", p, c, { valid: true }));
    m.record("1", p, c, {
      valid: true,
      cp_solver_called: true,
      audit_valid: true,
      day_minutes: 123,
    });
    m.record("1", p, c, {
      valid: true,
      cp_solver_called: true,
      audit_valid: true,
      day_minutes: 123,
    });
    assert.equal(m.count(), 1);
    assert.equal(m.retrieve(p)[0].exact_match, true);
    assert.equal(
      m.retrieve(
        profile(
          { ...sc, transport: { ...sc.transport, vehicle_count: 1 } },
          "rules",
          "none",
        ),
      ).length,
      0,
    );
    assert.equal(m.retrieve(profile(sc, "changed-rules", "none")).length, 0);
    assert.equal(
      m.retrieve(profile({ ...sc, initial_temperature: 1100 }, "rules", "none"))
        .length,
      0,
    );
    assert.equal(
      m.retrieve(profile({ ...sc, ramp_down_deg_per_min: 6 }, "rules", "none"))
        .length,
      0,
    );
    assert.equal(m.retrieve(profile(sc, "rules", "hold-near-min")).length, 0);
  } finally {
    m.close();
    rmSync(dir, { recursive: true, force: true });
  }
});
test("events persist before publication with stable run identity", () => {
  const dir = mkdtempSync(join(tmpdir(), "ld-event-"));
  try {
    const file = join(dir, "events.jsonl"),
      events = new Events(file, "run", performance.now());
    events.on("event", (e) => {
      assert.equal(JSON.parse(readFileSync(file, "utf8").trim()).seq, e.seq);
      assert.equal(e.run_id, "run");
    });
    events.send("run_started", { orders: 20 });
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});
test("Memory rejects saved answers and strips operation times from diagnostics", () => {
  assert.throws(() => memoryFacts({ schedule: { forge_start: [12] } }));
  assert.throws(() => memoryFacts({ result: { heat_start: [2] } }));
  const facts = memoryFacts({
    day_minutes: 123,
    diagnostics: {
      day1_by_route: { R: 123 },
      largest_press_gaps: [{ start_minutes: 60, end_minutes: 70, minutes: 10 }],
      early_day2_operations: [{ order: "A", start_minutes: 1500 }],
      largest_hold_excess: [{ order: "A", fire: 2, minutes: 20 }],
    },
  });
  assert.equal(facts.day_minutes, 123);
  assert.deepEqual(facts.diagnostics.largest_press_gap_minutes, [10]);
  assert.equal(facts.diagnostics.early_day2_operations, undefined);
  assert.equal(JSON.stringify(facts).includes("start_minutes"), false);
});
