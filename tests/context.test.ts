import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { Memory, profile } from "../src/memory.js";
import { bootstrap } from "../src/learning.js";
import {
  stateDistance,
  directReuse,
  strategyReference,
  plannerScenario,
  type ProductionFeatures,
} from "../src/context.js";
import { compressFeedback, updateExperience } from "../src/auxiliary_roles.js";
import { Events } from "../src/events.js";
import { ModelClient } from "../src/client.js";

const base = JSON.parse(readFileSync("configs/orders20.json", "utf8"));
const features: ProductionFeatures = {
  now_minutes: 0,
  production_day: 1,
  day_remaining_minutes: 1440,
  horizon_remaining_minutes: 5760,
  order_counts: { C430: 5 },
  remaining_operations: { C430: 40 },
  remaining_forge_minutes: { C430: 390 },
  cold_orders: 20,
  active_orders: 0,
  completed_orders: 0,
  heating_by_temperature: {},
  furnace_temperature_counts: { "1150": 6 },
  min_remaining_hold_minutes: 0,
  min_upper_slack_minutes: 1440,
  urgent_heating: 0,
  due_slack_min_minutes: 5760,
  free_slots: 30,
  press_busy: false,
  vehicles: [
    { location: 0, busy_minutes: 0 },
    { location: 0, busy_minutes: 0 },
  ],
};
test("state matching follows physical conditions, not day number; direct reuse is exact only", () => {
  const b = { ...features, production_day: 3 };
  assert.equal(stateDistance(features, b).distance, 0);
  const hot = {
    ...features,
    active_orders: 8,
    cold_orders: 12,
    min_upper_slack_minutes: 10,
    free_slots: 22,
  };
  assert.ok(stateDistance(features, hot).distance > 0);
  const sc = { ...base, state_features: features };
  const c = bootstrap(sc)[0];
  assert.equal(
    directReuse({ exact_match: true, strategy: c }, sc).eligible,
    true,
  );
  assert.equal(
    directReuse({ exact_match: false, strategy: c }, sc).eligible,
    false,
  );
  const changed = { ...sc, orders: sc.orders.slice(0, 4) };
  assert.equal(
    directReuse({ exact_match: true, strategy: c }, changed).eligible,
    false,
  );
  assert.ok(strategyReference(c, changed).differing_routes.length);
});
test("multiple retrieved alternatives include old good evidence; count changes require model reasoning; feedback is attributable", () => {
  const dir = mkdtempSync(join(tmpdir(), "ld-v3-context-")),
    m = new Memory(join(dir, "m.sqlite"));
  try {
    const sc = { ...base, state_features: features },
      p = profile(sc, "rules", "none"),
      good = bootstrap(sc)[0];
    m.record("old-good", p, good, {
      source: "construct",
      valid: true,
      audit_valid: true,
      gain_minutes: 10,
      cost: 1,
    });
    for (let i = 0; i < 120; i++)
      m.record(
        `new-${i}`,
        p,
        { ...good, policies: [1], name: `failed-${i}` },
        {
          valid: false,
          status: "construction_deadend",
          gain_minutes: 0,
          cost: 1,
        },
      );
    const other = {
      ...good,
      admission: "rolling" as const,
      vehicle_policy: "split" as const,
      policies: [2],
    };
    m.record("other", p, other, {
      source: "construct",
      valid: true,
      audit_valid: true,
      gain_minutes: 2,
      cost: 1,
    });
    const now = {
      ...sc,
      orders: sc.orders.filter((_: unknown, i: number) => i % 5 < 2),
      state_features: { ...features, cold_orders: 8 },
    };
    const target = profile(now, "rules", "none"),
      hits = m.retrieve(target);
    assert.ok(hits.some((h) => h.id === "old-good"));
    assert.ok(hits.some((h) => h.id === "other"));
    assert.ok(hits.every((h) => !h.exact_match));
    assert.ok(hits.every((h) => h.retrieval.success_probability === null));
    m.transfer(["old-good"], "adapted-trial", target, {
      valid: true,
      gain_minutes: 12,
      cost: 5,
      source: "construct",
    });
    assert.equal(
      m.retrieve(target).find((h) => h.id === "old-good")!.retrieval
        .transfer_valid,
      1,
    );
    assert.throws(() => m.transfer(["missing"], "x", target, { valid: false }));
  } finally {
    m.close();
    rmSync(dir, { recursive: true, force: true });
  }
});
test("independent summary retains recent tail; updater applies only cited versioned patches", async () => {
  const dir = mkdtempSync(join(tmpdir(), "ld-v3-roles-")),
    m = new Memory(join(dir, "m.sqlite"));
  try {
    const p = profile({ ...base, state_features: features }, "rules", "none"),
      s = bootstrap(base)[0];
    const facts = Array.from({ length: 10 }, (_, i) => ({
      id: `id${i}`,
      context: features,
      strategy: s,
      outcome: {
        valid: true,
        audit_valid: true,
        gain_minutes: 1,
        cost: 1,
        status: "AUDITED",
      },
    }));
    facts.forEach((f) => m.record(f.id, p, s, f.outcome));
    const events = new Events(join(dir, "events"), "run", performance.now());
    const summaryClient = {
      generate: async (_system: string, payload: any) => {
        assert.equal(payload.older_facts.length, 4);
        return {
          text: JSON.stringify({
            summary: "E1: Four audited trials; no causal claim.",
            evidence_ids: ["E1"],
          }),
          response_model: "qwen3.7-plus",
          usage: {},
          stop_reason: "end_turn",
          wall_s: 0,
        };
      },
    } as Pick<ModelClient, "generate">;
    const summary = await compressFeedback(
      summaryClient,
      facts,
      null,
      features,
      6,
      events,
      new AbortController().signal,
      1,
      join(dir, "summary.json"),
    );
    assert.equal(summary.status, "model");
    assert.equal(summary.covered_evidence_ids.length, 4);
    assert.equal(m.cards(p.compatible).length, 0);
    const nextClient = {
      generate: async (_system: string, payload: any) => {
        assert.equal(payload.older_facts.length, 1);
        assert.ok(payload.previous_summary.startsWith("E2:"));
        return {
          text: JSON.stringify({
            summary: "E2 is prior evidence; E1 is new. No causal claim.",
            evidence_ids: ["E1", "E2"],
          }),
          response_model: "qwen3.7-plus",
          usage: {},
          stop_reason: "end_turn",
          wall_s: 0,
        };
      },
    } as Pick<ModelClient, "generate">;
    const next = await compressFeedback(
      nextClient,
      [...facts, { ...facts[0], id: "id10" }],
      summary,
      features,
      6,
      events,
      new AbortController().signal,
      1,
      join(dir, "summary2.json"),
    );
    assert.equal(next.status, "model");
    assert.deepEqual(next.evidence_ids, ["id4", "id0"]);
    assert.equal(next.covered_evidence_ids.length, 5);
    const updater = {
      generate: async () => ({
        text: JSON.stringify({
          patches: [
            {
              op: "add",
              id: "conditional",
              expected_version: 0,
              conditions: ["cold start"],
              recommendation: "Consider the grouping; tested only here.",
              exceptions: [],
              evidence_ids: ["E1"],
            },
          ],
        }),
        response_model: "qwen3.7-plus",
        usage: {},
        stop_reason: "end_turn",
        wall_s: 0,
      }),
    } as Pick<ModelClient, "generate">;
    const result = await updateExperience(
      updater,
      facts,
      [],
      features,
      m,
      p.compatible,
      true,
      events,
      new AbortController().signal,
      1,
      join(dir, "update.json"),
    );
    assert.equal(result.status, "model");
    assert.equal(m.cards(p.compatible)[0].measured.trials, 1);
    assert.equal(m.cards(p.compatible)[0].source_contexts[0].active_orders, 0);
  } finally {
    m.close();
    rmSync(dir, { recursive: true, force: true });
  }
});

test("planner sees current commitments without receiving full executed logs or future answers", () => {
  const sc = structuredClone(base),
    o = sc.orders[0];
  sc.orders = [o];
  sc.state_features = features;
  sc.production_state = {
    now_ticks: 6,
    operations: [{ order: o.id, fire: 1, heat_start: 15, furnace: 0 }],
    transports: [
      {
        order: o.id,
        fire: 1,
        kind: "cold_to_furnace",
        source: 0,
        destination: 1,
        start: 0,
        end: 15,
        vehicle: 0,
      },
    ],
    furnaces: [{ id: 0, temperature: 1150 }],
    vehicles: [{ id: 0, location: 1, available_at_ticks: 15 }],
  };
  const p = plannerScenario(sc) as any;
  assert.equal(p.production_state, undefined);
  assert.equal(p.observed_materials[0].status, "transporting");
  assert.equal(p.observed_materials[0].heat_arrival_in_minutes, 3);
  assert.equal(
    p.observed_materials[0].remaining_hold_min_minutes,
    sc.routes[0].operations[0].hold_min,
  );
  assert.equal(p.observed_materials[0].current_trip.arrival_in_minutes, 3);
});
