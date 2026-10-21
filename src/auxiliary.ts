import { memoryStage, stageStrategy } from "./memory_stage.js";
/** Program-only pruning shared by two independent model roles. */
export function compactFacts(facts: any[], limit = 16) {
  return facts.slice(-limit).map((f) => ({
    id: f.id,
    stage: f.stage ?? memoryStage(f.outcome),
    context: f.context ?? f.outcome.production_context,
    strategy: stageStrategy(
      {
        name: f.strategy.name,
        engine: f.strategy.engine,
        groups: f.strategy.groups,
        policies: f.strategy.policies,
        seeds: f.strategy.seeds,
        cp_direction: f.strategy.direction,
        fraction: f.strategy.fraction,
        focus_orders: f.strategy.focus_orders,
        admission: f.strategy.admission,
        vehicle_policy: f.strategy.vehicle_policy,
        backtrack_budget: f.strategy.backtrack_budget,
        wip_limit: f.strategy.wip_limit,
        executable_algorithm: Boolean(f.strategy.algorithm_source),
        algorithm_hash: f.outcome.algorithm_hash,
      },
      f.stage ?? memoryStage(f.outcome),
    ),
    outcome: {
      status: f.outcome.status,
      strategy_id: f.outcome.strategy_id,
      model_round: f.outcome.model_round,
      parent_task_id: f.outcome.parent_task_id,
      allocated_seconds: f.outcome.allocated_seconds,
      effective_direction: f.outcome.effective_direction,
      effective_fraction: f.outcome.effective_fraction,
      source: f.outcome.source,
      audit_valid: f.outcome.audit_valid,
      purpose: f.outcome.purpose,
      actual_search: f.outcome.actual_search,
      baseline_at_publication_minutes:
        f.outcome.baseline_at_publication_minutes,
      completion_features: f.outcome.completion_features,
      completion_family: f.outcome.completion_family,
      valid: f.outcome.valid,
      baseline_before_minutes: f.outcome.baseline_before_minutes,
      day_minutes: f.outcome.day_minutes,
      gain_minutes: f.outcome.gain_minutes,
      cost: f.outcome.cost,
      error: f.outcome.error,
      failures: f.outcome.failures,
      failure: f.outcome.failure
        ? {
            kind: f.outcome.failure.kind,
            message: f.outcome.failure.message,
            proof_scope: "none",
          }
        : null,
      effective_search_key: f.outcome.effective_search_key,
      hint_provenance: f.outcome.hint_provenance,
      causal_status: f.outcome.causal_status ?? "observational",
      edit_contrast: f.outcome.edit_contrast ?? null,
      quality_feedback: f.outcome.quality_feedback,
    },
  }));
}
