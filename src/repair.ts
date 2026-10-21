import type { Strategy } from "./contracts.js";
import { hash } from "./memory.js";

export type Failure = {
  kind: string;
  message: string;
  proof_scope: "none";
  details: unknown;
};
export type RepairTicket = {
  id: string;
  campaign_id: string;
  root_campaign_id: string;
  task_id: string;
  evidence_id: string;
  strategy: Strategy;
  failure: Failure;
  state: "pending" | "running" | "answered" | "exhausted";
  calls: number;
  response_call?: number;
  children: string[];
  validation_feedback?: {
    kind: string;
    message: string;
    response_excerpt?: string;
  };
};

/** Failures retain their tool identity; CP UNKNOWN cannot replace a constructor error. */
export function constructionFailure(result: any): Failure | null {
  if (result.valid && result.audit_valid) return null;
  if (result.failure) return result.failure;
  const row = (result.rows ?? []).find((r: any) => r.error);
  return {
    kind:
      result.status === "WORKER_ERROR"
        ? "worker_error"
        : "construction_deadend",
    message: String(
      result.error ??
        row?.error ??
        result.status ??
        "No complete audited construction",
    ),
    proof_scope: "none",
    details: { rows: result.rows ?? [], errors: result.errors ?? [] },
  };
}

/** Durable repair inbox. A failed lineage has a bounded model budget, independent of normal rounds. */
export class Repairs {
  constructor(public tickets: RepairTicket[] = []) {
    // Explicit run resume may retry a read-only model request; its old outcome is not treated as success.
    for (const t of tickets) if (t.state === "running") t.state = "pending";
  }
  root(campaign: string): string {
    return (
      this.tickets.find((t) => t.children.includes(campaign))
        ?.root_campaign_id ?? campaign
    );
  }
  request(
    campaign: string,
    task: string,
    evidence: string,
    strategy: Strategy,
    failure: Failure,
  ) {
    const old = this.tickets.find(
      (t) =>
        t.campaign_id === campaign && ["pending", "running"].includes(t.state),
    );
    if (old) return old;
    const t: RepairTicket = {
      id: `repair-${this.tickets.length + 1}`,
      campaign_id: campaign,
      root_campaign_id: this.root(campaign),
      task_id: task,
      evidence_id: evidence,
      strategy,
      failure,
      state: "pending",
      calls: 0,
      children: [],
    };
    this.tickets.push(t);
    return t;
  }
  next(maxCalls: number): RepairTicket | undefined {
    for (const t of this.tickets) {
      if (!["pending", "exhausted"].includes(t.state)) continue;
      const spent = this.tickets
        .filter((x) => x.root_campaign_id === t.root_campaign_id)
        .reduce((sum, x) => sum + x.calls, 0);
      if (spent >= maxCalls) {
        t.state = "exhausted";
        continue;
      }
      // An explicit resumed run may raise the quota; no autonomous restart is performed.
      t.state = "pending";
      return t;
    }
  }
  unresolved() {
    return this.tickets.filter((t) => ["pending", "running"].includes(t.state));
  }
}

/** CP-only settings do not fix construction; exclude them when detecting unchanged repair proposals. */
export function constructionKey(s: Strategy) {
  const {
    name,
    hypothesis,
    evidence_ids,
    direction,
    fraction,
    focus_orders,
    ...settings
  } = s;
  return hash(settings);
}
