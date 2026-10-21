import { test } from "node:test";
import assert from "node:assert/strict";
import {
  completionDecision,
  completionFeedback,
} from "../src/partial_completion.js";
import { StrategyCycles } from "../src/strategy_cycles.js";
import { bootstrap } from "../src/learning.js";
import { readFileSync } from "node:fs";
const partial = {
  valid: false,
  partial_path: "part.json",
  failure: {
    kind: "construction_budget",
    message: "budget",
    proof_scope: "none" as const,
    details: { completed_operations: 31 },
  },
  partial_handoff: {
    eligible: true,
    content_hash: "sha",
    hint_values: 80,
    scheduled_forges: 31,
  },
};
test("only incomplete useful partials can enter completion; errors and bounds go to repair", () => {
  assert.equal(completionDecision(partial, true).eligible, true);
  for (const kind of [
    "program_error",
    "construction_error",
    "score_bound",
    "worker_error",
  ])
    assert.equal(
      completionDecision(
        { ...partial, failure: { ...partial.failure, kind } },
        true,
      ).eligible,
      false,
    );
  assert.equal(
    completionDecision({ ...partial, status: "AUDIT_FAILED" }, true).eligible,
    false,
  );
  assert.equal(
    completionDecision({ ...partial, partial_path: null }, true).eligible,
    false,
  );
  assert.equal(
    completionDecision({ ...partial, valid: true }, true).eligible,
    false,
  );
  assert.equal(completionDecision(partial, false).eligible, false);
});
test("completion UNKNOWN retains original failure and never becomes infeasibility", () => {
  const f = completionFeedback(
    partial.failure,
    { status: "UNKNOWN", hint_source: "current_partial", hint_values: 80 },
    "cp1",
  );
  assert.equal(f.kind, "construction_budget");
  assert.equal(f.proof_scope, "none");
  assert.equal((f.details as any).construction.completed_operations, 31);
  assert.equal((f.details as any).cp_completion.status, "UNKNOWN");
});
test("pending completion may run beyond ordinary batch quota and feedback waits for it", () => {
  const x = new StrategyCycles();
  x.open(1);
  const s = bootstrap(
    JSON.parse(readFileSync("configs/orders20.json", "utf8")),
  )[0];
  const id = x.add(1, 1, s, null, 0, 1, true);
  x.suspend(id, "partial_completion_pending");
  const completion = {
    campaign_id: id,
    action: "solve" as const,
    completion: { key: "p" },
  };
  assert.equal(x.allow(completion, [], false), true);
  assert.equal(x.allow({ campaign_id: id, action: "solve" }, [], false), false);
  assert.equal(x.closeIfReady([], [completion], 1, null), null);
  const saved = JSON.parse(JSON.stringify(x));
  const restored = new StrategyCycles(saved.campaigns, saved.cycles);
  assert.equal(restored.allow(completion, [], false), true);
  restored.suspend(id, "construction_repair");
  assert.equal(restored.allow(completion, [], false), false);
  assert.notEqual(restored.closeIfReady([], [], 2, null), null);
});
