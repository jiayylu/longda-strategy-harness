import { z } from "zod";
export const Group = z
  .array(
    z
      .object({ route: z.string().min(1), count: z.number().int().positive() })
      .strict(),
  )
  .min(1);
export const Candidate = z
  .object({
    name: z.string().min(1).max(100),
    engine: z.enum(["legacy", "extended"]).default("legacy"),
    admission: z.enum(["batch", "rolling"]).default("batch"),
    vehicle_policy: z.enum(["same", "split"]).default("same"),
    backtrack_budget: z.number().int().min(0).max(32).default(0),
    wip_limit: z.number().int().min(1).max(30).default(10),
    program_source: z.string().max(10000).nullable().default(null),
    hypothesis: z.string().max(600),
    evidence_ids: z.array(z.string()).max(12).default([]),
    groups: z.array(Group).min(1),
    loading: z.enum(["grouped", "interleaved"]),
    policies: z.array(z.number().int().min(0).max(5)).min(1).max(6),
    seeds: z.array(z.number().int().min(0).max(1000000)).min(1).max(3),
    direction: z.enum([
      "broad",
      "press_gap",
      "vehicle_link",
      "furnace_batch",
      "day_boundary",
    ]),
    fraction: z.number().min(0.1).max(1),
    focus_orders: z.array(z.string()).default([]),
  })
  .strict();
export const Proposal = z
  .object({
    summary: z.string().max(1000),
    candidates: z.array(Candidate).min(1).max(8),
  })
  .strict();
export type Strategy = z.infer<typeof Candidate>;
export interface Scenario {
  name: string;
  ticks_per_minute: number;
  horizon_minutes: number;
  furnace_count: number;
  capacity: number;
  initial_temperature: number;
  ramp_up_deg_per_min: number;
  ramp_down_deg_per_min: number;
  transport: Record<string, unknown>;
  orders: {
    id: string;
    route: string;
    release_minutes: number;
    due_minutes: number;
  }[];
  routes: { id: string; operations: Record<string, number>[] }[];
  [k: string]: unknown;
}
export function parseProposal(text: string, sc: Scenario) {
  const raw = text
    .trim()
    .replace(/^```(?:json)?\s*/, "")
    .replace(/\s*```$/, "");
  const p = Proposal.parse(JSON.parse(raw));
  let expanded = 0;
  for (const c of p.candidates) {
    const counts = new Map<string, number>();
    for (const g of c.groups) {
      const used = new Set<string>();
      for (const x of g) {
        if (used.has(x.route)) throw Error("Duplicate route in one group");
        used.add(x.route);
        counts.set(x.route, (counts.get(x.route) ?? 0) + x.count);
      }
    }
    const expected = new Map<string, number>();
    sc.orders.forEach((o) =>
      expected.set(o.route, (expected.get(o.route) ?? 0) + 1),
    );
    if (
      counts.size !== expected.size ||
      [...expected].some(([r, n]) => counts.get(r) !== n)
    )
      throw Error("Every order must appear exactly once");
    if (c.focus_orders.some((id) => !sc.orders.some((o) => o.id === id)))
      throw Error("Unknown focus order");
    expanded += new Set(c.policies).size * new Set(c.seeds).size;
  }
  if (expanded > 64)
    throw Error("At most 64 constructor trials per model response");
  return p;
}
export function expand(c: Strategy, sc: Scenario) {
  const by = new Map<string, string[]>();
  sc.orders.forEach((o) => by.set(o.route, [...(by.get(o.route) ?? []), o.id]));
  const used = new Map<string, number>();
  const waves = c.groups.map((group) => {
    const segments = group.map((x) => {
      const a = used.get(x.route) ?? 0;
      used.set(x.route, a + x.count);
      return by.get(x.route)!.slice(a, a + x.count);
    });
    return c.loading === "grouped"
      ? segments.flat()
      : Array.from(
          { length: Math.max(...segments.map((s) => s.length)) },
          (_, j) => segments.flatMap((s) => (s[j] ? [s[j]] : [])),
        ).flat();
  });
  return [...new Set(c.policies)].flatMap((policy) =>
    [...new Set(c.seeds)].map((seed) => ({
      waves,
      policy,
      seed,
      engine: c.engine,
      admission: c.admission,
      vehicle_policy: c.vehicle_policy,
      backtrack_budget: c.backtrack_budget,
      wip_limit: c.wip_limit,
      program_source: c.program_source,
    })),
  );
}
