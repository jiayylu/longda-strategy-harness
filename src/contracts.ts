import { z } from "zod";
import { modelJson } from "./model_json.js";
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
    engine: z.enum(["legacy", "extended", "calendar"]).default("legacy"),
    admission: z.enum(["batch", "rolling"]).default("batch"),
    vehicle_policy: z.enum(["same", "split"]).default("same"),
    backtrack_budget: z.number().int().min(0).max(32).default(0),
    wip_limit: z.number().int().min(1).max(30).default(10),
    program_source: z.string().max(10000).nullable().default(null),
    algorithm_source: z.string().max(20000).nullable().default(null),
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
export function parseProposal(
  text: string,
  sc: Scenario,
  onNormalize?: (fields: string[]) => void,
) {
  const data = modelJson(text) as any;
  const normalized: string[] = [];
  // Only presentation text is clipped. Executable settings, source and evidence remain strict.
  // The caller saves the full response before parsing so no original explanation is lost.
  const clip = (obj: any, key: string, limit: number, path: string) => {
    if (obj && typeof obj[key] === "string" && obj[key].length > limit) {
      obj[key] = obj[key].slice(0, limit - 1) + "…";
      normalized.push(path);
    }
  };
  clip(data, "summary", 1000, "summary");
  if (Array.isArray(data?.candidates))
    data.candidates.forEach((c: any, i: number) => {
      clip(c, "name", 100, `candidates[${i}].name`);
      clip(c, "hypothesis", 600, `candidates[${i}].hypothesis`);
    });
  const p = Proposal.parse(data);
  if (normalized.length) onNormalize?.(normalized);
  let expanded = 0;
  for (const c of p.candidates) {
    if (c.algorithm_source && c.engine !== "calendar")
      throw Error("algorithm_source requires engine=calendar");
    if (c.algorithm_source && c.program_source)
      throw Error(
        "Use one full construction algorithm or one legacy rank program",
      );
    const counts = new Map<string, number>();
    for (const g of c.groups) {
      const used = new Set<string>();
      for (const x of g) {
        if (used.has(x.route))
          throw Error(
            JSON.stringify({
              code: "DUPLICATE_ROUTE_IN_GROUP",
              candidate: c.name,
              group_index: c.groups.indexOf(g),
              route: x.route,
            }),
          );
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
      throw Error(
        JSON.stringify({
          code: "ORDER_COUNT_MISMATCH",
          candidate: c.name,
          routes: [...new Set([...expected.keys(), ...counts.keys()])]
            .map((route) => ({
              route,
              expected: expected.get(route) ?? 0,
              actual: counts.get(route) ?? 0,
              delta: (counts.get(route) ?? 0) - (expected.get(route) ?? 0),
            }))
            .filter((x) => x.delta !== 0 || !expected.has(x.route)),
          instruction:
            "Correct the groups for the CURRENT orders. Positive delta means excess, negative means missing; do not change the scenario.",
        }),
      );
    if (c.focus_orders.some((id) => !sc.orders.some((o) => o.id === id)))
      throw Error(
        JSON.stringify({
          code: "UNKNOWN_FOCUS_ORDERS",
          candidate: c.name,
          invalid_ids: c.focus_orders.filter(
            (id) => !sc.orders.some((o) => o.id === id),
          ),
        }),
      );
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
      algorithm_source: c.algorithm_source,
    })),
  );
}
