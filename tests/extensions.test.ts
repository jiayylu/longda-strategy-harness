import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { Allocator, bootstrap, strategyKey } from "../src/learning.js";
import { Memory, profile } from "../src/memory.js";
import { checkpoint, readCheckpoint, acquire } from "../src/checkpoint.js";
import { Events } from "../src/events.js";
import { ModelClient } from "../src/client.js";
import { summarize } from "../src/auxiliary.js";
const sc = JSON.parse(readFileSync("configs/orders20.json", "utf8"));
test("policy identity excludes explanation; generic bootstrap adapts counts", () => {
  const c = bootstrap(sc)[0];
  assert.equal(
    strategyKey(c),
    strategyKey({ ...c, name: "new", hypothesis: "new", evidence_ids: ["x"] }),
  );
  for (const n of [4, 8, 20]) {
    const input = {
      ...sc,
      orders: sc.orders
        .filter((_: any, i: number) => i % 5 < Math.ceil(n / 4))
        .slice(0, n),
    };
    for (const x of bootstrap(input))
      assert.equal(
        x.groups.flat().reduce((v, r) => v + r.count, 0),
        input.orders.length,
      );
  }
});
test("resource allocation keeps exploration; tracks construction and CP separately", () => {
  const a = new Allocator();
  a.update("construct", true, 10, 1);
  a.update("cp", false, 0, 10);
  const q = [{ arm: "construct" }, { arm: "cp" }];
  assert.equal(a.pick([{ arm: "new" }, ...q]), 0);
  const choices = Array.from({ length: 15 }, () => a.pick(q));
  assert.ok(choices.includes(1));
  assert.equal(a.arms.construct.gain, 10);
  assert.equal(a.arms.cp.gain, 0);
});
test("Memory permits audited construction; patches are versioned evidence-based and correctable", () => {
  const dir = mkdtempSync(join(tmpdir(), "ld-v2-")),
    m = new Memory(join(dir, "m.sqlite")),
    p = profile(sc, "rules", "none"),
    s = bootstrap(sc)[0];
  try {
    m.record("a", p, s, {
      valid: true,
      audit_valid: true,
      cp_solver_called: false,
      gain_minutes: 4,
      cost: 2,
      source: "construct",
    });
    m.record("b", p, s, {
      valid: false,
      status: "UNKNOWN",
      gain_minutes: 0,
      cost: 20,
      source: "solve",
    });
    m.use(["a"], "trial1");
    m.use(["a"], "trial1");
    const patch = {
      op: "add",
      id: "card",
      expected_version: 0,
      conditions: ["two vehicles"],
      recommendation: "try this grouping; limited evidence",
      exceptions: [],
      evidence_ids: ["a"],
    };
    m.patches(p.compatible, [patch], ["a"]);
    assert.equal(m.cards(p.compatible)[0].measured.gain_minutes, 4);
    assert.throws(() => m.patches(p.compatible, [patch], ["a"]));
    assert.throws(() =>
      m.patches(
        p.compatible,
        [{ ...patch, id: "bad", evidence_ids: ["invented"] }],
        ["a"],
      ),
    );
    m.patches(
      p.compatible,
      [
        {
          ...patch,
          op: "update",
          expected_version: 1,
          exceptions: ["one unresolved attempt"],
          evidence_ids: ["a", "b"],
        },
      ],
      ["a", "b"],
    );
    assert.equal(m.cards(p.compatible)[0].version, 2);
    assert.equal(m.cards(p.compatible)[0].measured.valid, 1);
    assert.equal(m.stats(p.compatible).uses[0].n, 1);
    m.patches(
      p.compatible,
      [{ ...patch, op: "retire", expected_version: 2, evidence_ids: ["b"] }],
      ["b"],
    );
    assert.equal(m.cards(p.compatible).length, 0);
  } finally {
    m.close();
    rmSync(dir, { recursive: true, force: true });
  }
});
test("resume preserves pending tasks and rejects changed model; active lock prevents duplicates", () => {
  const dir = mkdtempSync(join(tmpdir(), "ld-resume-"));
  try {
    checkpoint(join(dir, "checkpoint.json"), {
      identity: "same",
      queue: [{ id: "1" }],
      running: [{ id: "2" }],
    });
    const x = readCheckpoint(join(dir, "checkpoint.json"), "same");
    assert.equal(x.queue.length + x.running.length, 2);
    assert.throws(() =>
      readCheckpoint(join(dir, "checkpoint.json"), "changed"),
    );
    const release = acquire(dir, false);
    assert.throws(() => acquire(dir, true));
    release();
    const f = join(dir, "events.jsonl");
    new Events(f, "run", performance.now()).send("run_started");
    const e = new Events(f, "run", performance.now()).send("checkpoint");
    assert.equal(e.seq, 2);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});
test("auxiliary timeout falls back to factual summary without throwing", async () => {
  const dir = mkdtempSync(join(tmpdir(), "ld-aux-"));
  try {
    const fake = {
      generate: async () => {
        throw Error("timeout");
      },
    } as unknown as ModelClient;
    const r = await summarize(
      fake,
      [{ id: "1", strategy: bootstrap(sc)[0], outcome: { status: "UNKNOWN" } }],
      [],
      null,
      "p",
      new Events(join(dir, "events"), "r", performance.now()),
      new AbortController().signal,
      1,
      join(dir, "aux.json"),
      false,
    );
    assert.equal(r.status, "fallback");
    assert.ok(r.summary.includes("UNKNOWN"));
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});
