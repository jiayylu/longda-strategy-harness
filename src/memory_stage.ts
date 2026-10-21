/** Classification is based on executed work, never the model's strategy name. */
export const MEMORY_STAGES = [
  "construct",
  "cp_complete",
  "cp_improve",
  "cp_feasibility",
  "unclassified",
] as const;
export type MemoryStage = (typeof MEMORY_STAGES)[number];
export function memoryStage(x: any): MemoryStage {
  if (
    x.source === "construct" ||
    x.source === "constructor" ||
    x.status === "AUDITED" ||
    x.actual_search?.task_kind === "construction"
  )
    return "construct";
  if (
    x.purpose === "partial_complete" ||
    x.phase === "partial_completion" ||
    x.completion_only ||
    x.actual_search?.task_kind === "partial_completion"
  )
    return "cp_complete";
  if (
    x.source === "solve" ||
    x.cp_solver_called ||
    x.actual_search?.solver_called
  ) {
    return x.phase === "cold_start" && x.baseline_before_minutes == null
      ? "cp_feasibility"
      : "cp_improve";
  }
  return "unclassified";
}
export function stageSuccess(x: any, stage = memoryStage(x)) {
  const valid = x.valid === true && x.audit_valid === true;
  return stage === "cp_improve" ? valid && x.gain_minutes > 0 : valid;
}
/** Do not put constructor knobs in the evidence for a CP improvement claim. */
export function stageStrategy(s: any, stage: MemoryStage) {
  if (stage === "construct") {
    const { direction, cp_direction, fraction, focus_orders, ...construction } =
      s;
    return construction;
  }
  return {
    name: s.name,
    evidence_ids: s.evidence_ids,
    direction: s.direction ?? s.cp_direction,
    fraction: s.fraction,
    focus_orders: s.focus_orders,
    seeds: s.seeds,
    attribution:
      "CP execution only; constructor fields do not establish causality",
  };
}
