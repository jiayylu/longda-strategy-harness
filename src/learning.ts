import { z } from "zod";
import { hash } from "./memory.js";
import type { Strategy, Scenario } from "./contracts.js";
import { Candidate } from "./contracts.js";
export function strategyKey(s: Strategy) {
  const { name, hypothesis, evidence_ids, ...algorithm } = s;
  return hash(algorithm);
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
  })
  .strict();
export const AuxiliaryAnswer = z
  .object({
    summary: z.string().max(2400),
    evidence_ids: z.array(z.string()).max(40),
    patches: z.array(CardPatch).max(8),
    narration: z.string().max(500).default(""),
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
};
/** UCB with an explicit exploration floor; separate construction/CP arms. */
export class Allocator {
  constructor(
    public arms: Record<string, Arm> = {},
    public selections = 0,
    public allocations: Record<string, number> = {},
  ) {}
  update(key: string, valid: boolean, gain: number, cost: number) {
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
    a.recent = [...a.recent, Math.max(0, gain) / Math.max(0.001, cost)].slice(
      -12,
    );
    a.failures = gain > 0 ? 0 : a.failures + 1;
  }
  pick<T extends { arm: string }>(queue: T[]): number {
    this.selections++;
    const count = (key: string) => this.allocations[key] ?? 0;
    const untried = queue.findIndex((x) => count(x.arm) === 0);
    let index = untried;
    if (index < 0 && this.selections % 5 === 0)
      index = queue.reduce(
        (best, x, i) => (count(x.arm) < count(queue[best].arm) ? i : best),
        0,
      );
    if (index < 0) {
      const value = (key: string) => {
        const a = this.arms[key];
        const mean = a
          ? a.recent.reduce((x, y) => x + y, 0) / Math.max(1, a.recent.length)
          : 0;
        return (
          mean +
          Math.sqrt(
            (2 * Math.log(this.selections + 1)) / Math.max(1, count(key)),
          )
        );
      };
      index = queue.reduce(
        (best, x, i) => (value(x.arm) > value(queue[best].arm) ? i : best),
        0,
      );
    }
    const key = queue[index].arm;
    this.allocations[key] = count(key) + 1;
    return index;
  }
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
