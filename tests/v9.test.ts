import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, writeFileSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createHash } from "node:crypto";
import { Allocator, schedulingArm } from "../src/learning.js";
import { validateProposal } from "../src/proposal_validation.js";
import { EvidenceCatalog } from "../src/evidence.js";
import {
  completionValue,
  DiversityPolicy,
  scheduleStructure,
} from "../src/search_policy.js";
import { actualSearch, summarizeSearch } from "../src/search_statistics.js";
import { ModelClient } from "../src/client.js";
import { Events } from "../src/events.js";
const sc = JSON.parse(readFileSync("configs/orders20.json", "utf8"));
const candidate: any = {
  name: "plan",
  hypothesis: "test",
  groups: [sc.routes.map((r: any) => ({ route: r.id, count: 5 }))],
  loading: "grouped",
  policies: [0],
  seeds: [0],
  direction: "press_gap",
  fraction: 0.4,
  evidence_ids: [],
};
const proposal = (c: any) =>
  JSON.stringify({ summary: "test", candidates: [c] });
test("vehicle assignments are part of structural novelty", () => {
  const a = {
    forge_start: [10],
    furnace: [0],
    transport: [
      { id: 0, op: 0, kind: "to_press", start: 5, pickup_rank: 0, vehicle: 0 },
    ],
  };
  const b = structuredClone(a);
  b.transport[0].vehicle = 1;
  assert.notDeepEqual(scheduleStructure(a), scheduleStructure(b));
});
test("normalized resource shares invariant to units and account for in-flight reservations", () => {
  const simulate = (scale: number) => {
    const a = new Allocator();
    a.update("good", true, 10, 100 * scale);
    a.update("weak", true, 1, 100 * scale);
    for (let i = 0; i < 200; i++)
      a.pick([
        { arm: "good", estimated_cost: 10 * scale },
        { arm: "weak", estimated_cost: 10 * scale },
      ]);
    return a.allocations.good / (a.allocations.good + a.allocations.weak);
  };
  assert.equal(simulate(1), simulate(60));
  assert.ok(simulate(1) > 0.75 && simulate(1) < 0.95);
  const a = new Allocator();
  assert.equal(a.pick([{ arm: "a" }, { arm: "b" }]), 0);
  assert.equal(a.pick([{ arm: "a" }, { arm: "b" }]), 1);
});
test("actual scope instead of intent labels classifies full-model search", () => {
  assert.equal(
    schedulingArm(
      {
        action: "solve",
        strategy: { direction: "furnace_batch", fraction: 1 },
      },
      true,
      20,
    ),
    "cp:global_improvement",
  );
  assert.equal(
    actualSearch(
      { action: "solve", strategy: { direction: "furnace_batch" } },
      { scope: "global", released_orders: ["a"], cp_solver_called: true },
      1,
    ).task_kind,
    "global_improvement",
  );
});
test("count errors specify expected actual delta without modifying orders", () => {
  const c = structuredClone(candidate);
  c.groups[0][0].count = 6;
  assert.throws(
    () => validateProposal(proposal(c), sc, new Set()),
    (e) =>
      String(e).includes("ORDER_COUNT_MISMATCH") &&
      String(e).includes("actual") &&
      String(e).includes("delta"),
  );
  assert.equal(sc.orders.length, 20);
});
test("wrong run prefix yields exact evidence alternatives, never silent substitution", () => {
  assert.throws(
    () =>
      validateProposal(
        proposal({ ...candidate, evidence_ids: ["wrong/task-192/1"] }),
        sc,
        new Set(["correct/task-192/1"]),
      ),
    (e) =>
      String(e).includes("wrong/task-192/1") &&
      String(e).includes("correct/task-192/1"),
  );
});
test("valid sibling survives invalid proposal candidate", () => {
  try {
    validateProposal(
      JSON.stringify({
        summary: "test",
        candidates: [
          candidate,
          { ...candidate, name: "bad", evidence_ids: ["not-issued"] },
        ],
      }),
      sc,
      new Set(),
    );
    assert.fail();
  } catch (e) {
    assert.equal((e as any).accepted[0].name, "plan");
  }
});
test("historical evidence is issued scoped and digest checked; unavailable raw is explicit", () => {
  const dir = mkdtempSync(join(tmpdir(), "v9-evidence-"));
  try {
    const f = join(dir, "result.json");
    writeFileSync(
      f,
      JSON.stringify({
        valid: true,
        schedule_path: "/do-not-inject",
        day_minutes: 858,
      }),
    );
    const index = join(dir, "index.json");
    writeFileSync(
      index,
      JSON.stringify({
        roots: [dir],
        entries: {
          "old/task-595/1": {
            path: f,
            sha256: createHash("sha256").update(readFileSync(f)).digest("hex"),
          },
        },
      }),
    );
    const cat = new EvidenceCatalog(index),
      issued = new Set(["old/task-595/1", "old/missing"]);
    assert.equal(cat.read("old/task-595/1", issued).raw.day_minutes, 858);
    assert.equal(
      cat.read("old/task-595/1", issued).raw.schedule_path,
      undefined,
    );
    assert.equal(
      cat.read("old/missing", issued, { summary: 1 }).availability,
      "summary_only",
    );
    assert.throws(() => cat.read("guessed", issued));
    writeFileSync(f, "{}");
    assert.throws(() => cat.read("old/task-595/1", issued), /ARTIFACT_CHANGED/);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});
test("partial priority uses consistency slack and experience; low coverage is not excluded", () => {
  const low = completionValue(
    {
      scheduled_forges: 3,
      total_operations: 160,
      window_conflicts: 0,
      slack_min_minutes: 300,
    },
    { attempts: 0, valid: 0 },
  );
  const high = completionValue(
    {
      scheduled_forges: 150,
      total_operations: 160,
      window_conflicts: 50,
      slack_min_minutes: -50,
    },
    { attempts: 39, valid: 0 },
  );
  assert.ok(low.priority > high.priority);
  assert.ok(high.priority > 0);
});
test("novelty ignores absolute clock/furnace labels and probes have resource accounting", () => {
  assert.deepEqual(
    scheduleStructure({ forge_start: [1, 2], furnace: [0, 1] }),
    scheduleStructure({ forge_start: [10, 20], furnace: [3, 5] }),
  );
  const p = new DiversityPolicy();
  assert.equal(p.admit("a", 10), null);
  assert.equal(p.admit("a", 10), "duplicate_structure");
  assert.equal(p.admit("b", 10), "exploration_share");
  p.normal(90);
  assert.equal(p.admit("b", 10), null);
});
test("statistics distinguish accepted results publications and completion subset", () => {
  const r = summarizeSearch({
    publications: [{ day_minutes: 519 }],
    history: [
      {
        source: "construct",
        valid: true,
        audit_valid: true,
        status: "AUDITED",
      },
      {
        source: "solve",
        cp_solver_called: true,
        status: "UNKNOWN",
        actual_search: { task_kind: "partial_completion" },
      },
    ],
  });
  assert.equal(r.complete_valid_results, 1);
  assert.equal(r.incumbent_publications, 1);
  assert.equal(r.by_actual_work.partial_completion.solver_calls, 1);
  assert.equal(r.measured_cpu_seconds, null);
});
test("same model turn repairs concrete group error before dispatch", async () => {
  const oldFetch = globalThis.fetch,
    dir = mkdtempSync(join(tmpdir(), "v9-correction-"));
  let calls = 0;
  const requests: any[] = [];
  globalThis.fetch = async (_url, init) => {
    requests.push(JSON.parse(String(init?.body)));
    calls++;
    const c = structuredClone(candidate);
    if (calls === 1) c.groups[0][0].count = 6;
    return new Response(
      JSON.stringify({
        content: [{ type: "text", text: proposal(c) }],
        stop_reason: "end_turn",
      }),
      { headers: { "content-type": "application/json" } },
    );
  };
  try {
    const c = new ModelClient(
      "https://fixture.invalid",
      "fixture",
      "unused",
      "low",
      1000,
    );
    c.latestState = () => ({
      physical_revision: "same",
      search_revision: "same",
    });
    c.validateProposal = (t) => validateProposal(t, sc, new Set());
    const r = await c.generate(
      "test",
      {},
      10,
      new Events(join(dir, "events.jsonl"), "test", performance.now()),
      new AbortController().signal,
    );
    assert.equal(calls, 2);
    assert.ok(JSON.stringify(requests[1]).includes("ORDER_COUNT_MISMATCH"));
    assert.equal(JSON.parse(r.text).candidates[0].groups[0][0].count, 5);
  } finally {
    globalThis.fetch = oldFetch;
    rmSync(dir, { recursive: true, force: true });
  }
});

test("valid sibling is delivered when repeated sibling corrections fail", async () => {
  const oldFetch = globalThis.fetch,
    dir = mkdtempSync(join(tmpdir(), "v9-retained-"));
  let calls = 0;
  globalThis.fetch = async () => {
    calls++;
    return new Response(
      JSON.stringify({
        content: [
          {
            type: "text",
            text: JSON.stringify({
              summary: "mixed",
              candidates: [
                candidate,
                { ...candidate, name: "invalid", evidence_ids: ["unissued"] },
              ],
            }),
          },
        ],
        stop_reason: "end_turn",
      }),
      { headers: { "content-type": "application/json" } },
    );
  };
  try {
    const c = new ModelClient(
      "https://fixture.invalid",
      "fixture",
      "unused",
      "low",
      1000,
    );
    c.latestState = () => ({
      physical_revision: "same",
      search_revision: "same",
    });
    c.validateProposal = (t) => validateProposal(t, sc, new Set());
    const r = await c.generate(
      "test",
      {},
      10,
      new Events(join(dir, "events.jsonl"), "test", performance.now()),
      new AbortController().signal,
    );
    assert.equal(calls, 3);
    assert.deepEqual(
      JSON.parse(r.text).candidates.map((x: any) => x.name),
      ["plan"],
    );
  } finally {
    globalThis.fetch = oldFetch;
    rmSync(dir, { recursive: true, force: true });
  }
});
