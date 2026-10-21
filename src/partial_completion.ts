import type { Failure } from "./repair.js";

export type CompletionTask = {
  key: string;
  source_evidence: string;
  failure: Failure;
  partial_hash: string;
};

/** Only actual worker-produced partials are eligible. Program/audit errors never auto-complete. */
export function completionDecision(result: any, enabled: boolean) {
  const allowed = new Set([
    "construction_deadend",
    "construction_budget",
    "resource_budget",
  ]);
  if (!enabled) return { eligible: false, reason: "completion_disabled" };
  if (
    result.status === "AUDIT_FAILED" ||
    result.rows?.some(
      (r: any) =>
        r.status === "audit_failed" ||
        r.status === "program_or_construction_error",
    )
  )
    return { eligible: false, reason: "failure_requires_repair" };
  if (result.valid) return { eligible: false, reason: "already_complete" };
  if (!allowed.has(result.failure?.kind))
    return { eligible: false, reason: "failure_requires_repair" };
  const p = result.partial_handoff;
  if (
    !result.partial_path ||
    !p?.eligible ||
    !p.content_hash ||
    !(p.hint_values > 0)
  )
    return { eligible: false, reason: "no_usable_partial" };
  return { eligible: true, reason: "mutable_partial_hint" };
}

export function completionFeedback(
  original: Failure,
  result: any,
  evidence: string,
): Failure {
  return {
    ...original,
    message:
      original.message +
      "; partial CP completion: " +
      String(result.status ?? "OUTCOME_UNKNOWN"),
    details: {
      construction: original.details,
      cp_completion: {
        evidence_id: evidence,
        status: result.status ?? "OUTCOME_UNKNOWN",
        valid: Boolean(result.valid && result.audit_valid),
        proof_scope: result.proof_scope ?? "none",
        hint_source: result.hint_source,
        hint_values: result.hint_values,
        solver_wall_s: result.solver_wall_s,
        error: result.error,
        interpretation:
          "Hints may move. UNKNOWN is not infeasibility; no baseline-improvement threshold was required. Do not infer that the original partial was extendable unchanged.",
      },
    },
  };
}
