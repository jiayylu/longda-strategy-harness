import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, rmSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import {
  liveSnapshot,
  requiresRefresh,
  algorithmChange,
} from "../src/live_state.js";
import {
  qualityFeedback,
  CompletionPolicy,
  hintProvenance,
} from "../src/search_policy.js";
import { ModelClient } from "../src/client.js";
import { Events } from "../src/events.js";

test("search revisions never advance physical state; baseline changes rebind without reconfirmation", () => {
  const a = liveSnapshot("physical", null, [], 60, 0),
    b = liveSnapshot("physical", { day_minutes: 500 }, [], 50, 2);
  assert.equal(a.physical_revision, b.physical_revision);
  assert.notEqual(a.search_revision, b.search_revision);
  assert.equal(requiresRefresh(a, b), false);
  assert.equal(
    requiresRefresh(
      b,
      liveSnapshot("physical", { day_minutes: 501 }, [], 40, 0),
    ),
    false,
  );
  assert.equal(
    requiresRefresh(
      b,
      liveSnapshot("physical", { day_minutes: 520 }, [], 40, 0),
    ),
    false,
  );
});
test("legal poor score triggers quality repair; budget UNKNOWN remains distinct", () => {
  const poor = qualityFeedback(
    { valid: true, audit_valid: true, day_minutes: 340 },
    748,
  )!;
  assert.equal(poor.kind, "construction_quality");
  assert.equal(poor.proof_scope, "none");
  assert.equal((poor.details as any).legal, true);
  assert.equal(
    qualityFeedback({ valid: true, audit_valid: true, day_minutes: 748 }, 748),
    null,
  );
  assert.equal(qualityFeedback({ valid: false, status: "UNKNOWN" }, 748), null);
  assert.equal(
    qualityFeedback({ valid: true, audit_valid: true, day_minutes: 340 }, null),
    null,
  );
});
test("completion quota persists and limits concurrent queued work, not a feasibility prune", () => {
  const p = new CompletionPolicy();
  assert.equal(p.admit("a", 60, true, 0, 0), null);
  assert.equal(p.admit("b", 60, true, 0, 0), "completion_resource_share");
  p.normal(240);
  assert.equal(p.admit("b", 60, true, 0, 0), null);
  const q = new CompletionPolicy(JSON.parse(JSON.stringify(p.ledger)));
  assert.equal(q.admit("b", 60, true, 0, 0), null);
  assert.equal(q.admit("c", 60, false, 1, 1), "queue_capacity");
  assert.equal(q.ledger.partial_reserved_s, 120);
});
test("hint attribution and algorithm edit are explicit", () => {
  assert.equal(hintProvenance({}, "best", "best").kind, "shared_incumbent");
  assert.equal(
    hintProvenance({}, "own", "best").kind,
    "own_complete_candidate",
  );
  assert.equal(
    hintProvenance({ completion: {}, partial_path: "p" }, "best", "best").kind,
    "own_partial",
  );
  const d = algorithmChange("a\nb\nc", "a\nx\nc")!;
  assert.deepEqual(d.removed, ["b"]);
  assert.deepEqual(d.added, ["x"]);
});
test("improved incumbent does not block final strategy delivery", async () => {
  const oldFetch = globalThis.fetch,
    dir = mkdtempSync(join(tmpdir(), "v8-fresh-"));
  let minutes: number | null = null;
  const requests: any[] = [];
  globalThis.fetch = async (_url, init) => {
    requests.push(JSON.parse(String(init!.body)));
    if (requests.length === 1) minutes = 700;
    return new Response(
      JSON.stringify({
        content: [
          {
            type: "text",
            text: JSON.stringify({
              summary: "test",
              candidates: [{ name: "candidate" }],
              tool_calls: [],
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
      "secret",
      "off",
      1024,
    );
    c.latestState = () =>
      liveSnapshot(
        "physical",
        minutes === null ? null : { day_minutes: minutes },
        [],
        100,
        0,
      );
    const ans = await c.generate(
      "",
      { best_complete: null },
      10,
      new Events(join(dir, "events"), "test", performance.now()),
      new AbortController().signal,
    );
    assert.equal(requests.length, 1);
    assert.equal(ans.turn_status, "COMPLETED");
    assert.ok(
      readFileSync(join(dir, "events"), "utf8").includes("proposal_rebased"),
    );
  } finally {
    globalThis.fetch = oldFetch;
    rmSync(dir, { recursive: true, force: true });
  }
});
test("physical changes require refresh and cannot bypass its limit", async () => {
  const oldFetch = globalThis.fetch,
    dir = mkdtempSync(join(tmpdir(), "v8-churn-"));
  let n = 100;
  globalThis.fetch = async () => {
    n += 20;
    return new Response(
      JSON.stringify({
        content: [{ type: "text", text: '{"summary":"x","candidates":[{}]}' }],
        stop_reason: "end_turn",
      }),
      { headers: { "content-type": "application/json" } },
    );
  };
  try {
    const c = new ModelClient(
      "https://fixture.invalid",
      "fixture",
      "secret",
      "off",
      1024,
    );
    c.latestState = () =>
      liveSnapshot(`physical-${n}`, { day_minutes: n }, [], 100, 0);
    const a = await c.generate(
      "",
      {},
      10,
      new Events(join(dir, "events"), "test", performance.now()),
      new AbortController().signal,
    );
    assert.equal(a.turn_status, "BUDGET_LIMITED");
    assert.equal(a.delivery_reason, "physical_refresh_exhausted");
    assert.deepEqual(JSON.parse(a.text).candidates, []);
  } finally {
    globalThis.fetch = oldFetch;
    rmSync(dir, { recursive: true, force: true });
  }
});

test("unchanged source read is not repeatedly executed and is pinned for final delivery", async () => {
  const oldFetch = globalThis.fetch,
    dir = mkdtempSync(join(tmpdir(), "v8-reads-"));
  let calls = 0,
    executions = 0;
  const sent: any[] = [];
  globalThis.fetch = async (_, init) => {
    sent.push(JSON.parse(String(init!.body)));
    calls++;
    const reply =
      calls < 3
        ? {
            summary: "read",
            candidates: [],
            tool_calls: [
              {
                name: "read_algorithm",
                arguments: { name: "calendar_default" },
              },
            ],
          }
        : { summary: "done", candidates: [{ name: "adapt" }], tool_calls: [] };
    return new Response(
      JSON.stringify({
        content: [{ type: "text", text: JSON.stringify(reply) }],
        stop_reason: "end_turn",
      }),
      { headers: { "content-type": "application/json" } },
    );
  };
  try {
    const c = new ModelClient(
      "https://fixture.invalid",
      "fixture",
      "secret",
      "off",
      1024,
    );
    c.toolRuntime = {
      execute: async () => {
        executions++;
        return { source: "def construct(): return root()", trial_id: "tool-1" };
      },
    };
    const answer = await c.generate(
      "",
      {},
      10,
      new Events(join(dir, "events"), "t", performance.now()),
      new AbortController().signal,
    );
    assert.equal(executions, 1);
    assert.equal(calls, 3);
    assert.equal(answer.turn_status, "COMPLETED");
    assert.ok(JSON.stringify(sent[2]).includes("working_reference_sources"));
    assert.ok(
      JSON.stringify(sent[2]).includes("def construct(): return root()"),
    );
  } finally {
    globalThis.fetch = oldFetch;
    rmSync(dir, { recursive: true, force: true });
  }
});

test("incomplete final answer cannot erase an audited algorithm-tool result", async () => {
  const oldFetch = globalThis.fetch,
    dir = mkdtempSync(join(tmpdir(), "v8-saved-"));
  let n = 0;
  globalThis.fetch = async () =>
    new Response(
      JSON.stringify(
        n++ === 0
          ? {
              content: [
                {
                  type: "text",
                  text: JSON.stringify({
                    tool_calls: [
                      { name: "run_algorithm", arguments: { source: "x" } },
                    ],
                  }),
                },
              ],
              stop_reason: "end_turn",
            }
          : {
              content: [{ type: "text", text: '{"partial"' }],
              stop_reason: "max_tokens",
            },
      ),
      { headers: { "content-type": "application/json" } },
    );
  try {
    const c = new ModelClient(
      "https://fixture.invalid",
      "fixture",
      "secret",
      "off",
      1024,
    );
    c.toolRuntime = {
      execute: async () => ({
        trial_id: "tool-1",
        valid: true,
        audit_valid: true,
        day_minutes: 500,
      }),
    };
    const ans = await c.generate(
      "",
      {},
      10,
      new Events(join(dir, "events"), "t", performance.now()),
      new AbortController().signal,
    );
    assert.equal(ans.turn_status, "BUDGET_LIMITED");
    assert.equal(ans.partial_results?.[0].tool_result.day_minutes, 500);
    assert.equal(ans.partial_results?.[1].delivery_warning, "output_limit");
  } finally {
    globalThis.fetch = oldFetch;
    rmSync(dir, { recursive: true, force: true });
  }
});
