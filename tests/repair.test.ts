import { memoryFacts } from "../src/memory.js";
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import {
  Repairs,
  constructionFailure,
  constructionKey,
} from "../src/repair.js";
import { bootstrap } from "../src/learning.js";
import { StrategyCycles } from "../src/strategy_cycles.js";
const strategy = bootstrap(
  JSON.parse(readFileSync("configs/orders20.json", "utf8")),
)[0];
const failure = constructionFailure({
  status: "PROGRAM_ERROR",
  failure: {
    kind: "program_error",
    message: "Forbidden program syntax: IsNot",
    proof_scope: "none",
    details: { stage: "program_validation" },
  },
})!;
test("original constructor errors survive CP UNKNOWN and carry no infeasibility proof", () => {
  assert.equal(failure.kind, "program_error");
  assert.match(failure.message, /IsNot/);
  assert.equal(failure.proof_scope, "none");
  assert.equal(constructionFailure({ valid: true, audit_valid: true }), null);
});
test("repair inbox survives interrupted model calls and counts a whole revision lineage", () => {
  const r = new Repairs();
  const t = r.request("a", "task1", "fact1", strategy, failure);
  assert.equal(r.request("a", "task2", "fact2", strategy, failure), t);
  t.state = "running";
  t.calls = 1;
  const restored = new Repairs(JSON.parse(JSON.stringify(r.tickets)));
  assert.equal(restored.next(2)!.state, "pending");
  const first = restored.tickets[0];
  first.state = "answered";
  first.children = ["b"];
  const second = restored.request("b", "task3", "fact3", strategy, failure);
  assert.equal(second.root_campaign_id, "a");
  second.calls = 1;
  assert.equal(restored.next(2), undefined);
  assert.equal(second.state, "exhausted");
});
test("CP-only or prose changes cannot masquerade as constructor repair", () => {
  assert.equal(
    constructionKey(strategy),
    constructionKey({
      ...strategy,
      name: "renamed",
      direction: "vehicle_link",
      fraction: 0.8,
    }),
  );
  assert.notEqual(
    constructionKey(strategy),
    constructionKey({ ...strategy, wip_limit: 3 }),
  );
});
test("failed construction suspends descendants even after the final planning round", () => {
  const c = new StrategyCycles();
  c.open(1);
  const id = c.add(1, 1, strategy, null, 0, 6, true);
  c.suspend(id, "construction_repair");
  assert.equal(c.allow({ campaign_id: id, action: "solve" }, [], true), false);
  const revised = c.addRepair(
    id,
    { ...strategy, wip_limit: 3 },
    null,
    1,
    6,
    true,
  );
  assert.equal(c.campaigns[revised].repaired_from, id);
  assert.equal(
    c.allow({ campaign_id: revised, action: "construct" }, [], false),
    true,
  );
  assert.equal(
    c.closeIfReady(
      [],
      [{ campaign_id: revised, action: "construct" }],
      2,
      null,
    ),
    null,
  );
});

test("Memory keeps failure classes and counts but not projected operation times", () => {
  const x = memoryFacts({
    failure: {
      kind: "construction_deadend",
      message: "bounded_deadend",
      details: {
        completed_operations: 7,
        failure_context: {
          frontier_ticks: 800,
          active: [{ op: 1, heat_start_ticks: 99 }],
          recent_choices: [{ start: 700 }],
          rejections: { hold_upper_exceeded: 3 },
        },
      },
    },
  });
  assert.equal(x.failure.details.completed_operations, 7);
  assert.deepEqual(x.failure.details.rejections, { hold_upper_exceeded: 3 });
  assert.equal(JSON.stringify(x).includes("heat_start_ticks"), false);
  assert.equal(JSON.stringify(x).includes("recent_choices"), false);
});

test("resume closes unanswered tool calls once without fabricating infeasibility", async () => {
  const { mkdtempSync, readFileSync, rmSync } = await import("node:fs");
  const { tmpdir } = await import("node:os");
  const { join } = await import("node:path");
  const { Events } = await import("../src/events.js");
  const dir = mkdtempSync(join(tmpdir(), "repair-events-"));
  try {
    const file = join(dir, "events.jsonl");
    const e = new Events(file, "run", performance.now());
    e.send("tool_call", { call_id: "task-1/1", action: "construct" });
    const restored = new Events(file, "run", performance.now());
    restored.recoverInterruptedTools();
    restored.recoverInterruptedTools();
    const rows = readFileSync(file, "utf8")
      .trim()
      .split("\n")
      .map((x) => JSON.parse(x));
    assert.equal(rows.length, 2);
    assert.equal(rows[1].data.status, "OUTCOME_UNKNOWN");
    assert.equal(rows[1].data.proof_scope, "none");
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("raising an explicit repair quota reopens exhausted work without clearing its history", () => {
  const r = new Repairs();
  const t = r.request("a", "task", "fact", strategy, failure);
  t.calls = 2;
  assert.equal(r.next(2), undefined);
  assert.equal(t.state, "exhausted");
  assert.equal(r.next(3), t);
  assert.equal(t.calls, 2);
});

test("verbose model prose does not discard an executable strategy; executable fields stay strict", async () => {
  const { parseProposal } = await import("../src/contracts.js");
  const sc = JSON.parse(readFileSync("configs/orders20.json", "utf8"));
  const p = {
    summary: "s".repeat(1200),
    candidates: [{ ...strategy, hypothesis: "h".repeat(900) }],
  };
  let fields: string[] = [];
  const parsed = parseProposal(JSON.stringify(p), sc, (f) => {
    fields = f;
  });
  assert.equal(parsed.candidates[0].hypothesis.length, 600);
  assert.deepEqual(parsed.candidates[0].groups, strategy.groups);
  assert.deepEqual(fields, ["summary", "candidates[0].hypothesis"]);
  assert.throws(() =>
    parseProposal(
      JSON.stringify({
        ...p,
        candidates: [{ ...p.candidates[0], fraction: 2 }],
      }),
      sc,
    ),
  );
  assert.throws(() =>
    parseProposal(
      JSON.stringify({
        ...p,
        candidates: [{ ...p.candidates[0], program_source: "x".repeat(10001) }],
      }),
      sc,
    ),
  );
});
