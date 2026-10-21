import { z } from "zod";
import { hash } from "./memory.js";
import type { Strategy, Scenario } from "./contracts.js";
import { Candidate } from "./contracts.js";
import { MEMORY_STAGES } from "./memory_stage.js";
export function strategyKey(s: Strategy) {
  const { name, hypothesis, evidence_ids, ...algorithm } = s;
  return hash(algorithm);
}
export function constructionArm(strategy: Strategy) {
  return `construct:${strategy.engine}:${strategy.admission}:${strategy.vehicle_policy}:${strategy.algorithm_source || strategy.program_source ? hash(strategy.algorithm_source ?? strategy.program_source).slice(0, 10) : "builtin"}`;
}
export const CardPatch = z
  .object({
    op: z.enum(["add", "update", "retire"]),
    id: z.string().min(1).max(100),
    expected_version: z.number().int().min(0),
    conditions: z.array(z.string().max(300)).max(8),
    recommendation: z.string().max(1000),
    exceptions: z.array(z.string().max(300)).max(8),
    evidence_ids: z.array(z.string()).min(1).max(20),
    stage: z.enum(MEMORY_STAGES).optional(),
    retirement: z
      .object({
        reason: z.enum(["superseded", "evidence_error", "scope_obsolete"]),
        explanation: z.string().min(30).max(1500),
        evidence_ids: z.array(z.string()).min(1).max(20),
        replacement_id: z.string().optional(),
        // Terminal outcome records alone cannot prove factual corruption or obsolete rules.
        verified_issue_ids: z.array(z.string()).max(10).optional(),
      })
      .strict()
      .optional(),
  })
  .strict();
export type Arm = {
  attempts: number;
  valid: number;
  improvements: number;
  gain: number;
  cost: number;
  recent: number[];
  failures: number;
  observations?: { reward: number; cost: number }[];
};
/** Resource-share scheduling. Exploitation rates are normalized BEFORE exploration. */
export class Allocator {
  lastDecision: any = null;
  constructor(
    public arms: Record<string, Arm> = {},
    public selections = 0,
    public allocations: Record<string, number> = {},
    readonly exploration = 0.15,
  ) {}
  update(
    key: string,
    valid: boolean,
    gain: number,
    cost: number,
    feasibility = false,
  ) {
    const a = (this.arms[key] ??= {
      attempts: 0,
      valid: 0,
      improvements: 0,
      gain: 0,
      cost: 0,
      recent: [],
      failures: 0,
    });
    a.attempts++;
    a.valid += Number(valid);
    a.improvements += Number(gain > 0);
    a.gain += Math.max(0, gain);
    a.cost += Math.max(0.001, cost);
    const reward = feasibility ? Number(valid) : Math.max(0, gain);
    a.observations = [
      ...(a.observations ?? []),
      { reward, cost: Math.max(0.001, cost) },
    ].slice(-24);
    a.recent = a.observations.map((o) => o.reward / o.cost);
    a.failures = gain > 0 ? 0 : a.failures + 1;
  }
  settle(key: string, reserved: number, actual: number) {
    this.allocations[key] = Math.max(
      0,
      (this.allocations[key] ?? 0) + actual - reserved,
    );
  }
  pick<T extends { arm: string; estimated_cost?: number; priority?: number }>(
    queue: T[],
  ): number {
    this.selections++;
    const keys = [...new Set(queue.map((x) => x.arm))],
      rates: Record<string, number> = {};
    for (const k of keys) {
      const o = this.arms[k]?.observations ?? [];
      rates[k] =
        o.reduce((s, x) => s + x.reward, 0) /
        Math.max(
          0.001,
          o.reduce((s, x) => s + x.cost, 0),
        );
    }
    const sum = Object.values(rates).reduce((s, x) => s + x, 0);
    const shares = Object.fromEntries(
      keys.map((k) => [
        k,
        sum > 0
          ? ((1 - this.exploration) * rates[k]) / sum +
            this.exploration / keys.length
          : 1 / keys.length,
      ]),
    );
    // Completed resource use plus reservations includes in-flight work; inactive arms do not block the queue.
    const total = keys.reduce((s, k) => s + (this.allocations[k] ?? 0), 0);
    const avg =
      queue.reduce((s, x) => s + Math.max(0.001, x.estimated_cost ?? 1), 0) /
      queue.length;
    const unseen = keys.find((k) => !(this.allocations[k] > 0));
    const best =
      unseen ??
      keys.reduce((a, b) =>
        shares[b] * (total + avg) - (this.allocations[b] ?? 0) >
        shares[a] * (total + avg) - (this.allocations[a] ?? 0)
          ? b
          : a,
      );
    const candidates = queue
      .map((x, i) => ({ x, i }))
      .filter(({ x }) => x.arm === best);
    const chosen = candidates.reduce((a, b) =>
      (b.x.priority ?? 0) > (a.x.priority ?? 0) ? b : a,
    );
    const reserved = Math.max(0.001, chosen.x.estimated_cost ?? 1);
    this.allocations[best] = (this.allocations[best] ?? 0) + reserved;
    this.lastDecision = {
      arm: best,
      shares,
      rates,
      exploration: this.exploration,
      reserved_slot_s: reserved,
      method: "normalized_rate_resource_deficit",
      measured_cpu: false,
    };
    return chosen.i;
  }
}
export function schedulingArm(
  task: any,
  hasBaseline: boolean,
  totalOrders: number,
) {
  if (task.action === "construct")
    return `${hasBaseline ? "improve" : "feasibility"}:${constructionArm(task.strategy)}`;
  if (task.completion) return "cp:partial_completion";
  if (task.exploratory_quality_probe) return "cp:diverse_probe";
  if (!hasBaseline) return "cp:feasibility";
  const s = task.strategy;
  const global =
    s.direction === "broad" ||
    (!s.focus_orders?.length && s.fraction >= 1) ||
    s.focus_orders?.length >= totalOrders;
  return global
    ? "cp:global_improvement"
    : `cp:local:${s.direction}:${s.focus_orders?.length || Math.ceil(s.fraction * totalOrders)}`;
}
export function bootstrap(sc: Scenario): Strategy[] {
  const routes = sc.routes.filter((r) =>
    sc.orders.some((o) => o.route === r.id),
  );
  const n = (r: string) => sc.orders.filter((o) => o.route === r).length;
  const sorted = [...routes].sort((a, b) => {
    const ratio = (r: typeof a) =>
      r.operations.reduce((v, q) => v + q.forge, 0) /
      r.operations.reduce((v, q) => v + q.hold_min + q.forge, 0);
    return ratio(b) - ratio(a);
  });
  const permute = (x: typeof sorted): (typeof sorted)[] =>
    x.length <= 1
      ? [x]
      : x.flatMap((r, i) =>
          permute(x.filter((_, j) => i !== j)).map((t) => [r, ...t]),
        );
  const permutations =
    sorted.length <= 4
      ? permute(sorted)
      : [
          sorted,
          ...sorted.map((_, i) => [...sorted.slice(i), ...sorted.slice(0, i)]),
          [...sorted].reverse(),
        ];
  const result: Strategy[] = [];
  // Reuse the old single-car algorithmic idea (mixed-route small batches),
  // deriving every order and quantity from this input; never load a saved timetable.
  const spread = Array.from(
    { length: Math.max(...routes.map((r) => n(r.id))) },
    (_, j) => routes.filter((r) => n(r.id) > j).map((r) => r.id),
  ).flat();
  for (const width of [Math.min(4, routes.length), 3, 5]) {
    const groups: { route: string; count: number }[][] = [];
    for (let i = 0; i < spread.length; i += width) {
      const counts = new Map<string, number>();
      for (const r of spread.slice(i, i + width))
        counts.set(r, (counts.get(r) ?? 0) + 1);
      groups.push([...counts].map(([route, count]) => ({ route, count })));
    }
    for (const engine of ["legacy", "extended"])
      result.push(
        Candidate.parse({
          name: `mixed-routes-${width}-${engine}`,
          hypothesis:
            "Small mixed-route groups reduce concurrent transport pressure; a generic control from the earlier one-car constructor, not a saved solution.",
          groups,
          loading: "interleaved",
          policies: [0, 1, 2, 3, 4, 5],
          seeds: [0],
          direction: "broad",
          fraction: 1,
          engine,
          admission: "batch",
          vehicle_policy: "same",
          wip_limit: width,
        }),
      );
  }
  for (const [idx, order] of permutations.entries())
    for (const chunk of [5, 3]) {
      const groups: { route: string; count: number }[][] = [];
      for (let k = 0; k < order.length; k += 2) {
        const pair = order.slice(k, k + 2);
        for (
          let off = 0;
          off < Math.max(...pair.map((r) => n(r.id)));
          off += chunk
        )
          groups.push(
            pair.flatMap((r) =>
              n(r.id) > off
                ? [{ route: r.id, count: Math.min(chunk, n(r.id) - off) }]
                : [],
            ),
          );
      }
      for (const variant of [
        "legacy",
        "rolling",
        "split",
        "backtrack",
      ] as const)
        result.push(
          Candidate.parse({
            name: `bootstrap-${idx}-${chunk}-${variant}`,
            hypothesis:
              "Generic route-based grouping; must be computed and audited on this input.",
            groups,
            loading: "interleaved",
            policies: [idx % 5],
            seeds: [0],
            direction: [
              "broad",
              "press_gap",
              "vehicle_link",
              "furnace_batch",
              "day_boundary",
            ][idx % 5],
            fraction: 0.4,
            engine: variant === "legacy" ? "legacy" : "extended",
            admission: variant === "legacy" ? "batch" : "rolling",
            vehicle_policy: ["split", "backtrack"].includes(variant)
              ? "split"
              : "same",
            backtrack_budget: variant === "backtrack" ? 8 : 0,
            wip_limit: chunk * 2,
          }),
        );
    }
  const seen = new Set<string>();
  return result.filter((x) => {
    const key = strategyKey(x);
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}
