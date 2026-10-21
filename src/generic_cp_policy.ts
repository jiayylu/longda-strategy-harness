import { hash } from "./memory.js";

/** Resource policy only: no feasibility pruning or global stopping claims. */
export type RetryContext = {
  baseline: string;
  hint: string | null;
  direction: string;
  fraction: number;
  focus_orders: string[];
  preference: string;
  configured_seconds: number;
  threads: number;
};
type Observation = {
  attempts: number;
  consecutive_no_gain: number;
  publications: number;
  cooldown_until_s: number;
  last_status?: string;
  last_evidence?: string;
  last_actual_key?: string;
  last_used_s: number;
};
type Family = Observation & { context: RetryContext };
type ActualScope = Observation & {
  baseline: string;
  hint: string | null;
  scope: string;
  released_orders: string[];
};
export type RetryState = {
  families: Record<string, Family>;
  actual_scopes: Record<string, ActualScope>;
  observations: number;
  cooldowns: number;
  resumed_probes: number;
};
export const emptyRetryState = (): RetryState => ({
  families: {},
  actual_scopes: {},
  observations: 0,
  cooldowns: 0,
  resumed_probes: 0,
});
const observation = (): Observation => ({
  attempts: 0,
  consecutive_no_gain: 0,
  publications: 0,
  cooldown_until_s: 0,
  last_used_s: 0,
});
export class GenericCpPolicy {
  constructor(
    readonly state: RetryState = emptyRetryState(),
    readonly noGainLimit = 3,
    readonly cooldownSeconds = 90,
  ) {}
  key(context: RetryContext) {
    // A new random seed is a probe, not a new neighborhood.
    return hash({
      ...context,
      focus_orders: [...context.focus_orders].sort(),
    });
  }
  decision(context: RetryContext, now: number) {
    const key = this.key(context);
    const family = this.state.families[key];
    const actual = family?.last_actual_key
      ? this.state.actual_scopes[family.last_actual_key]
      : undefined;
    const until = Math.max(
      family?.cooldown_until_s ?? 0,
      actual?.cooldown_until_s ?? 0,
    );
    return {
      key,
      ready: until <= now,
      cooldown_until_s: until,
      consecutive_no_gain: family?.consecutive_no_gain ?? 0,
      actual_scope_key: family?.last_actual_key ?? null,
    };
  }
  launched(context: RetryContext, now: number) {
    const d = this.decision(context, now);
    const resumed = d.ready && d.cooldown_until_s > 0;
    if (resumed) {
      this.state.resumed_probes++;
      const family = this.state.families[d.key];
      if (family) family.cooldown_until_s = 0;
      const actual = d.actual_scope_key
        ? this.state.actual_scopes[d.actual_scope_key]
        : undefined;
      if (actual && actual.cooldown_until_s <= now) actual.cooldown_until_s = 0;
    }
    return { ...d, resumed };
  }
  record(
    context: RetryContext,
    result: any,
    published: boolean,
    evidence: string,
    now: number,
  ) {
    if (
      !["UNKNOWN", "FEASIBLE", "OPTIMAL", "INFEASIBLE", "DUPLICATE"].includes(
        result.status,
      ) ||
      (!result.cp_solver_called && result.status !== "DUPLICATE")
    )
      return null;
    const key = this.key(context);
    const family = (this.state.families[key] ??= {
      ...observation(),
      context: { ...context, focus_orders: [...context.focus_orders].sort() },
    });
    let actualKey: string | undefined;
    let actual: ActualScope | undefined;
    if (
      ["local", "global"].includes(result.scope) &&
      Array.isArray(result.released_orders) &&
      result.released_orders.length
    ) {
      const released = [...new Set<string>(result.released_orders)].sort();
      actualKey = hash({
        baseline: context.baseline,
        hint: context.hint,
        scope: result.scope,
        released_orders: released,
        preference: context.preference,
        seconds: context.configured_seconds,
        threads: context.threads,
      });
      actual = this.state.actual_scopes[actualKey] ??= {
        ...observation(),
        baseline: context.baseline,
        hint: context.hint,
        scope: result.scope,
        released_orders: released,
      };
      family.last_actual_key = actualKey;
    }
    const success = published && result.valid && result.audit_valid;
    let cooling = false;
    for (const entry of [family, ...(actual ? [actual] : [])]) {
      entry.attempts++;
      entry.last_status = result.status;
      entry.last_evidence = evidence;
      entry.last_used_s = now;
      if (success) {
        entry.publications++;
        entry.consecutive_no_gain = 0;
        entry.cooldown_until_s = 0;
      } else {
        entry.consecutive_no_gain++;
        if (
          result.status === "DUPLICATE" ||
          entry.consecutive_no_gain >= this.noGainLimit
        ) {
          // Other in-flight tasks do not keep extending an existing cooldown.
          if (entry.cooldown_until_s <= now)
            entry.cooldown_until_s = now + this.cooldownSeconds;
          cooling = true;
        }
      }
    }
    this.state.observations++;
    if (cooling) this.state.cooldowns++;
    return {
      ...this.decision(context, now),
      cooling,
      baseline: context.baseline,
      hint: context.hint,
      direction: context.direction,
      fraction: context.fraction,
      focus_orders: context.focus_orders,
      released_orders: actual?.released_orders ?? null,
      evidence,
      status: result.status,
      published: Boolean(success),
      interpretation:
        "Temporary resource reallocation, not infeasibility or neighborhood exhaustion. Other baselines/hints/scopes remain eligible.",
    };
  }
}
