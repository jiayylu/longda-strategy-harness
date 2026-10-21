import type { Strategy } from "./contracts.js";

export type StrategyTrial = {
  id: string;
  parent_task_id?: string;
  action: "construct" | "solve";
  purpose?: "construct" | "improve" | "partial_complete";
  status: string;
  valid: boolean;
  audit_valid: boolean;
  baseline_before_minutes: number | null;
  day_minutes: number | null;
  gain_minutes: number;
  wall_s: number;
  cpu_slot_seconds: number;
  direction: string;
  fraction: number;
  seed: number;
  allowance_s: number;
  proof_scope: string;
  policy?: number;
  engine?: string;
  admission?: string;
  program_hash?: string | null;
  error?: string;
  failure?: any;
  effective_search_key?: string;
  duplicate_of?: any;
  hint_provenance?: any;
};
export type Campaign = {
  id: string;
  round: number;
  name: string;
  strategy: Strategy;
  baseline_minutes: number | null;
  selected_at_s: number;
  required_trials: number;
  required_cp_trials: number;
  next_seed: number;
  trials: StrategyTrial[];
  blocked_reason?: string;
  repaired_from?: string;
};
export type Cycle = {
  round: number;
  campaigns: string[];
  closed: boolean;
  feedback?: any;
};
export type CycleTask = {
  completion?: unknown;
  campaign_id?: string;
  action: "construct" | "solve";
};

/** Group descendants under an immutable proposal identity, independently of retry seeds. */
export class StrategyCycles {
  constructor(
    public campaigns: Record<string, Campaign> = {},
    public cycles: Cycle[] = [],
  ) {}
  open(round: number) {
    this.cycles.push({ round, campaigns: [], closed: false });
  }
  add(
    round: number,
    index: number,
    strategy: Strategy,
    baseline: number | null,
    now: number,
    trials: number,
    cpEnabled: boolean,
  ) {
    const id = `round-${round}/strategy-${index}`;
    this.campaigns[id] = {
      id,
      round,
      name: strategy.name,
      strategy,
      baseline_minutes: baseline,
      selected_at_s: now,
      required_trials: trials,
      required_cp_trials: 0, // CP is earned by competitive construction, not a compulsory quota.
      next_seed: strategy.seeds[0],
      trials: [],
    };
    this.cycles.find((c) => c.round === round)!.campaigns.push(id);
    return id;
  }
  recordTool(
    id: string,
    strategy: Strategy,
    round: number,
    trial: StrategyTrial,
  ) {
    if (!this.campaigns[id])
      this.campaigns[id] = {
        id,
        round,
        name: strategy.name,
        strategy,
        baseline_minutes: trial.baseline_before_minutes,
        selected_at_s: 0,
        required_trials: 0,
        required_cp_trials: 0,
        next_seed: 0,
        trials: [],
        blocked_reason: "tool_trial_only",
      };
    const cycle = this.cycles.find((c) => c.round === round);
    if (cycle && !cycle.campaigns.includes(id)) cycle.campaigns.push(id);
    if (!this.campaigns[id].trials.some((t) => t.id === trial.id))
      this.campaigns[id].trials.push(trial);
  }
  remove(id: string) {
    const c = this.campaigns[id];
    if (c)
      this.cycles
        .find((x) => x.round === c.round)!
        .campaigns.splice(
          this.cycles.find((x) => x.round === c.round)!.campaigns.indexOf(id),
          1,
        );
    delete this.campaigns[id];
  }
  suspend(id: string, reason: string) {
    this.campaigns[id].blocked_reason = reason;
  }
  addRepair(
    parent: string,
    strategy: Strategy,
    baseline: number | null,
    now: number,
    trials: number,
    cp: boolean,
  ) {
    const round = this.campaigns[parent].round;
    const index =
      this.cycles.find((x) => x.round === round)!.campaigns.length + 1;
    const id = this.add(round, index, strategy, baseline, now, trials, cp);
    this.campaigns[id].repaired_from = parent;
    return id;
  }
  nextSeed(id: string) {
    const c = this.campaigns[id];
    const seed = c.next_seed;
    c.next_seed = (seed + 1) % 1000001;
    return seed;
  }
  record(id: string | undefined, trial: StrategyTrial) {
    if (id) this.campaigns[id].trials.push(trial);
  }
  latest() {
    return this.cycles.at(-1);
  }
  allow(task: CycleTask, running: CycleTask[], allowAfterFinalRound: boolean) {
    if (!task.campaign_id) return true;
    const c = this.campaigns[task.campaign_id];
    const cycle = this.cycles.find((x) => x.round === c.round)!;
    if (task.completion)
      return c.blocked_reason === "partial_completion_pending";
    if (c.blocked_reason) return false;
    if (cycle.closed) return allowAfterFinalRound;
    const active = running.filter((t) => t.campaign_id === c.id);
    if (c.trials.length + active.length >= c.required_trials) return false;
    // Reserve attempts for CP: many constructor variants must not consume the entire batch.
    const cp =
      c.trials.filter((t) => t.action === "solve").length +
      active.filter((t) => t.action === "solve").length;
    const reserve = Math.max(0, c.required_cp_trials - cp);
    return (
      task.action === "solve" ||
      c.trials.length + active.length < c.required_trials - reserve
    );
  }
  cpFailures(id: string) {
    let n = 0;
    for (const t of [...this.campaigns[id].trials].reverse()) {
      if (t.action !== "solve") continue;
      if (t.gain_minutes > 0) break;
      n++;
    }
    return n;
  }
  cpAllowance(id: string, base: number, cap: number) {
    return Math.min(base, cap); // UNKNOWN alone does not justify larger repeated searches.
  }
  summary(id: string) {
    const c = this.campaigns[id],
      counts: Record<string, number> = {};
    for (const t of c.trials) counts[t.status] = (counts[t.status] ?? 0) + 1;
    const audited = c.trials.filter((t) => t.valid && t.audit_valid);
    return {
      strategy_id: id,
      round: c.round,
      name: c.name,
      blocked_reason: c.blocked_reason,
      repaired_from: c.repaired_from,
      strategy: c.strategy,
      baseline_at_selection_minutes: c.baseline_minutes,
      required_trials: c.required_trials,
      required_cp_trials: c.required_cp_trials,
      completed_trials: c.trials.length,
      construction_trials: c.trials.filter((t) => t.action === "construct")
        .length,
      cp_trials: c.trials.filter((t) => t.action === "solve").length,
      completion_trials: c.trials.filter(
        (t) => t.purpose === "partial_complete",
      ).length,
      improvement_trials: c.trials.filter(
        (t) => t.action === "solve" && t.purpose !== "partial_complete",
      ).length,
      audited_valid_trials: audited.length,
      best_candidate_minutes: audited.length
        ? Math.max(...audited.map((t) => t.day_minutes!))
        : null,
      published_gain_minutes: c.trials.reduce((v, t) => v + t.gain_minutes, 0),
      summed_attempt_wall_s: c.trials.reduce((v, t) => v + t.wall_s, 0),
      cpu_slot_seconds: c.trials.reduce((v, t) => v + t.cpu_slot_seconds, 0),
      statuses: counts,
      evidence_ids: c.trials.map((t) => t.id),
      recent_attempts: c.trials.slice(-12),
      interpretation:
        "Published gain is credit against the shared baseline at publication; per-attempt baselines and CPU-slot-seconds are recorded separately. UNKNOWN and construction failure do not prove infeasibility. Uncontrolled parallel comparisons do not establish causality.",
    };
  }
  closeIfReady(
    running: CycleTask[],
    queued: CycleTask[],
    now: number,
    sharedBest: number | null,
    roundErrors: unknown[] = [],
  ) {
    const cycle = this.latest();
    if (!cycle || cycle.closed) return null;
    const ids = new Set(cycle.campaigns);
    if (
      [...running, ...queued.filter((t) => t.completion)].some(
        (t) => t.campaign_id && ids.has(t.campaign_id),
      )
    )
      return null;
    const sufficient = (c: Campaign) =>
      Boolean(c.blocked_reason) ||
      (c.trials.length >= c.required_trials &&
        c.trials.filter((t) => t.action === "solve").length >=
          c.required_cp_trials);
    for (const id of ids) {
      if (
        !sufficient(this.campaigns[id]) &&
        queued.some((t) => t.campaign_id === id)
      )
        return null;
    }
    cycle.closed = true;
    cycle.feedback = {
      round: cycle.round,
      evaluated_at_s: now,
      shared_best_minutes: sharedBest,
      status: !ids.size
        ? "no_executable_strategy"
        : [...ids].every((id) => sufficient(this.campaigns[id]))
          ? "batch_evaluated"
          : "tasks_exhausted",
      strategies: [...ids].map((id) => this.summary(id)),
      round_errors: roundErrors,
    };
    return cycle.feedback;
  }
}
