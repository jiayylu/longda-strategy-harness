import { stageStrategy } from "./memory_stage.js";
import type { Scenario, Strategy } from "./contracts.js";
import { parseProposal } from "./contracts.js";

export interface ProductionFeatures {
  now_minutes: number;
  production_day: number;
  day_remaining_minutes: number;
  horizon_remaining_minutes: number;
  order_counts: Record<string, number>;
  remaining_operations: Record<string, number>;
  remaining_forge_minutes: Record<string, number>;
  cold_orders: number;
  active_orders: number;
  completed_orders: number;
  heating_by_temperature: Record<string, number>;
  furnace_temperature_counts: Record<string, number>;
  min_remaining_hold_minutes: number;
  min_upper_slack_minutes: number;
  urgent_heating: number;
  due_slack_min_minutes: number;
  free_slots: number;
  press_busy: boolean;
  vehicles: { location: number; busy_minutes: number }[];
}
export function stateDistance(a?: ProductionFeatures, b?: ProductionFeatures) {
  if (!a || !b)
    return { distance: 1, reasons: ["historical state was not recorded"] };
  const parts: [string, number][] = [];
  const scalar = (label: string, x: number, y: number, scale: number) =>
    parts.push([label, Math.min(3, Math.abs(x - y) / scale)]);
  scalar("active WIP count", a.active_orders, b.active_orders, 10);
  scalar("cold backlog", a.cold_orders, b.cold_orders, 10);
  scalar(
    "upper-window urgency",
    a.min_upper_slack_minutes,
    b.min_upper_slack_minutes,
    120,
  );
  scalar(
    "remaining hold",
    a.min_remaining_hold_minutes,
    b.min_remaining_hold_minutes,
    120,
  );
  scalar(
    "remaining time today",
    a.day_remaining_minutes,
    b.day_remaining_minutes,
    1440,
  );
  scalar(
    "remaining delivery horizon",
    a.horizon_remaining_minutes,
    b.horizon_remaining_minutes,
    1440,
  );
  scalar(
    "earliest due slack",
    a.due_slack_min_minutes,
    b.due_slack_min_minutes,
    1440,
  );
  scalar("available furnace slots", a.free_slots, b.free_slots, 30);
  scalar("press occupancy", Number(a.press_busy), Number(b.press_busy), 1);
  for (const field of [
    "remaining_operations",
    "heating_by_temperature",
    "furnace_temperature_counts",
  ] as const) {
    const aa = a[field],
      bb = b[field],
      keys = new Set([...Object.keys(aa), ...Object.keys(bb)]);
    const delta = [...keys].reduce(
      (s, k) => s + Math.abs((aa[k] ?? 0) - (bb[k] ?? 0)),
      0,
    );
    const total = Math.max(
      1,
      [...Object.values(aa), ...Object.values(bb)].reduce((s, x) => s + x, 0),
    );
    parts.push([field, delta / total]);
  }
  const locations = (x: ProductionFeatures) =>
    x.vehicles
      .map((v) => v.location)
      .sort()
      .join(",");
  parts.push([
    "vehicle positions",
    Number(locations(a) !== locations(b)) * 0.5,
  ]);
  scalar(
    "vehicle availability",
    a.vehicles.reduce((s, v) => s + v.busy_minutes, 0),
    b.vehicles.reduce((s, v) => s + v.busy_minutes, 0),
    10,
  );
  return {
    distance: parts.reduce((s, p) => s + p[1], 0) / parts.length,
    reasons: parts
      .filter((p) => p[1] > 0.05)
      .sort((a, b) => b[1] - a[1])
      .slice(0, 5)
      .map((p) => p[0]),
  };
}

/** Only exact contexts are automatic. Other experiences go to the main model. */
export function directReuse(
  memory: { exact_match: boolean; strategy: Strategy },
  sc: Scenario,
) {
  if (!memory.exact_match)
    return {
      eligible: false,
      reason: "context differs: main-model adaptation required",
    };
  if (memory.strategy.focus_orders.length)
    return {
      eligible: false,
      reason: "order-specific focus must be reconsidered",
    };
  if (
    sc.production_state &&
    !["extended", "calendar"].includes(memory.strategy.engine)
  )
    return { eligible: false, reason: "observed-state engine required" };
  try {
    parseProposal(
      JSON.stringify({
        summary: "exact-context reusable strategy",
        candidates: [memory.strategy],
      }),
      sc,
    );
    return {
      eligible: true,
      reason:
        "same context and complete current-order coverage; rebuild from current state",
    };
  } catch {
    return {
      eligible: false,
      reason: "historical groups do not cover current orders",
    };
  }
}

export function strategyReference(strategy: Strategy, sc: Scenario) {
  const historical: Record<string, number> = {};
  for (const g of strategy.groups)
    for (const x of g)
      historical[x.route] = (historical[x.route] ?? 0) + x.count;
  const current: Record<string, number> = {};
  for (const o of sc.orders) current[o.route] = (current[o.route] ?? 0) + 1;
  return {
    historical_counts: historical,
    current_counts: current,
    interpretation:
      "Historical grouping is a reference, not a command or a new order count. Compare several strategies, then reason and emit groups covering exactly current orders.",
    differing_routes: [
      ...new Set([...Object.keys(historical), ...Object.keys(current)]),
    ].filter((k) => historical[k] !== current[k]),
  };
}

/** Planner gets current facts, not a redundant replay of the whole executed log. */
export function plannerScenario(sc: Scenario) {
  const { production_state: raw, state_features, ...staticScenario } = sc;
  if (!raw) return { ...staticScenario, current_state: state_features };
  const st = raw as any,
    now = Number(st.now_ticks),
    scale = sc.ticks_per_minute;
  const routes = new Map(sc.routes.map((r) => [r.id, r]));
  const materials = sc.orders.map((o) => {
    const rows = st.operations
      .filter((x: any) => x.order === o.id)
      .sort((a: any, b: any) => a.fire - b.fire);
    const q = rows.at(-1),
      process = routes.get(o.route)!.operations.find((x) => x.fire === q?.fire);
    if (!q)
      return {
        order: o.id,
        route: o.route,
        status: "cold",
        release_minutes: o.release_minutes,
        due_minutes: o.due_minutes,
      };
    const trip = st.transports.find(
      (t: any) => t.order === o.id && t.start < now && t.end >= now,
    );
    const delivered = st.transports.some(
      (t: any) =>
        t.order === o.id && t.kind === "press_to_finished" && t.end <= now,
    );
    const status = trip
      ? "transporting"
      : delivered
        ? "completed"
        : q.forge_start !== undefined && q.forge_end >= now
          ? "forging"
          : q.forge_end !== undefined
            ? "awaiting_next_committed_stage"
            : "heating";
    const holdElapsed = Math.max(0, (now - q.heat_start) / scale);
    return {
      order: o.id,
      route: o.route,
      status,
      fire: q.fire,
      furnace: q.furnace,
      remaining_hold_min_minutes:
        q.forge_start === undefined
          ? Math.max(0, Number(process!.hold_min) - holdElapsed)
          : 0,
      remaining_hold_max_minutes:
        q.forge_start === undefined
          ? Number(process!.hold_max) - holdElapsed
          : 0,
      heat_arrival_in_minutes:
        q.heat_start > now ? (q.heat_start - now) / scale : 0,
      remaining_forge_minutes:
        q.forge_end !== undefined
          ? Math.max(0, (q.forge_end - now) / scale)
          : Number(process!.forge),
      current_trip: trip
        ? {
            kind: trip.kind,
            source: trip.source,
            destination: trip.destination,
            arrival_in_minutes: (trip.end - now) / scale,
            vehicle: trip.vehicle,
          }
        : null,
      due_in_minutes: o.due_minutes - now / scale,
    };
  });
  return {
    ...staticScenario,
    current_state: state_features,
    observed_materials: materials,
    observed_furnaces: st.furnaces,
    observed_vehicles: st.vehicles,
    history_handling:
      "Full observed event history is frozen in the execution model; this planner view contains only current commitments, never an old future schedule.",
  };
}

export function compactReferences(memories: any[]) {
  return memories.map((m) => ({
    id: m.id,
    evidence_ids: m.evidence_ids,
    stage: m.stage,
    strategy: m.stage ? stageStrategy(m.strategy, m.stage) : m.strategy,
    source_state: m.retrieval.source_features,
    context_difference: m.retrieval.reasons,
    adaptation: m.adaptation,
    directly_reusable: m.direct_reuse.eligible,
    observations: m.observations,
    valid_observations: m.valid_observations,
    result: {
      valid: m.outcome.valid,
      source: m.outcome.source,
      gain_minutes: m.outcome.gain_minutes,
      baseline_before_minutes: m.outcome.baseline_before_minutes,
      day_minutes: m.outcome.day_minutes,
      cost: m.outcome.cost,
      status: m.outcome.status,
    },
  }));
}
