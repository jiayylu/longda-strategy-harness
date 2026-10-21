import type { Failure } from "./repair.js";
/** A preference/resource decision, never an infeasibility claim. */
export function qualityFeedback(
  result: any,
  baseline: number | null,
  baselineDiagnostics?: unknown,
): Failure | null {
  if (!result.valid || !result.audit_valid || baseline == null) return null;
  const gap = baseline - Number(result.day_minutes);
  if (!(gap > Math.max(10, baseline * 0.1))) return null;
  return {
    kind: "construction_quality",
    proof_scope: "none",
    message: `Complete legal candidate gives ${result.day_minutes} min, current baseline ${baseline} min; gap ${gap} min. Revise construction before further CP.`,
    details: {
      legal: true,
      candidate_minutes: result.day_minutes,
      baseline_minutes: baseline,
      gap_minutes: gap,
      threshold_minutes: Math.max(10, baseline * 0.1),
      candidate_diagnostics: result.diagnostics ?? null,
      baseline_diagnostics: baselineDiagnostics ?? null,
      instruction:
        "Not infeasible. Inspect grouping/admission and first-day idle gaps; compare with current baseline. Do not merely change CP seeds.",
    },
  };
}
export type CompletionLedger = {
  normal_reserved_s: number;
  partial_reserved_s: number;
  reservations: Record<string, number>;
};
export class CompletionPolicy {
  constructor(
    public ledger: CompletionLedger = {
      normal_reserved_s: 0,
      partial_reserved_s: 0,
      reservations: {},
    },
    readonly share = 0.2,
  ) {}
  admit(
    key: string,
    seconds: number,
    baseline: boolean,
    pending: number,
    running: number,
    recent: { attempts: number; valid: number } = { attempts: 0, valid: 0 },
  ) {
    if (pending + running >= 2) return "queue_capacity";
    if (this.ledger.reservations[key] != null) return null;
    // One initial probe credit. Thereafter <=share of reserved CP allowance, not task count.
    // After several unsuccessful probes, reduce total share to 5% with one initial credit.
    // Success restores the configured cap; UNKNOWN only affects resource allocation.
    const effectiveShare =
      recent.attempts >= 3 && recent.valid === 0
        ? Math.min(this.share, 0.05)
        : this.share;
    if (
      baseline &&
      this.ledger.partial_reserved_s + seconds >
        seconds +
          (effectiveShare / (1 - effectiveShare)) *
            this.ledger.normal_reserved_s
    )
      return effectiveShare < this.share
        ? "completion_low_yield_share"
        : "completion_resource_share";
    this.ledger.partial_reserved_s += seconds;
    this.ledger.reservations[key] = seconds;
    return null;
  }
  normal(seconds: number) {
    this.ledger.normal_reserved_s += seconds;
  }
}
export function hintProvenance(
  task: any,
  seed: string | undefined,
  best: string | undefined,
) {
  return {
    kind: task.completion
      ? "own_partial"
      : seed && seed === best
        ? "shared_incumbent"
        : seed
          ? "own_complete_candidate"
          : "none",
    path: task.completion ? task.partial_path : (seed ?? null),
    parent_task_id: task.parent_task_id ?? null,
    attribution:
      "Observed search result, not controlled proof of strategy advantage",
  };
}

/** Heuristic priorities never establish infeasibility: CP may revise all hints. */
export function completionValue(
  features: any,
  history: { attempts: number; valid: number },
  age = 0,
) {
  const coverage = Math.max(
    0,
    Math.min(
      1,
      (features.scheduled_forges ?? 0) /
        Math.max(1, features.total_operations ?? 1),
    ),
  );
  const conflicts =
    (features.window_conflicts ?? 0) + (features.press_conflicts ?? 0);
  const success = (history.valid + 1) / (history.attempts + 2);
  const slack = features.slack_min_minutes;
  const room = slack == null ? 0.5 : slack < 0 ? 0 : Math.min(1, slack / 120);
  return {
    priority:
      0.25 * coverage +
      0.3 / (1 + conflicts) +
      0.25 * success +
      0.2 * room +
      Math.min(0.2, Math.max(0, age) / 600),
    features,
    history,
    interpretation:
      "Ranking only; conflicting mutable hints may be repaired; low coverage is not a prune.",
  };
}
export class DiversityPolicy {
  constructor(public state: any = { seen: [], normal: 0, probe: 0 }) {}
  normal(seconds: number) {
    this.state.normal += seconds;
  }
  admit(signature: string, seconds: number) {
    if (this.state.seen.includes(signature)) return "duplicate_structure";
    if (this.state.probe + seconds > seconds + this.state.normal / 9)
      return "exploration_share";
    this.state.seen.push(signature);
    this.state.probe += seconds;
    return null;
  }
}
export function scheduleStructure(schedule: any) {
  const starts = schedule.forge_start ?? [];
  const order = starts
    .map((s: number, i: number) => ({ s, i }))
    .sort((a: any, b: any) => a.s - b.s || a.i - b.i)
    .map((x: any) => x.i);
  // Equality groups abstract interchangeable furnace labels; no times or timetable is stored in Memory.
  const ids = new Map();
  const groups = (schedule.furnace ?? []).map((f: any) => {
    if (!ids.has(f)) ids.set(f, ids.size);
    return ids.get(f);
  });
  return {
    press_order: order,
    furnace_partition: groups,
    transport_structure: Array.isArray(schedule.transport)
      ? [...schedule.transport]
          .sort(
            (a: any, b: any) =>
              a.start - b.start || a.pickup_rank - b.pickup_rank || a.id - b.id,
          )
          .map((t: any) => ({ op: t.op, kind: t.kind, vehicle: t.vehicle }))
      : null,
  };
}
