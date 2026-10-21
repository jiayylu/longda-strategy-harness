/** Wire metadata is normalized before strict business validation. Unknown business keys survive. */
export function normalizeEnvelope(value: any, toolsAvailable: boolean) {
  if (!value || typeof value !== "object" || Array.isArray(value))
    throw Error("INVALID_ENVELOPE: return a JSON object");
  const result = { ...value };
  if ("tool_calls" in result) {
    if (!Array.isArray(result.tool_calls))
      throw Error(
        "INVALID_TOOL_CALLS: tool_calls must be an array; use [] for a final proposal",
      );
    if (!result.tool_calls.length) delete result.tool_calls;
    else {
      if (result.candidates?.length)
        throw Error(
          "MIXED_DELIVERY: execute one tool OR submit candidates, not both. Preserve candidates and remove tool_calls to submit.",
        );
      if (!toolsAvailable)
        throw Error(
          "TOOLS_UNAVAILABLE: this role cannot execute tools; return the requested final JSON",
        );
    }
  }
  return result;
}

export function recordDelivery(
  report: any,
  call: number | string,
  answer: any,
) {
  const p = report.planner_delivery;
  p.turn_outcomes ??= [];
  p.non_delivery_reasons ??= {};
  p.protocol_feedback_count ??= 0;
  p.protocol_feedback_count +=
    answer.delivery_diagnostics?.protocol_errors ?? 0;
  const row = {
    call,
    status: answer.turn_status,
    reason: answer.delivery_reason ?? null,
    diagnostics: answer.delivery_diagnostics ?? null,
    candidate_count: (() => {
      try {
        return JSON.parse(answer.text).candidates?.length ?? 0;
      } catch {
        return 0;
      }
    })(),
  };
  p.turn_outcomes.push(row);
  if (
    answer.turn_status === "BUDGET_LIMITED" ||
    answer.delivery_reason === "empty_proposal"
  ) {
    const why = answer.delivery_reason ?? "turn_budget_exhausted";
    p.non_delivery_reasons[why] = (p.non_delivery_reasons[why] ?? 0) + 1;
    p.errors++;
    report.errors.push({
      model_call: call,
      kind: why,
      error: "No final strategy delivered",
      diagnostics: row.diagnostics,
    });
  }
  return row;
}
