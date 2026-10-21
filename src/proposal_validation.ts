import { modelJson } from "./model_json.js";
import { parseProposal, type Scenario } from "./contracts.js";
export class ProposalIssue extends Error {
  constructor(
    readonly details: any,
    readonly accepted: any[] = [],
  ) {
    super(JSON.stringify({ code: "PROPOSAL_ERRORS", details }));
  }
}
export function validateProposal(
  text: string,
  sc: Scenario,
  issued: Set<string>,
) {
  const raw = modelJson(text) as any;
  if (!Array.isArray(raw.candidates) || raw.candidates.length > 8)
    return parseProposal(text, sc);
  const accepted: any[] = [],
    issues: any[] = [];
  for (const [index, candidate] of raw.candidates.entries()) {
    try {
      const c = parseProposal(
        JSON.stringify({ ...raw, candidates: [candidate] }),
        sc,
      ).candidates[0];
      const invalid = c.evidence_ids.filter((id) => !issued.has(id));
      if (invalid.length)
        issues.push({
          candidate: index,
          name: c.name,
          code: "UNKNOWN_EVIDENCE",
          invalid_ids: invalid,
          suggestions: invalid.map((id) => ({
            invalid_id: id,
            alternatives: [...issued]
              .filter(
                (x) =>
                  x.split("/").slice(1).join("/") ===
                  id.split("/").slice(1).join("/"),
              )
              .slice(0, 5),
          })),
        });
      else accepted.push(c);
    } catch (e) {
      issues.push({
        candidate: index,
        name: candidate?.name,
        details: String(e),
      });
    }
  }
  if (issues.length) throw new ProposalIssue(issues, accepted);
  return parseProposal(text, sc);
}
