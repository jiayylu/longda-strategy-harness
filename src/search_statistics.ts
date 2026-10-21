export function actualSearch(task: any, result: any, totalOrders: number) {
  const requested = task.strategy?.direction;
  const released = result.released_orders ?? [];
  const kind =
    task.action === "construct"
      ? "construction"
      : task.completion
        ? "partial_completion"
        : result.scope === "global"
          ? "global_improvement"
          : result.scope === "local"
            ? "local_improvement"
            : "unclassified";
  return {
    task_kind: kind,
    requested_direction: requested,
    scope: result.scope ?? null,
    released_orders: released,
    released_order_count: released.length,
    total_orders: totalOrders,
    solver_called: !!result.cp_solver_called,
    scope_verified: !!result.scope,
  };
}
export function summarizeSearch(report: any) {
  const h = report.history ?? [];
  const status: Record<string, number> = {},
    kind: Record<string, any> = {};
  for (const t of h) {
    status[t.status] = (status[t.status] ?? 0) + 1;
    const k =
      t.actual_search?.task_kind ??
      (t.source === "construct" ? "construction" : "unclassified");
    const b = (kind[k] ??= {
      attempts: 0,
      solver_calls: 0,
      complete_valid: 0,
      improvements: 0,
      gain_minutes: 0,
      task_wall_s: 0,
      solver_wall_s: 0,
      estimated_slot_s: 0,
    });
    b.attempts++;
    b.solver_calls += Number(!!t.cp_solver_called);
    b.complete_valid += Number(!!(t.valid && t.audit_valid));
    b.improvements += Number(t.gain_minutes > 0);
    b.gain_minutes += t.gain_minutes ?? 0;
    b.task_wall_s += t.task_wall_s ?? 0;
    b.solver_wall_s += t.solver_wall_s ?? 0;
    b.estimated_slot_s += t.estimated_slot_s ?? 0;
  }
  return {
    memory: {
      ...report.memory_accounting,
      by_stage: Object.fromEntries(
        ["construct", "cp_complete", "cp_improve", "cp_feasibility"].map(
          (stage) => {
            const completed = h.filter(
              (x: any) =>
                x.memory_stage === stage && x.reused_evidence_ids?.length,
            );
            return [
              stage,
              {
                executed: (report.memory_accounting?.executions ?? []).filter(
                  (x: any) => x.stage === stage && x.evidence_ids.length,
                ).length,
                completed: completed.length,
                complete_valid: completed.filter(
                  (x: any) => x.valid && x.audit_valid,
                ).length,
                improvements: completed.filter(
                  (x: any) => x.valid && x.audit_valid && x.gain_minutes > 0,
                ).length,
                gain_minutes: completed.reduce(
                  (s: number, x: any) => s + (x.gain_minutes ?? 0),
                  0,
                ),
              },
            ];
          },
        ),
      ),
      attribution:
        "Retrieval/execution/success/improvement are separate observational counts, not causal benefit.",
    },
    schema_version: 1,
    completed_attempts: h.length,
    interrupted_attempts: report.interrupted_attempts ?? [],
    pending_tasks: report.pending_tasks ?? 0,
    complete_valid_results: h.filter((x: any) => x.valid && x.audit_valid)
      .length,
    incumbent_publications: (report.publications ?? []).length,
    publications: report.publications ?? [],
    status_counts: status,
    by_actual_work: kind,
    cp_calls_include_partial_completion: true,
    model_tool_results: (report.algorithm_tool_trials ?? []).length,
    timing_note:
      "Task and solver durations are sums over overlapping tasks, not elapsed run time. Estimated slot seconds are task wall time times configured threads, NOT measured CPU time.",
    measured_cpu_seconds: null,
    planner_delivery: report.planner_delivery ?? null,
    auxiliary_outcomes: report.auxiliary_outcomes ?? [],
    task_pool: report.task_pool ?? null,
    generic_cp_retry: report.generic_cp_retry
      ? {
          observations: report.generic_cp_retry.observations,
          cooldowns: report.generic_cp_retry.cooldowns,
          resumed_probes: report.generic_cp_retry.resumed_probes,
          requested_families: Object.keys(report.generic_cp_retry.families)
            .length,
          actual_scopes: Object.keys(report.generic_cp_retry.actual_scopes)
            .length,
          cooling_families_at_stop: Object.values(
            report.generic_cp_retry.families,
          ).filter((x: any) => x.cooldown_until_s > (report.elapsed_s ?? 0))
            .length,
          interpretation:
            "Resource cooldown, not infeasible pruning or global early stop. Full ledger is in report/checkpoint.",
        }
      : null,
    edit_contrast_count: h.filter((x: any) => x.edit_contrast).length,
  };
}
