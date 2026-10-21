import { hash } from "./memory.js";
/** Physical state is an immutable input. Search revisions never advance factory time. */
export function liveSnapshot(
  physicalRevision: string,
  best: any,
  facts: any[],
  remaining: number,
  running: number,
) {
  const recent = facts.slice(-6).map((f) => ({
    evidence_id: f.id,
    source: f.outcome.source,
    valid: f.outcome.valid,
    day_minutes: f.outcome.day_minutes,
    failure: f.outcome.failure
      ? { kind: f.outcome.failure.kind, message: f.outcome.failure.message }
      : null,
  }));
  const feedback = facts
    .filter((f) => f.outcome.failure || f.outcome.quality_feedback)
    .slice(-3)
    .map((f) => ({
      evidence_id: f.id,
      failure: f.outcome.failure
        ? { kind: f.outcome.failure.kind, message: f.outcome.failure.message }
        : null,
      quality_feedback: f.outcome.quality_feedback
        ? {
            kind: f.outcome.quality_feedback.kind,
            message: f.outcome.quality_feedback.message,
          }
        : null,
    }));
  return {
    physical_revision: physicalRevision,
    search_revision: hash({
      best: best?.evidence_id ?? best?.evidence ?? best?.day_minutes,
      recent,
    }),
    best_complete: best
      ? {
          day_minutes: best.day_minutes,
          evidence_id: best.evidence_id ?? best.evidence,
          strategy_name: best.strategy?.name,
          strategy: compactStrategy(best.strategy),
          metrics: best.metrics,
          diagnostics: best.diagnostics,
        }
      : null,
    recent_results: recent,
    priority_feedback: feedback,
    remaining_run_s: remaining,
    running_tasks: running,
    instruction:
      "Physical orders/WIP are unchanged. These are current search results; read raw evidence before diagnosis.",
  };
}
export function requiresRefresh(before: any, after: any) {
  if (!before || !after) return false;
  if (before.physical_revision !== after.physical_revision) return true;
  // A stronger incumbent is a search observation, not a changed factory state.
  // CP launch binds the proposal to the newest audited incumbent.
  return false;
}
export function compactStrategy(strategy: any) {
  if (!strategy) return strategy;
  const { algorithm_source, program_source, ...rest } = strategy;
  return {
    ...rest,
    algorithm_hash: algorithm_source ? hash(algorithm_source) : null,
    program_hash: program_source ? hash(program_source) : null,
    source_note:
      "Full source is retained with the evidence; use read_evidence.",
  };
}

export function algorithmChange(
  before: string | undefined,
  after: string | undefined,
) {
  if (!after) return null;
  const a = (before ?? "").split("\n"),
    b = after.split("\n");
  let start = 0;
  while (start < a.length && start < b.length && a[start] === b[start]) start++;
  let tail = 0;
  while (
    tail < a.length - start &&
    tail < b.length - start &&
    a[a.length - 1 - tail] === b[b.length - 1 - tail]
  )
    tail++;
  return {
    parent_hash: before ? hash(before) : null,
    algorithm_hash: hash(after),
    changed: before !== after,
    from_line: start + 1,
    removed: a.slice(start, a.length - tail),
    added: b.slice(start, b.length - tail),
    interpretation:
      "Text change only; improvement must be established by measured trials.",
  };
}
