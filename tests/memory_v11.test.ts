import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { Memory, profile } from "../src/memory.js";
import { bootstrap } from "../src/learning.js";
import { memoryStage } from "../src/memory_stage.js";
import { validateToken } from "../src/credentials.js";
import { updateExperience } from "../src/auxiliary_roles.js";
import { Events } from "../src/events.js";
import { summarizeSearch } from "../src/search_statistics.js";

const sc = JSON.parse(readFileSync("configs/orders20.json", "utf8"));
function fixture() {
  const dir = mkdtempSync(join(tmpdir(), "longda-memory-v11-"));
  const db = new Memory(join(dir, "memory.sqlite")),
    p = profile(sc, "rules", "none"),
    s = bootstrap(sc)[0];
  const add = (id: string, extra: any) =>
    db.record(id, p, s, {
      valid: true,
      audit_valid: true,
      day_minutes: 400,
      cost: 2,
      ...extra,
    });
  add("construct-good", { source: "construct", gain_minutes: 0 });
  add("cp-good", {
    source: "solve",
    baseline_before_minutes: 854,
    day_minutes: 858,
    gain_minutes: 4,
  });
  add("partial-good", {
    source: "solve",
    purpose: "partial_complete",
    gain_minutes: 0,
  });
  add("unknown", {
    source: "solve",
    status: "UNKNOWN",
    valid: false,
    audit_valid: false,
    gain_minutes: 0,
  });
  return {
    db,
    p,
    s,
    dir,
    close: () => {
      db.close();
      rmSync(dir, { recursive: true, force: true });
    },
  };
}
const patch = (id: string, evidence_ids: string[]) => ({
  op: "add",
  id,
  expected_version: 0,
  conditions: ["same input"],
  recommendation: "Keep testing this direction; observational evidence only.",
  exceptions: [],
  evidence_ids,
});

test("retrieval separates constructor, partial completion and CP improvement evidence", () => {
  const f = fixture();
  try {
    assert.equal(f.db.retrieve(f.p, 8, "construct")[0].id, "construct-good");
    assert.equal(f.db.retrieve(f.p, 8, "cp_complete")[0].id, "partial-good");
    assert.equal(f.db.retrieve(f.p, 8, "cp_improve")[0].id, "cp-good");
    assert.deepEqual(
      f.db
        .stats(f.p.compatible)
        .strategies.map((x: any) => x.stage)
        .sort(),
      ["construct", "cp_complete", "cp_improve"].sort(),
    );
    assert.equal(
      memoryStage({ source: "solve", phase: "cold_start" }),
      "cp_feasibility",
    );
    assert.throws(
      () =>
        f.db.patches(
          f.p.compatible,
          [patch("mixed", ["construct-good", "cp-good"])],
          ["construct-good", "cp-good"],
        ),
      /ONE executed stage/,
    );
    assert.throws(
      () =>
        f.db.patches(
          f.p.compatible,
          [{ ...patch("wrong", ["cp-good"]), stage: "construct" }],
          ["cp-good"],
        ),
      /disagrees/,
    );
  } finally {
    f.close();
  }
});
test("858 regression: same successful evidence with op retire cannot remove a card", () => {
  const f = fixture();
  try {
    const original = patch("full-search", ["cp-good"]);
    f.db.patches(f.p.compatible, [original], ["cp-good"]);
    assert.throws(
      () =>
        f.db.patches(
          f.p.compatible,
          [{ ...original, op: "retire", expected_version: 1 }],
          ["cp-good"],
        ),
      /Retirement requires/,
    );
    assert.throws(
      () =>
        f.db.patches(
          f.p.compatible,
          [
            {
              ...original,
              op: "retire",
              expected_version: 1,
              retirement: {
                reason: "evidence_error",
                explanation:
                  "The newer calls were UNKNOWN so this might be unreliable.",
                evidence_ids: ["unknown"],
              },
            },
          ],
          ["cp-good", "unknown"],
        ),
      /independently verified/,
    );
    assert.throws(
      () =>
        f.db.patches(
          f.p.compatible,
          [
            {
              ...original,
              op: "update",
              expected_version: 1,
              evidence_ids: ["unknown"],
            },
          ],
          ["unknown"],
        ),
      /retain earlier successful/,
    );
    assert.equal(f.db.cards(f.p.compatible)[0].version, 1);
  } finally {
    f.close();
  }
});
test("retirement supports real supersession with active same-stage replacement preserving evidence", () => {
  const f = fixture();
  try {
    f.db.patches(
      f.p.compatible,
      [patch("old", ["cp-good"]), patch("new", ["cp-good", "unknown"])],
      ["cp-good", "unknown"],
    );
    const retire = {
      ...patch("old", ["cp-good"]),
      op: "retire",
      expected_version: 1,
      retirement: {
        reason: "superseded",
        explanation:
          "The replacement preserves this successful trial and adds a documented unresolved trial.",
        evidence_ids: ["cp-good", "unknown"],
        replacement_id: "new",
      },
    };
    assert.throws(
      () => f.db.patches(f.p.compatible, [retire], ["unknown"]),
      /Unknown evidence/,
    );
    f.db.patches(f.p.compatible, [retire], ["cp-good", "unknown"]);
    assert.deepEqual(
      f.db.cards(f.p.compatible).map((c) => c.id),
      ["new"],
    );
  } finally {
    f.close();
  }
});
test("updater receives old raw success facts, with CP-only fields, and no opaque evidence aliases", async () => {
  const f = fixture();
  try {
    f.db.patches(f.p.compatible, [patch("old", ["cp-good"])], ["cp-good"]);
    let checked = false;
    await updateExperience(
      {
        generate: async (_system: string, payload: any) => {
          const fact = payload.trial_facts.find(
            (x: any) => x.outcome.day_minutes === 858,
          );
          assert.ok(fact);
          assert.equal(fact.stage, "cp_improve");
          assert.equal(fact.strategy.engine, undefined);
          assert.equal(fact.outcome.baseline_before_minutes, 854);
          assert.ok(payload.experience_cards[0].evidence_ids.includes(fact.id));
          checked = true;
          return {
            text: '{"patches":[]}',
            response_model: "fake",
            usage: {},
            stop_reason: "end_turn",
            wall_s: 0,
          };
        },
      },
      [],
      f.db.cards(f.p.compatible),
      {},
      f.db,
      f.p.compatible,
      true,
      new Events(join(f.dir, "events"), "run", performance.now()),
      new AbortController().signal,
      1,
      join(f.dir, "update.json"),
    );
    assert.ok(checked);
  } finally {
    f.close();
  }
});
test("credential guard reproduces ByteString error and rejects privately before network", () => {
  const bad = "、not-a-real-key";
  assert.throws(
    () => new Headers({ Authorization: "Bearer " + bad }),
    /ByteString/,
  );
  for (const x of [bad, " good", "good\nbad", "'abc'", "Bearer abc"]) {
    try {
      validateToken(x);
      assert.fail("accepted invalid token");
    } catch (e) {
      assert.ok(!String(e).includes(x));
      assert.match(String(e), /credential/);
    }
  }
  assert.equal(validateToken("sk-test-only"), "sk-test-only");
});

test("one malformed updater item does not discard a valid sibling; uncited claims are rejected", async () => {
  const f = fixture();
  try {
    const e = f.db.evidence("cp-good", f.p.compatible)!;
    const result = await updateExperience(
      {
        generate: async (_system: string, payload: any) => {
          const eid = payload.trial_facts[0].id;
          return {
            text: JSON.stringify({
              patches: [
                patch("valid-sibling", [eid]),
                { ...patch("overlong", [eid]), exceptions: ["x".repeat(301)] },
                {
                  ...patch("uncited", [eid]),
                  recommendation: "Based on E999, discard this approach.",
                },
              ],
              edit_assessments: [
                {
                  evidence_ids: [eid, eid, eid, eid, eid],
                  changed_component: "direction",
                  observation: "Too many references",
                  verdict: "confounded",
                },
              ],
            }),
            response_model: "fake",
            usage: {},
            stop_reason: "end_turn",
            wall_s: 0,
          };
        },
      },
      [{ ...e, context: e.profile.state }],
      [],
      {},
      f.db,
      f.p.compatible,
      true,
      new Events(join(f.dir, "events"), "run", performance.now()),
      new AbortController().signal,
      1,
      join(f.dir, "update.json"),
    );
    assert.equal(result.status, "model");
    assert.equal(result.changes.length, 1);
    assert.equal((result as any).rejected.length, 3);
    assert.deepEqual(
      f.db.cards(f.p.compatible).map((c) => c.id),
      ["valid-sibling"],
    );
  } finally {
    f.close();
  }
});
test("read-only Memory cannot write and statistics separate execution from success and gain", () => {
  const f = fixture();
  try {
    const ro = new Memory(join(f.dir, "memory.sqlite"), true);
    assert.equal(ro.retrieve(f.p, 8, "construct").length, 1);
    assert.throws(() => ro.use(["cp-good"], "t"), /readonly/i);
    ro.close();
    const out = summarizeSearch({
      memory_accounting: {
        executions: [
          { stage: "cp_improve", evidence_ids: ["cp-good"] },
          { stage: "cp_improve", evidence_ids: ["cp-good"] },
        ],
      },
      history: [
        {
          memory_stage: "cp_improve",
          reused_evidence_ids: ["cp-good"],
          valid: true,
          audit_valid: true,
          gain_minutes: 4,
          status: "FEASIBLE",
        },
        {
          memory_stage: "cp_improve",
          reused_evidence_ids: ["cp-good"],
          valid: false,
          gain_minutes: 0,
          status: "UNKNOWN",
        },
      ],
    });
    assert.deepEqual(out.memory.by_stage.cp_improve, {
      executed: 2,
      completed: 2,
      complete_valid: 1,
      improvements: 1,
      gain_minutes: 4,
    });
  } finally {
    f.close();
  }
});
