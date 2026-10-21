import { z } from "zod";
import { CardPatch } from "./learning.js";
import { compactFacts } from "./auxiliary.js";
import { ModelClient } from "./client.js";
import { Memory } from "./memory.js";
import { save, type Events } from "./events.js";

const Summary = z
  .object({
    summary: z.string().min(1).max(4000),
    evidence_ids: z.array(z.string()).max(100),
  })
  .strict();
const Assessment = z
  .object({
    evidence_ids: z.array(z.string()).min(1).max(4),
    changed_component: z.string().max(200),
    observation: z.string().max(1200),
    verdict: z.enum(["helped", "hurt", "no_observed_effect", "confounded"]),
  })
  .strict();
const Updates = z
  .object({
    patches: z.array(z.unknown()).max(6),
    edit_assessments: z.array(z.unknown()).max(8).default([]),
  })
  .strict();
const parse = (s: string) =>
  JSON.parse(
    s
      .trim()
      .replace(/^```(?:json)?\s*/, "")
      .replace(/\s*```$/, ""),
  );
const SUMMARIZER = `You are an independent feedback-compaction service, not the planner or memory updater. Return ONLY JSON {"summary":"concise evidence-linked summary","evidence_ids":["E1"]}. Merge the previous summary with the older measured facts. Preserve failures, contradictory observations, distinctions between constructor deadend / CP UNKNOWN / proved infeasible, and the state contexts in which results occurred. Compare gain from the actual starting baseline and cost; no causal conclusion from uncontrolled comparisons. Current orders/state and the recent raw tail are preserved separately for the main planner. Do not invent progress, emit plans, edit memory or include timetables. Use supplied evidence IDs. Make the result shorter than the input.`;
const UPDATER = `Each conditions/exceptions string must be at most 300 characters; recommendation at most 1000. Each edit_assessment has at most FOUR evidence_ids. Use evidence_ids arrays for citations; avoid E-number ranges in prose. Every fact mentioned in prose must be supported by that patch's evidence_ids, including contradictions. No internal deliberation or "I will retain" text in stored experience. Experience stages are separate: construct, cp_complete, cp_improve. Every patch must state stage and cite only facts with that executed stage. A CP success cannot support a constructor claim. For CP claims discuss actual released orders, baseline, seed/hint and allocated seconds, not engine/admission/vehicle_policy. Retirement requires a retirement object {reason:"superseded|evidence_error|scope_obsolete", explanation:"explicit evidence-based reason", evidence_ids:["E1"], replacement_id:"active same-stage card id if superseded", verified_issue_ids:[]}. A superseding card must preserve original conditions and supporting facts. Error/obsolete claims require independent issue records, which you cannot create. Missing those records means no retirement. Update must retain original successful evidence. UNKNOWN and lower recent gains alone justify qualification, not retirement. Original facts behind every supplied card are included. You are an independent experiment-memory updater. Read current production-state features, historical experience cards and verified trial facts. Return ONLY JSON {"patches":[{"op":"add|update|retire","id":"stable id","expected_version":0,"conditions":["applicable state conditions"],"recommendation":"conditional reusable strategy, not a timetable","exceptions":["contradictory evidence and limits"],"evidence_ids":["E1"]}]}. At most TWO patches, each with at most TWENTY evidence_ids; select the relevant evidence, not the whole history. You may also return edit_assessments (up to eight rows: evidence_ids, changed_component, observation, verdict). Add uses version0; update/retire must match supplied version. Keep cold-start and WIP-continuation evidence separate; distinguish similar route counts from identical equipment state. Reconcile contradictory evidence by narrowing conditions or retiring a claim, not erasing original facts. A different order count is a prompt for planner adaptation, never permission to reuse obsolete counts. Do not change rules, algorithm code or measured scores. Program recomputes statistics and source contexts. UNKNOWN is not infeasible, and failed construction does not prove no legal schedule exists. Keep an existing card unchanged if evidence is insufficient. No causality claims without controlled comparisons. Legal-but-poor quality feedback does not mean an invalid schedule. Shared-incumbent CP gains do not establish that the associated constructor helped; check hint_provenance and actual baseline. Read edit_contrast: adopted candidates do not prove individual edits helped. For each claim distinguish helped, hurt, no_observed_effect, confounded. Single runs or multiple simultaneous changes remain confounded; preserve that qualification in exceptions. No evidence for a reusable lesson: return patches:[] (NO_ENTRY), retain raw records. Cite both parent and changed trials when available. Conditions describe transferable order/WIP/resource patterns, never exact timetable answers.`;

function aliasFacts(facts: any[], cards: any[] = [], extraIds: string[] = []) {
  const ids = [
    ...new Set<string>([
      ...facts.map((f) => f.id),
      ...cards.flatMap((c) => c.evidence_ids ?? []),
      ...extraIds,
    ]),
  ];
  const forward = new Map(ids.map((id, i) => [id, `E${i + 1}`])),
    back = new Map([...forward].map(([id, a]) => [a, id]));
  return {
    facts: facts.map((f) => ({ ...f, id: forward.get(f.id) })),
    cards: cards.map((c) => ({
      ...c,
      evidence_ids: c.evidence_ids.map((id: string) => forward.get(id)),
    })),
    ids,
    back,
    forward,
    restore: (id: string) => {
      const full = back.get(id);
      if (!full) throw Error("Unknown evidence alias");
      return full;
    },
  };
}

export async function compressFeedback(
  client: Pick<ModelClient, "generate">,
  facts: any[],
  previous: any,
  context: any,
  tail: number,
  events: Events,
  signal: AbortSignal,
  seconds: number,
  path: string,
) {
  const started = performance.now();
  const older = facts.slice(0, Math.max(0, facts.length - tail));
  const covered = new Set<string>(previous?.covered_evidence_ids ?? []);
  const unseen = older.filter((f) => !covered.has(f.id));
  const compact = compactFacts(unseen, 32),
    pack = aliasFacts(compact, [], previous?.evidence_ids ?? []);
  const previousText = (previous?.summary ?? "").replace(
    /\bE\d+\b/g,
    (a: string) => pack.forward.get(previous?.evidence_aliases?.[a]) ?? a,
  );
  const previousEvidence = (previous?.evidence_ids ?? []).map((id: string) =>
    pack.forward.get(id),
  );
  const payload = {
    current_context: context,
    previous_summary: previousText,
    previous_evidence_ids: previousEvidence,
    older_facts: pack.facts,
    program_pruned_older_count: Math.max(0, unseen.length - compact.length),
    recent_raw_tail_kept: Math.min(tail, facts.length),
  };
  save(path.replace(/\.json$/, ".request.json"), {
    system: SUMMARIZER,
    payload,
    evidence_map: Object.fromEntries(pack.back),
  });
  try {
    const answer = await client.generate(
      SUMMARIZER,
      payload,
      seconds,
      events,
      signal,
    );
    save(path.replace(/\.json$/, ".response.json"), answer);
    const parsed = Summary.parse(parse(answer.text));
    parsed.evidence_ids = parsed.evidence_ids.map(pack.restore);
    if (parsed.summary.length >= JSON.stringify(payload).length)
      throw Error("Summary did not compress");
    const result = {
      status: "model",
      role: "summary",
      ...parsed,
      evidence_aliases: Object.fromEntries(pack.back),
      covered_evidence_ids: [
        ...new Set([...covered, ...compact.map((f) => f.id)]),
      ],
      input_chars: JSON.stringify(payload).length,
      summary_chars: parsed.summary.length,
      model: answer.response_model,
      wall_s: (performance.now() - started) / 1000,
    };
    save(path, result);
    events.send("auxiliary", {
      message: `独立摘要本次处理 ${compact.length} 条旧反馈；保留最近 ${tail} 条原始事实`,
    });
    return result;
  } catch (e) {
    const result = {
      status: "fallback",
      role: "summary",
      summary: previousText + "\n" + JSON.stringify(compact),
      evidence_ids: [
        ...new Set([
          ...(previous?.evidence_ids ?? []),
          ...compact.map((f) => f.id),
        ]),
      ],
      evidence_aliases: Object.fromEntries(pack.back),
      covered_evidence_ids: [
        ...new Set([...covered, ...compact.map((f) => f.id)]),
      ],
      error: String(e),
      wall_s: (performance.now() - started) / 1000,
    };
    save(path, result);
    return result;
  }
}

export async function updateExperience(
  client: Pick<ModelClient, "generate">,
  facts: any[],
  cards: any[],
  context: any,
  db: Memory | null,
  compatible: string,
  write: boolean,
  events: Events,
  signal: AbortSignal,
  seconds: number,
  path: string,
) {
  const started = performance.now(),
    recent = compactFacts(facts, 32),
    // Every cited old fact accompanies its card, not just an opaque E-number.
    prior = db
      ? cards.flatMap((c) =>
          c.evidence_ids
            .map((id: string) => {
              const e = db.evidence(id, compatible);
              return e ? { ...e, context: e.profile.state } : null;
            })
            .filter(Boolean),
        )
      : [],
    allFacts = [
      ...new Map(
        [...recent, ...compactFacts(prior, prior.length)].map((f) => [f.id, f]),
      ).values(),
    ],
    pack = aliasFacts(allFacts, cards);
  const payload = {
    working_state: context,
    experience_cards: pack.cards,
    trial_facts: pack.facts,
    output_extension: {
      edit_assessments: [
        {
          evidence_ids: ["E1"],
          changed_component: "changed strategy/code component",
          observation:
            "Measured execution difference; include failure and cost",
          verdict: "helped|hurt|no_observed_effect|confounded",
        },
      ],
      instruction:
        "Keep assessments even when patches is empty. Never infer that every edit in an adopted candidate helped.",
    },
  };
  save(path.replace(/\.json$/, ".request.json"), {
    system: UPDATER,
    payload,
    evidence_map: Object.fromEntries(pack.back),
  });
  try {
    const answer = await client.generate(
      UPDATER,
      payload,
      seconds,
      events,
      signal,
    );
    save(path.replace(/\.json$/, ".response.json"), answer);
    const parsed = Updates.parse(parse(answer.text)),
      changes: unknown[] = [],
      rejected: unknown[] = [];
    const assessments: any[] = [];
    for (const [index, raw] of parsed.edit_assessments.entries()) {
      try {
        const row = Assessment.parse(raw),
          evidence_ids = row.evidence_ids.map(pack.restore);
        assessments.push({
          ...row,
          evidence_ids,
          proposed_verdict: row.verdict,
          verdict:
            row.verdict === "no_observed_effect" ? row.verdict : "confounded",
          attribution_limit:
            "Observed outcome; no isolated causal comparison established by these trial records",
        });
      } catch (e) {
        rejected.push({ kind: "edit_assessment", index, error: String(e) });
      }
    }
    for (const [index, raw] of parsed.patches.entries()) {
      try {
        const patch = CardPatch.parse(raw);
        const cited = new Set([
          ...patch.evidence_ids,
          ...(patch.retirement?.evidence_ids ?? []),
        ]);
        const prose = [
          patch.recommendation,
          ...patch.conditions,
          ...patch.exceptions,
          patch.retirement?.explanation ?? "",
        ].join(" ");
        for (const id of prose.match(/\bE\d+\b/g) ?? [])
          if (!cited.has(id))
            throw Error(
              "Unlisted evidence in patch prose: " +
                id +
                "; include the supporting fact or remove the unsupported claim.",
            );
        const stable = (text: string) =>
          text.replace(/\bE\d+\b/g, pack.restore);
        patch.recommendation = stable(patch.recommendation);
        patch.conditions = patch.conditions.map(stable);
        patch.exceptions = patch.exceptions.map(stable);
        patch.evidence_ids = patch.evidence_ids.map(pack.restore);
        if (patch.retirement) {
          patch.retirement.explanation = stable(patch.retirement.explanation);
          patch.retirement.evidence_ids = patch.retirement.evidence_ids.map(
            pack.restore,
          );
        }
        if (write && db)
          changes.push(
            ...db.patches(
              compatible,
              [patch],
              allFacts.map((f) => f.id),
            ),
          );
      } catch (e) {
        rejected.push({
          kind: "patch",
          index,
          id: (raw as any)?.id,
          error: String(e),
        });
      }
    }
    const result = {
      status: "model",
      role: "memory_updater",
      proposed: parsed.patches,
      edit_assessments: assessments,
      changes,
      rejected,
      model: answer.response_model,
      wall_s: (performance.now() - started) / 1000,
    };
    save(path, result);
    events.send("auxiliary", {
      message: `独立 Memory 更新：应用 ${changes.length} 条，拒绝 ${rejected.length} 条；原始证据保留`,
    });
    return result;
  } catch (e) {
    const result = {
      status: "fallback",
      role: "memory_updater",
      changes: [],
      error: String(e),
      wall_s: (performance.now() - started) / 1000,
    };
    save(path, result);
    return result;
  }
}
