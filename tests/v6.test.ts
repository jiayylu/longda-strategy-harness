import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { ToolReceipts, executionKey } from "../src/tool_receipts.js";
import { ModelClient } from "../src/client.js";
import { Events } from "../src/events.js";

test("receipts deduplicate identical execution and explicit retry changes attempt, not constraints", async () => {
  const dir = mkdtempSync(join(tmpdir(), "ld-v6-"));
  try {
    const receipts = new ToolReceipts(dir);
    let n = 0;
    const run = async () => ({
      trial_id: `tool-${++n}`,
      valid: false,
      status: "BUDGET_EXHAUSTED",
    });
    await receipts.run({ source: "a", seconds: 3 }, false, run);
    const r = await receipts.run({ seconds: 3, source: "a" }, false, run);
    assert.equal(r.receipt_replayed, true);
    assert.equal(n, 1);
    await receipts.run({ source: "a", seconds: 10 }, false, run);
    assert.equal(n, 2);
    await receipts.run({ source: "a", seconds: 3 }, true, run);
    assert.equal(n, 3);
    assert.notEqual(
      executionKey({ source: "a", state: { now: 1 } }),
      executionKey({ source: "a", state: { now: 2 } }),
    );
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});
test("inflight identical tools cannot execute twice", async () => {
  const dir = mkdtempSync(join(tmpdir(), "ld-v6-"));
  try {
    const r = new ToolReceipts(dir);
    let finish: (v: any) => void = () => {};
    const first = r.run(
      { a: 1 },
      false,
      () =>
        new Promise((resolve) => {
          finish = resolve;
        }),
    );
    await Promise.resolve();
    assert.equal(
      (await r.run({ a: 1 }, true, async () => ({ wrong: true }))).status,
      "ALREADY_IN_PROGRESS",
    );
    finish({ valid: true });
    await first;
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});
test("tool budget produces structured partial result and preserves each receipt", async () => {
  const dir = mkdtempSync(join(tmpdir(), "ld-v6-")),
    old = globalThis.fetch;
  let calls = 0;
  globalThis.fetch = async () =>
    new Response(
      JSON.stringify({
        content: [
          {
            type: "text",
            text: JSON.stringify({
              tool_calls: [
                {
                  name: "get_latest_state",
                  arguments: {},
                },
              ],
            }),
          },
        ],
        stop_reason: "end_turn",
      }),
      { headers: { "content-type": "application/json" } },
    );
  try {
    const c = new ModelClient(
      "https://invalid",
      "fixture",
      "secret",
      "low",
      4096,
    );
    c.turnDirectory = join(dir, "turns");
    c.toolRuntime = {
      execute: async () => ({
        trial_id: `tool-${++calls}`,
        valid: false,
        error: "needs repair",
      }),
    };
    const r = await c.generate(
      "",
      {},
      5,
      new Events(join(dir, "events"), "x", performance.now()),
      new AbortController().signal,
    );
    assert.equal(r.turn_status, "BUDGET_LIMITED");
    assert.equal(calls, 6);
    assert.equal(r.partial_results?.length, 6);
  } finally {
    globalThis.fetch = old;
    rmSync(dir, { recursive: true, force: true });
  }
});
test("malformed JSON feeds format evidence to model for same-turn correction", async () => {
  const dir = mkdtempSync(join(tmpdir(), "ld-v6-")),
    old = globalThis.fetch;
  let n = 0;
  let saw = false;
  globalThis.fetch = async (_u, init) => {
    saw ||= String(init?.body).includes("INVALID_JSON");
    return new Response(
      JSON.stringify({
        content: [{ type: "text", text: n++ ? "{}" : "{bad" }],
        stop_reason: "end_turn",
      }),
      { headers: { "content-type": "application/json" } },
    );
  };
  try {
    const c = new ModelClient(
      "https://invalid",
      "fixture",
      "secret",
      "low",
      4096,
    );
    await c.generate(
      "",
      {},
      5,
      new Events(join(dir, "events"), "x", performance.now()),
      new AbortController().signal,
    );
    assert.equal(c.requestCount, 2);
    assert.ok(saw);
  } finally {
    globalThis.fetch = old;
    rmSync(dir, { recursive: true, force: true });
  }
});

test("completed algorithm tools can finish normally without inventing another proposal", async () => {
  const dir = mkdtempSync(join(tmpdir(), "ld-v6-")),
    old = globalThis.fetch;
  let n = 0;
  globalThis.fetch = async () =>
    new Response(
      JSON.stringify({
        content: [
          {
            type: "text",
            text: JSON.stringify(
              n++
                ? { candidates: [], tool_calls: [] }
                : {
                    tool_calls: [
                      { name: "run_algorithm", arguments: { source: "x" } },
                    ],
                  },
            ),
          },
        ],
        stop_reason: "end_turn",
      }),
      { headers: { "content-type": "application/json" } },
    );
  try {
    const c = new ModelClient(
      "https://invalid",
      "fixture",
      "secret",
      "low",
      4096,
    );
    c.toolRuntime = {
      execute: async () => ({
        trial_id: "tool-1",
        valid: true,
        audit_valid: true,
      }),
    };
    const r = await c.generate(
      "",
      {},
      5,
      new Events(join(dir, "events"), "x", performance.now()),
      new AbortController().signal,
    );
    assert.equal(r.turn_status, "TOOLS_COMPLETED");
    assert.equal(r.partial_results?.[0].tool_result.audit_valid, true);
  } finally {
    globalThis.fetch = old;
    rmSync(dir, { recursive: true, force: true });
  }
});
