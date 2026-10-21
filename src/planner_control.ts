/** Coordinate planner delivery with finite run/trial budgets. No scheduling rules here. */
export function plannerWaitSeconds(
  apiSeconds: number,
  remainingSeconds: number,
) {
  return Math.max(0, Math.min(apiSeconds, remainingSeconds));
}
export function mayPublishTarget(
  requirePlanner: boolean,
  evaluatedThisInvocation: number,
) {
  return !requirePlanner || evaluatedThisInvocation > 0;
}
export function mayLaunchTrial(
  completed: number,
  running: number,
  limit: number,
  requirePlanner: boolean,
  evaluated: number,
  plannerOrigin: boolean,
) {
  // Reserve one trial for actual execution of a returned model strategy.
  const reserve = requirePlanner && evaluated === 0 && !plannerOrigin ? 1 : 0;
  return completed + running < limit - reserve;
}

/** Constructor identity deliberately excludes these fields; solver identity must include them. */
export function solverGuidanceKey(
  strategy: {
    direction: string;
    fraction: number;
    focus_orders: string[];
    seeds: number[];
  },
  incumbentPath?: string,
  hintPath?: string,
) {
  return JSON.stringify({
    direction: strategy.direction,
    fraction: strategy.fraction,
    orders: [...strategy.focus_orders].sort(),
    seed: strategy.seeds[0],
    incumbent: incumbentPath ?? null,
    hint: hintPath ?? null,
  });
}
