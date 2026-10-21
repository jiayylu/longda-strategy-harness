import { hash } from "./memory.js";

/** Explicit observational contrasts; single/uncontrolled runs never prove causal benefit. */
export function editContrast(
  parent: any,
  current: any,
  evidence: string,
  context: any,
) {
  const changes: Record<string, any> = {};
  for (const field of [
    "groups",
    "loading",
    "engine",
    "admission",
    "vehicle_policy",
    "algorithm_source",
    "program_source",
    "direction",
    "fraction",
    "focus_orders",
    "seeds",
    "policies",
  ]) {
    if (
      hash(parent?.strategy?.[field] ?? null) !==
      hash(current.strategy?.[field] ?? null)
    )
      changes[field] = field.endsWith("_source")
        ? {
            before_hash: hash(parent?.strategy?.[field] ?? null),
            after_hash: hash(current.strategy?.[field] ?? null),
          }
        : {
            before: parent?.strategy?.[field] ?? null,
            after: current.strategy?.[field] ?? null,
          };
  }
  return {
    parent_evidence_id: parent?.id ?? null,
    evidence_id: evidence,
    context_hash: hash(context),
    changes,
    parent_status: parent?.outcome?.status ?? null,
    current_status: current.outcome.status,
    parent_minutes: parent?.outcome?.day_minutes ?? null,
    current_minutes: current.outcome.day_minutes,
    observed_delta_minutes:
      parent?.outcome?.valid && current.outcome.valid
        ? current.outcome.day_minutes - parent.outcome.day_minutes
        : null,
    verdict: "confounded",
    reason: parent
      ? "Observational comparison; input, solver randomness, baseline and multiple edits may differ. Not a controlled causal test."
      : "No measured parent trial available; no effectiveness attribution.",
  };
}
