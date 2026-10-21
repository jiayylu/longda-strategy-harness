import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, rmSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { modelJson } from "../src/model_json.js";
import { ModelClient } from "../src/client.js";
import { Events } from "../src/events.js";
import { Candidate, parseProposal } from "../src/contracts.js";
import { bootstrap } from "../src/learning.js";
import { Memory, profile } from "../src/memory.js";

test("one fenced JSON preserves code while ambiguous blocks are rejected", () => {
  assert.deepEqual(modelJson('Diagnosis.\n```json\n{"source":"x\\ny"}\n```'), {
    source: "x\ny",
  });
  assert.throws(() => modelJson("```json\n{}\n```\n```json\n{}\n```"));
  assert.throws(() => modelJson('```json\n{"incomplete":'));
  assert.deepEqual(
    modelJson(
      'Let me inspect it:\n{"tool_calls":[{"name":"read_algorithm","arguments":{"source":"def x(): return {\\\"k\\\": 1}"}}]}',
    ),
    {
      tool_calls: [
        {
          name: "read_algorithm",
          arguments: { source: 'def x(): return {"k": 1}' },
        },
      ],
    },
  );
  assert.throws(() => modelJson("First {} then {}"));
  assert.throws(() => modelJson('First {} then {"cut":'));
});

test("model receives actual tool failure, then revises code within the same turn", async () => {
  const old = globalThis.fetch,
    dir = mkdtempSync(join(tmpdir(), "ld-alg-"));
  const requests: any[] = [];
  let count = 0;
  globalThis.fetch = async (_url, init) => {
    requests.push(JSON.parse(String(init!.body)));
    const text =
      count++ === 0
        ? JSON.stringify({
            tool_calls: [
              { name: "validate_algorithm", arguments: { source: "bad" } },
            ],
          })
        : JSON.stringify({ summary: "corrected", candidates: [] });
    return new Response(
      JSON.stringify({
        content: [{ type: "text", text }],
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
    c.toolRuntime = {
      execute: async () => ({
        valid: false,
        error: "Forbidden call open",
        trial_id: "tool-1",
      }),
    };
    const answer = await c.generate(
      "system",
      { orders: 20 },
      2,
      new Events(join(dir, "events"), "t", performance.now()),
      new AbortController().signal,
    );
    assert.equal(answer.tool_steps, 1);
    assert.equal(c.requestCount, 2);
    assert.equal(
      JSON.parse(requests[1].messages[0].content).tool_history[0].tool_result
        .error,
      "Forbidden call open",
    );
    assert.equal(requests[0].output_config.effort, "low");
  } finally {
    globalThis.fetch = old;
    rmSync(dir, { recursive: true, force: true });
  }
});

test("algorithm versions survive Memory retrieval across order counts without copying schedules", () => {
  const dir = mkdtempSync(join(tmpdir(), "ld-alg-memory-"));
  try {
    const sc = JSON.parse(readFileSync("configs/orders20.json", "utf8"));
    const strategy = Candidate.parse({
      ...bootstrap(sc)[0],
      engine: "calendar",
      algorithm_source: "def construct():\n return root()",
    });
    const db = new Memory(join(dir, "memory.sqlite")),
      p = profile(sc, "frozen", "none");
    db.record("a", p, strategy, {
      valid: false,
      audit_valid: false,
      cost: 1,
      status: "partial",
      algorithm_hash: "version-a",
    });
    const changed = { ...sc, orders: sc.orders.slice(0, 10) };
    const hits = db.retrieve(profile(changed, "frozen", "none"), 3);
    assert.equal(hits.length, 1);
    assert.equal(hits[0].strategy.algorithm_source, strategy.algorithm_source);
    assert.ok(hits[0].scenario_difference.order_count_distance > 0);
    db.close();
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("final proposal cannot silently run algorithm_source through rank-only engine", () => {
  const sc = JSON.parse(readFileSync("configs/orders20.json", "utf8"));
  const bad = {
    ...bootstrap(sc)[0],
    engine: "extended",
    algorithm_source: "def construct():\n return root()",
  };
  assert.throws(
    () =>
      parseProposal(JSON.stringify({ summary: "x", candidates: [bad] }), sc),
    /requires engine=calendar/,
  );
});

test("provider schema preserves effort exclusivity and normalizes empty tool envelope", async () => {
  const old = globalThis.fetch,
    dir = mkdtempSync(join(tmpdir(), "ld-schema-"));
  let body: any;
  globalThis.fetch = async (_url, init) => {
    body = JSON.parse(String(init!.body));
    return new Response(
      JSON.stringify({
        content: [
          {
            type: "text",
            text: JSON.stringify({
              summary: "x",
              candidates: [],
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
    const client = new ModelClient(
      "https://invalid",
      "fixture",
      "secret",
      "low",
      8192,
      1024,
    );
    client.outputSchema = {
      type: "object",
      properties: {},
      additionalProperties: false,
    };
    const r = await client.generate(
      "",
      {},
      1,
      new Events(join(dir, "e"), "x", performance.now()),
      new AbortController().signal,
    );
    assert.equal(body.output_config.format.type, "json_schema");
    assert.equal(body.output_config.effort, undefined);
    assert.equal(body.thinking.budget_tokens, 1024);
    assert.deepEqual(JSON.parse(r.text), { summary: "x", candidates: [] });
  } finally {
    globalThis.fetch = old;
    rmSync(dir, { recursive: true, force: true });
  }
});

test("candidate submission cannot be silently discarded in a mixed tool response", async () => {
  const old = globalThis.fetch,
    dir = mkdtempSync(join(tmpdir(), "ld-mixed-"));
  globalThis.fetch = async () =>
    new Response(
      JSON.stringify({
        content: [
          {
            type: "text",
            text: JSON.stringify({
              candidates: [{}],
              tool_calls: [
                {
                  name: "read_algorithm",
                  arguments: { name: "calendar_default" },
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
      8192,
    );
    c.toolRuntime = {
      execute: async () => {
        throw Error("must not execute");
      },
    };
    const answer = await c.generate(
      "",
      {},
      1,
      new Events(join(dir, "e"), "x", performance.now()),
      new AbortController().signal,
    );
    assert.equal(answer.turn_status, "BUDGET_LIMITED");
    assert.equal(answer.delivery_reason, "protocol_rejected");
    assert.equal(answer.delivery_diagnostics.protocol_errors, 3);
    assert.ok(
      readFileSync(join(dir, "e"), "utf8").includes("model_turn_partial"),
    );
  } finally {
    globalThis.fetch = old;
    rmSync(dir, { recursive: true, force: true });
  }
});
