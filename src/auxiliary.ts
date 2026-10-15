import { AuxiliaryAnswer } from "./learning.js";
import { ModelClient } from "./client.js";
import { AUX_SYSTEM } from "./prompts.js";
import { Memory } from "./memory.js";
import { save, type Events } from "./events.js";
export function compactFacts(facts: any[], limit = 16) {
  return facts.slice(-limit).map((f) => ({
    id: f.id,
    strategy: {
      name: f.strategy.name,
      engine: f.strategy.engine,
      groups: f.strategy.groups,
      policies: f.strategy.policies,
      seeds: f.strategy.seeds,
      cp_direction: f.strategy.direction,
      admission: f.strategy.admission,
      vehicle_policy: f.strategy.vehicle_policy,
      backtrack_budget: f.strategy.backtrack_budget,
      wip_limit: f.strategy.wip_limit,
    },
    outcome: {
      status: f.outcome.status,
      source: f.outcome.source,
      valid: f.outcome.valid,
      baseline_before_minutes: f.outcome.baseline_before_minutes,
      day_minutes: f.outcome.day_minutes,
      gain_minutes: f.outcome.gain_minutes,
      cost: f.outcome.cost,
      error: f.outcome.error,
      failures: f.outcome.failures,
    },
  }));
}
export async function summarize(
  client: ModelClient,
  facts: any[],
  cards: any[],
  db: Memory | null,
  compatible: string,
  events: Events,
  signal: AbortSignal,
  timeout: number,
  path: string,
  write: boolean,
) {
  const started = performance.now(),
    compact = compactFacts(facts, 16),
    ids = new Set<string>([
      ...compact.map((x) => x.id),
      ...cards.flatMap((c) => c.evidence_ids ?? []),
    ]);
  const aliases = new Map([...ids].map((id, i) => [id, `E${i + 1}`]));
  const original = new Map([...aliases].map(([id, alias]) => [alias, id]));
  const payload = {
    facts: compact.map((f) => ({ ...f, id: aliases.get(f.id) })),
    cards: cards.map((c) => ({
      ...c,
      evidence_ids: c.evidence_ids.map((id: string) => aliases.get(id)),
    })),
    instruction:
      "Use exactly the supplied E1,E2,... evidence IDs. Candidate names are labels, never numeric ratios. cp_direction affects only solve tasks; it does not drive construction. Recommend at most TWO patches this call. No causal claim without a controlled comparison.",
  };
  save(path.replace(/\.json$/, ".request.json"), {
    payload,
    evidence_map: Object.fromEntries(original),
  });
  try {
    const answer = await client.generate(
      AUX_SYSTEM,
      payload,
      timeout,
      events,
      signal,
    );
    save(path.replace(/\.json$/, ".response.json"), answer);
    const data = AuxiliaryAnswer.parse(
      JSON.parse(
        answer.text
          .trim()
          .replace(/^```(?:json)?\s*/, "")
          .replace(/\s*```$/, ""),
      ),
    );
    const restore = (id: string) => {
      const full = original.get(id);
      if (!full) throw Error("Unrecognized summary evidence alias");
      return full;
    };
    data.evidence_ids = data.evidence_ids.map(restore);
    for (const p of data.patches) p.evidence_ids = p.evidence_ids.map(restore);
    if (
      data.evidence_ids.some((x) => !ids.has(x)) ||
      data.patches.some((p) => p.evidence_ids.some((x) => !ids.has(x)))
    )
      throw Error("Unrecognized summary evidence");
    if (
      data.summary.length >= JSON.stringify(compact).length &&
      compact.length > 3
    )
      throw Error("Summary did not compress");
    const changes: unknown[] = [],
      rejected_patches: unknown[] = [];
    if (write && db)
      for (const patch of data.patches) {
        try {
          changes.push(...db.patches(compatible, [patch], [...ids]));
        } catch (e) {
          rejected_patches.push({
            id: patch.id,
            op: patch.op,
            error: String(e),
          });
        }
      }
    const narration =
      /(最优|达标|合法方案|optimal|feasib|target reached)/i.test(data.narration)
        ? ""
        : data.narration;
    const result = {
      status: "model",
      ...data,
      narration_draft: narration,
      narration: "",
      input_chars: JSON.stringify(payload).length,
      summary_chars: data.summary.length,
      changes,
      rejected_patches,
      model: answer.response_model,
      usage: answer.usage,
      wall_s: (performance.now() - started) / 1000,
    };
    save(path, result);
    events.send("auxiliary", {
      message: `已压缩 ${compact.length} 条试验事实；经验更新 ${changes.length} 条，拒绝 ${rejected_patches.length} 条过期或无效更新`,
    });
    return result;
  } catch (e) {
    const result = {
      status: "fallback",
      summary: JSON.stringify(compact),
      evidence_ids: [...ids],
      changes: [],
      error: String(e),
      wall_s: (performance.now() - started) / 1000,
    };
    save(path, result);
    events.send("auxiliary", {
      message: "辅助调用未完成或校验未过，使用程序事实摘要；搜索继续",
    });
    return result;
  }
}
