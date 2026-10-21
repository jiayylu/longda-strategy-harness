/** Provider-enforced output shape; executable validation still uses contracts.ts. */
const str = { type: "string" };
const nullableString = { type: ["string", "null"] };
const arr = (items: any) => ({ type: "array", items });
const obj = (
  properties: Record<string, any>,
  required = Object.keys(properties),
) => ({ type: "object", properties, required, additionalProperties: false });
const choice = (...values: string[]) => ({ type: "string", enum: values });
const integer = (minimum: number, maximum: number) => ({
  type: "integer",
  minimum,
  maximum,
});
const candidate = obj({
  name: str,
  hypothesis: str,
  evidence_ids: arr(str),
  groups: arr(arr(obj({ route: str, count: integer(1, 100000) }))),
  loading: choice("grouped", "interleaved"),
  policies: arr(integer(0, 5)),
  seeds: arr(integer(0, 1000000)),
  direction: choice(
    "broad",
    "press_gap",
    "vehicle_link",
    "furnace_batch",
    "day_boundary",
  ),
  fraction: { type: "number", minimum: 0.1, maximum: 1 },
  focus_orders: arr(str),
  engine: choice("legacy", "extended", "calendar"),
  admission: choice("batch", "rolling"),
  vehicle_policy: choice("same", "split"),
  backtrack_budget: integer(0, 32),
  wip_limit: integer(1, 30),
  program_source: nullableString,
  algorithm_source: nullableString,
});
const args = obj(
  {
    name: str,
    source: str,
    seconds: { type: "number" },
    parent_hash: str,
    resume_trial_id: str,
    rerun: { type: "boolean" },
    trial_id: str,
    trial_ids: arr(str),
    evidence_id: str,
    section: str,
    offset: { type: "integer", minimum: 0 },
  },
  [],
);
export const PLANNER_SCHEMA = obj({
  summary: str,
  candidates: { ...arr(candidate), maxItems: 8 },
  tool_calls: {
    ...arr(
      obj({
        name: choice(
          "get_latest_state",
          "read_evidence",
          "read_guide",
          "read_algorithm",
          "validate_algorithm",
          "run_algorithm",
          "inspect_trial",
          "compare_trials",
        ),
        arguments: args,
      }),
    ),
    maxItems: 1,
  },
});
