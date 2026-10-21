import { hash } from "./memory.js";
import type { Strategy } from "./contracts.js";

export type PoolState = {
  epoch: string;
  cursor: number;
  seen: string[];
  pass?: number;
};
export const emptyPool = (): PoolState => ({
  epoch: "",
  cursor: 0,
  seen: [],
  pass: 0,
});

/** Finite set of distinct neighborhoods per incumbent. Scheduler decides resource shares. */
export function neighborhoodCandidates(strategy: Strategy, orderIds: string[]) {
  const result: Strategy[] = [];
  for (const direction of [
    "press_gap",
    "vehicle_link",
    "furnace_batch",
    "day_boundary",
    "broad",
  ] as const) {
    for (const fraction of direction === "broad" ? [1] : [0.2, 0.4, 0.6]) {
      result.push({ ...strategy, direction, fraction, focus_orders: [] });
    }
  }
  // Alternative explicit scopes avoid all seeds repeatedly choosing the same orders.
  for (const fraction of [0.2, 0.4]) {
    const n = Math.max(1, Math.ceil(orderIds.length * fraction));
    for (let start = 0; start < orderIds.length; start += n) {
      const scope = [
        ...orderIds.slice(start),
        ...orderIds.slice(0, start),
      ].slice(0, n);
      result.push({
        ...strategy,
        direction: "press_gap",
        fraction,
        focus_orders: scope,
      });
    }
  }
  return [...new Map(result.map((s) => [neighborhoodKey(s), s])).values()];
}
export function neighborhoodKey(s: Strategy) {
  return hash({
    direction: s.direction,
    fraction: s.fraction,
    focus: [...(s.focus_orders ?? [])].sort(),
  });
}
export function fillNeighborhoods(
  state: PoolState,
  epoch: string,
  strategy: Strategy,
  orders: string[],
  count: number,
  occupied: Strategy[],
  allowed: (strategy: Strategy) => boolean = () => true,
) {
  if (state.epoch !== epoch)
    Object.assign(state, { epoch, cursor: 0, seen: [], pass: 0 });
  const candidates = neighborhoodCandidates(strategy, orders);
  const busy = new Set(occupied.map(neighborhoodKey));
  const available = candidates.filter(
    (c) => !busy.has(neighborhoodKey(c)) && allowed(c),
  );
  if (count <= 0 || !available.length) return [];
  // A persistent control lane must not prevent the other lanes from changing
  // seed passes. Busy/cooling scopes remain excluded even after a rollover.
  // We need not wait for an unseen scope held by a long-running control task.
  if (available.every((c) => state.seen.includes(neighborhoodKey(c)))) {
    state.pass = (state.pass ?? 0) + 1;
    state.seen = [];
    state.cursor = 0;
  }
  const blocked = new Set([...state.seen, ...busy]);
  const added: Strategy[] = [];
  const cursor = state.cursor;
  for (let k = 0; k < candidates.length && added.length < count; k++) {
    const index = (cursor + k) % candidates.length,
      c = candidates[index];
    const key = neighborhoodKey(c);
    if (blocked.has(key) || !allowed(c)) continue;
    blocked.add(key);
    state.seen.push(key);
    added.push({
      ...c,
      name: `portfolio-${c.direction}-${Math.round(c.fraction * 100)}-${index}`,
      hypothesis:
        "Program-generated neighborhood from current audited incumbent; not a delivered model strategy",
      evidence_ids: [],
      seeds: [(state.pass ?? 0) % 1000001],
    });
    state.cursor = (index + 1) % candidates.length;
  }
  return added;
}
