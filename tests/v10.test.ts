import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { normalizeEnvelope, recordDelivery } from "../src/protocol.js";
import { ModelClient } from "../src/client.js";
import { Events } from "../src/events.js";
import { validateProposal } from "../src/proposal_validation.js";
import { parseProposal, expand } from "../src/contracts.js";
import {
  emptyPool,
  fillNeighborhoods,
  neighborhoodKey,
} from "../src/task_pool.js";
import { CompletionPolicy } from "../src/search_policy.js";
import { editContrast } from "../src/experiment_evidence.js";
const sc = JSON.parse(readFileSync("configs/orders20.json", "utf8"));
const candidate = {
  name: "ext-split-p1-s131",
  hypothesis: "Adapt counts from current orders",
  groups: [
    sc.routes.map((r: any) => ({
      route: r.id,
      count: sc.orders.filter((o: any) => o.route === r.id).length,
    })),
  ],
  loading: "interleaved",
  policies: [1],
  seeds: [131],
  direction: "press_gap",
  fraction: 0.4,
  evidence_ids: [],
};
async function responseTest(
  replies: any[],
  verify: (c: ModelClient, answer: any, requests: any[], log: string) => void,
  options: any = {},
) {
  const old = globalThis.fetch,
    dir = mkdtempSync(join(tmpdir(), "v10-protocol-"));
  const requests: any[] = [];
  let minutes = 500;
  globalThis.fetch = async (_, init) => {
    requests.push(JSON.parse(String(init?.body)));
    minutes += 100;
    return new Response(
      JSON.stringify({
        content: [
          {
            type: "text",
            text: JSON.stringify(
              replies[Math.min(requests.length - 1, replies.length - 1)],
            ),
          },
        ],
        stop_reason: "end_turn",
      }),
      { headers: { "content-type": "application/json" } },
    );
  };
  try {
    const client = new ModelClient(
      "https://fixture.invalid",
      "fixture",
      "unused",
      "low",
      2048,
    );
    client.latestState = () => ({
      physical_revision: "same",
      search_revision: String(minutes),
      best_complete: { day_minutes: minutes },
    });
    client.validateProposal = (text) => validateProposal(text, sc, new Set());
    client.toolRuntime = {
      execute: async () => {
        throw Error("Must not execute mixed delivery");
      },
    };
    const answer = await client.generate(
      "test",
      {},
      5,
      new Events(join(dir, "events"), "test", performance.now()),
      new AbortController().signal,
    );
    verify(client, answer, requests, readFileSync(join(dir, "events"), "utf8"));
  } finally {
    globalThis.fetch = old;
    rmSync(dir, { recursive: true, force: true });
  }
}
test("real wire shape: empty tool_calls normalized BEFORE strict validator, expanded into executable queue configs despite incumbent churn", async () => {
  await responseTest(
    [
      {
        summary: "actual wire format",
        candidates: [candidate],
        tool_calls: [],
      },
    ],
    (c, a, req, log) => {
      assert.equal(req.length, 1);
      assert.equal(a.turn_status, "COMPLETED");
      const p = parseProposal(a.text, sc);
      const jobs = p.candidates.flatMap((x) => expand(x, sc));
      assert.ok(jobs.length > 0);
      assert.equal(jobs[0].seed, 131);
      assert.ok(log.includes("proposal_rebased"));
      assert.ok(log.includes("protocol_normalized"));
      assert.equal(log.includes("proposal_refresh"), false);
    },
  );
});
test("nonempty mixed tool+proposal is corrected, never silently dropped or executed", async () => {
  await responseTest(
    [
      {
        summary: "mixed",
        candidates: [candidate],
        tool_calls: [{ name: "run_algorithm", arguments: {} }],
      },
      { summary: "fixed", candidates: [candidate], tool_calls: [] },
    ],
    (c, a, req) => {
      assert.equal(req.length, 2);
      assert.ok(JSON.stringify(req[1]).includes("MIXED_DELIVERY"));
      assert.equal(a.delivery_diagnostics.protocol_errors, 1);
      assert.equal(a.delivery_reason, "delivered");
    },
  );
});
test("unknown business fields remain errors; no silent broad schema stripping", async () => {
  await responseTest(
    [
      {
        summary: "bad",
        candidates: [candidate],
        tool_calls: [],
        weaken_constraints: true,
      },
    ],
    (c, a, req) => {
      assert.equal(req.length, 3);
      assert.equal(a.delivery_reason, "protocol_rejected");
      const r: any = { planner_delivery: { errors: 0 }, errors: [] };
      recordDelivery(r, 1, a);
      assert.equal(r.planner_delivery.errors, 1);
      assert.equal(
        r.planner_delivery.non_delivery_reasons.protocol_rejected,
        1,
      );
      assert.equal(r.planner_delivery.protocol_feedback_count, 3);
    },
  );
});
test("malformed protocol never treated as a final proposal", () => {
  assert.throws(
    () => normalizeEnvelope({ tool_calls: "none" }, true),
    /INVALID_TOOL_CALLS/,
  );
  assert.throws(
    () => normalizeEnvelope({ tool_calls: [{}] }, false),
    /TOOLS_UNAVAILABLE/,
  );
});
test("task pool offers distinct scopes, resumes without replay, rebinds to a new incumbent", () => {
  const strategy = parseProposal(
    JSON.stringify({ summary: "x", candidates: [candidate] }),
    sc,
  ).candidates[0];
  const state = emptyPool(),
    ids = sc.orders.map((o: any) => o.id);
  const first = fillNeighborhoods(state, "best1", strategy, ids, 12, []);
  assert.equal(first.length, 12);
  assert.equal(new Set(first.map(neighborhoodKey)).size, 12);
  assert.ok(new Set(first.map((x) => x.direction)).size >= 4);
  const copy = JSON.parse(JSON.stringify(state));
  const second = fillNeighborhoods(copy, "best1", strategy, ids, 12, first);
  assert.ok(
    second.every(
      (s) => !first.map(neighborhoodKey).includes(neighborhoodKey(s)),
    ),
  );
  assert.ok(fillNeighborhoods(copy, "best2", strategy, ids, 6, []).length > 0);
  assert.deepEqual(first[0].groups, strategy.groups); // no process mutation
});
test("completion allowance shrinks after fruitless probes but retains future exploration credit", () => {
  const p = new CompletionPolicy();
  p.normal(1000);
  assert.equal(p.admit("a", 30, true, 0, 0, { attempts: 3, valid: 0 }), null);
  assert.equal(p.admit("b", 30, true, 0, 0, { attempts: 3, valid: 0 }), null);
  assert.equal(
    p.admit("c", 30, true, 0, 0, { attempts: 3, valid: 0 }),
    "completion_low_yield_share",
  );
  p.normal(1000);
  assert.equal(p.admit("c", 30, true, 0, 0, { attempts: 3, valid: 0 }), null);
});
test("single-run algorithm comparison preserves evidence and never claims causality", () => {
  const x = editContrast(
    {
      id: "parent",
      strategy: { groups: [1] },
      outcome: { valid: true, day_minutes: 500 },
    },
    {
      strategy: { groups: [2] },
      outcome: { valid: true, day_minutes: 520, status: "AUDITED" },
    },
    "new",
    {},
  );
  assert.equal(x.observed_delta_minutes, 20);
  assert.equal(x.verdict, "confounded");
  assert.equal(x.parent_evidence_id, "parent");
  assert.ok(x.changes.groups);
});

test("replay the actual v9 rejected response through protocol, scenario validation and constructor task expansion", async () => {
  const wires = JSON.parse(
    readFileSync("tests/fixtures/v9-wire-proposals.json", "utf8"),
  );
  assert.ok(wires.length > 0);
  const old = globalThis.fetch,
    dir = mkdtempSync(join(tmpdir(), "v10-real-wire-"));
  try {
    for (const wire of wires) {
      let calls = 0;
      globalThis.fetch = async () => {
        calls++;
        return new Response(
          JSON.stringify({
            content: [{ type: "text", text: JSON.stringify(wire) }],
            stop_reason: "end_turn",
          }),
          { headers: { "content-type": "application/json" } },
        );
      };
      const c = new ModelClient(
        "https://fixture.invalid",
        "fixture",
        "unused",
        "low",
        8192,
      );
      // Issued evidence is reconstructed only for the protocol replay, not used as a schedule hint.
      const issued = new Set<string>(
        wire.candidates.flatMap((x: any) => x.evidence_ids),
      );
      c.validateProposal = (t) => validateProposal(t, sc, issued);
      const a = await c.generate(
        "replay",
        {},
        5,
        new Events(join(dir, "events"), "test", performance.now()),
        new AbortController().signal,
      );
      assert.equal(calls, 1);
      assert.equal(a.delivery_diagnostics.protocol_errors, 0);
      const p = parseProposal(a.text, sc);
      assert.equal(p.candidates.length, 2);
      assert.ok(p.candidates.flatMap((s) => expand(s, sc)).length >= 2);
    }
  } finally {
    globalThis.fetch = old;
    rmSync(dir, { recursive: true, force: true });
  }
});
