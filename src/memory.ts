import { DatabaseSync } from "node:sqlite";
import { createHash } from "node:crypto";
import { mkdirSync } from "node:fs";
import { dirname } from "node:path";
import { CardPatch, strategyKey } from "./learning.js";
import { Candidate, type Scenario, type Strategy } from "./contracts.js";
import { stateDistance, type ProductionFeatures } from "./context.js";
import { memoryStage, stageSuccess, type MemoryStage } from "./memory_stage.js";
export function canonical(x: unknown): string {
  if (Array.isArray(x)) return "[" + x.map(canonical).join(",") + "]";
  if (x && typeof x === "object")
    return (
      "{" +
      Object.entries(x)
        .sort(([a], [b]) => a.localeCompare(b))
        .map(([k, v]) => JSON.stringify(k) + ":" + canonical(v))
        .join(",") +
      "}"
    );
  return JSON.stringify(x);
}
export const hash = (x: unknown) =>
  createHash("sha256").update(canonical(x)).digest("hex");
// Memory learns from strategies and aggregate outcomes, never saved timetables.
export function memoryFacts(input: Record<string, any>) {
  const forbidden = new Set([
    "schedule",
    "best_schedule",
    "heat_start",
    "heat_end",
    "forge_start",
    "forge_end",
    "pickup_event",
    "dropoff_event",
    "transport_tasks",
  ]);
  const check = (x: unknown): void => {
    if (!x || typeof x !== "object") return;
    for (const [k, value] of Object.entries(x)) {
      if (forbidden.has(k))
        throw Error("Memory cannot store a timetable: " + k);
      check(value);
    }
  };
  check(input);
  const result = { ...input };
  if (result.failure) {
    const f = result.failure,
      d = f.details ?? {},
      state = d.failure_context ?? {};
    result.failure = {
      kind: f.kind,
      message: f.message,
      proof_scope: "none",
      details: {
        stage: d.stage,
        exception_type: d.exception_type,
        completed_operations: d.completed_operations,
        nodes: d.nodes,
        backtracks: d.backtracks,
        rejections: state.rejections,
        interpretation:
          "Aggregate failure evidence only; detailed projected reservations stay in the run log, not reusable Memory.",
      },
    };
  }
  if (result.diagnostics) {
    const d = result.diagnostics;
    result.diagnostics = {
      day1_by_route: d.day1_by_route,
      largest_press_gap_minutes:
        d.largest_press_gap_minutes ??
        (d.largest_press_gaps ?? []).map((g: any) => g.minutes),
      largest_hold_excess_minutes:
        d.largest_hold_excess_minutes ??
        (d.largest_hold_excess ?? []).map((g: any) => g.minutes),
      interpretation:
        "Aggregate measured outcomes; no saved timeline or assignment.",
    };
  }
  return result;
}
export function profile(sc: Scenario, ruleHash: string, preference: string) {
  const { orders, ...base } = sc;
  const features = {
    ...base,
    name: undefined,
    orders: orders
      .map(({ id, ...o }) => o)
      .sort((a, b) => canonical(a).localeCompare(canonical(b))),
  };
  return {
    exact: hash(features),
    compatible: hash({
      ruleHash,
      preference,
      furnaces: sc.furnace_count,
      capacity: sc.capacity,
      transport: sc.transport,
      ticks: sc.ticks_per_minute,
      initial_temperature: sc.initial_temperature,
      ramp_up: sc.ramp_up_deg_per_min,
      ramp_down: sc.ramp_down_deg_per_min,
      quotient_identical_orders: sc.quotient_identical_orders,
    }),
    counts: Object.fromEntries(
      sc.routes.map((r) => [
        r.id,
        orders.filter((o) => o.route === r.id).length,
      ]),
    ),
    horizon: sc.horizon_minutes,
    routes: Object.fromEntries(
      sc.routes.map((r) => [r.id, hash(r.operations)]),
    ),
    state: sc.state_features as ProductionFeatures | undefined,
  };
}
export class Memory {
  private db: DatabaseSync;
  constructor(
    file: string,
    readonly readOnly = false,
  ) {
    mkdirSync(dirname(file), { recursive: true });
    this.db = new DatabaseSync(file, { readOnly });
    this.db.exec("PRAGMA busy_timeout=5000;");
    const version = Number(
      (this.db.prepare("PRAGMA user_version").get() as { user_version: number })
        .user_version,
    );
    if (![0, 1, 2, 3, 4].includes(version)) {
      this.db.close();
      throw Error(`Unsupported Memory schema ${version}`);
    }
    if (readOnly) {
      if (version !== 4)
        throw Error("Read-only Memory requires v11 migration on a COPY first.");
      return;
    }
    this.db.exec(
      "PRAGMA journal_mode=WAL; CREATE TABLE IF NOT EXISTS experiences (id TEXT PRIMARY KEY, created TEXT NOT NULL, compatible TEXT NOT NULL, exact TEXT NOT NULL, profile TEXT NOT NULL, strategy TEXT NOT NULL, outcome TEXT NOT NULL); CREATE INDEX IF NOT EXISTS experience_context ON experiences(compatible); CREATE TABLE IF NOT EXISTS cards(id TEXT PRIMARY KEY, compatible TEXT NOT NULL, version INTEGER NOT NULL, retired INTEGER NOT NULL, body TEXT NOT NULL); CREATE TABLE IF NOT EXISTS card_history(id TEXT NOT NULL, version INTEGER NOT NULL, body TEXT NOT NULL, PRIMARY KEY(id,version)); CREATE TABLE IF NOT EXISTS uses(id TEXT PRIMARY KEY, evidence TEXT NOT NULL, trial TEXT NOT NULL);",
    );
    this.db.exec(
      "CREATE TABLE IF NOT EXISTS transfers(id TEXT PRIMARY KEY,source TEXT NOT NULL,trial TEXT NOT NULL,context TEXT NOT NULL,outcome TEXT NOT NULL); CREATE INDEX IF NOT EXISTS transfer_source ON transfers(source);",
    );
    if (version < 4) {
      this.db.exec("BEGIN IMMEDIATE");
      try {
        this.db.exec(
          "ALTER TABLE experiences ADD COLUMN stage TEXT NOT NULL DEFAULT 'unclassified'; ALTER TABLE cards ADD COLUMN stage TEXT NOT NULL DEFAULT 'unclassified'; ALTER TABLE cards ADD COLUMN review_state TEXT NOT NULL DEFAULT 'needs_review';",
        );
        for (const r of this.db
          .prepare("SELECT id,outcome FROM experiences")
          .all() as any[])
          this.db
            .prepare("UPDATE experiences SET stage=? WHERE id=?")
            .run(memoryStage(JSON.parse(r.outcome)), r.id);
        for (const r of this.db
          .prepare("SELECT id,body FROM cards")
          .all() as any[]) {
          const stages = new Set(
            (JSON.parse(r.body).evidence_ids ?? []).map(
              (id: string) =>
                (
                  this.db
                    .prepare("SELECT stage FROM experiences WHERE id=?")
                    .get(id) as any
                )?.stage ?? "unclassified",
            ),
          );
          this.db
            .prepare("UPDATE cards SET stage=? WHERE id=?")
            .run(
              stages.size === 1 ? ([...stages][0] as string) : "unclassified",
              r.id,
            );
        }
        this.db.exec(
          "CREATE INDEX IF NOT EXISTS memory_stage_context ON experiences(compatible,stage); PRAGMA user_version=4; COMMIT",
        );
      } catch (e) {
        this.db.exec("ROLLBACK");
        throw e;
      }
    } else this.db.exec("PRAGMA user_version=4;");
    this.db.exec(
      "CREATE TABLE IF NOT EXISTS memory_issues(id TEXT PRIMARY KEY, compatible TEXT NOT NULL, kind TEXT NOT NULL, evidence_id TEXT NOT NULL, detail TEXT NOT NULL);",
    );
  }
  record(
    id: string,
    p: ReturnType<typeof profile>,
    strategy: Strategy,
    outcome: Record<string, unknown>,
  ) {
    Candidate.parse(strategy);
    if (outcome.valid === true && outcome.audit_valid !== true)
      throw Error("Memory cannot mark unaudited result valid");
    this.db
      .prepare(
        "INSERT OR IGNORE INTO experiences(id,created,compatible,exact,profile,strategy,outcome,stage) VALUES(?,?,?,?,?,?,?,?)",
      )
      .run(
        id,
        new Date().toISOString(),
        p.compatible,
        p.exact,
        JSON.stringify(p),
        JSON.stringify(strategy),
        JSON.stringify(memoryFacts(outcome)),
        memoryStage(outcome),
      );
  }
  evidence(id: string, compatible: string) {
    const r = this.db
      .prepare(
        "SELECT id,profile,strategy,outcome,stage FROM experiences WHERE id=? AND compatible=?",
      )
      .get(id, compatible) as any;
    return r
      ? {
          id: r.id,
          stage: r.stage,
          profile: JSON.parse(r.profile),
          strategy: JSON.parse(r.strategy),
          outcome: memoryFacts(JSON.parse(r.outcome)),
        }
      : null;
  }
  retrieve(p: ReturnType<typeof profile>, max = 6, stage?: MemoryStage) {
    const rows = this.db
      .prepare(
        "SELECT * FROM experiences WHERE compatible=?" +
          (stage ? " AND stage=?" : ""),
      )
      .all(...(stage ? [p.compatible, stage] : [p.compatible])) as any[];
    const aggregated = new Map<string, any>();
    for (const r of rows) {
      const old = JSON.parse(r.profile),
        outcome = memoryFacts(JSON.parse(r.outcome));
      delete outcome.artifact;
      const strategy = Candidate.parse(JSON.parse(r.strategy));
      const countDistance = [
        ...new Set([...Object.keys(p.counts), ...Object.keys(old.counts)]),
      ].reduce(
        (s, k) => s + Math.abs((old.counts[k] ?? 0) - (p.counts[k] ?? 0)),
        0,
      );
      const state = stateDistance(p.state, old.state);
      const shared = Object.keys(p.routes).filter(
        (k) => old.routes?.[k] === p.routes[k],
      );
      const routeOverlap =
        shared.length /
        Math.max(
          1,
          new Set([...Object.keys(p.routes), ...Object.keys(old.routes ?? {})])
            .size,
        );
      const exact = r.exact === p.exact;
      // This is an interpretable ranking, NOT a calibrated success probability.
      const rank =
        (exact ? 1000 : 0) +
        (stageSuccess(outcome, r.stage) ? 20 : 0) +
        10 * routeOverlap -
        10 * state.distance -
        countDistance /
          Math.max(
            1,
            Object.values(p.counts).reduce((s, x) => s + x, 0),
          );
      const reuse = this.db
        .prepare("SELECT outcome FROM transfers WHERE source=?")
        .all(r.id) as any[];
      const transferred = reuse
        .map((x) => JSON.parse(x.outcome))
        .filter((x) => memoryStage(x) === r.stage);
      const failures = transferred.filter((x) => x.valid === false).length;
      const adjusted =
        rank - Math.min(5, failures / Math.max(1, transferred.length));
      const item = {
        id: r.id,
        stage: r.stage,
        success_definition:
          r.stage === "cp_improve"
            ? "audited improvement above launch/publication baseline"
            : "complete audited schedule",
        exact_match: exact,
        strategy,
        outcome,
        rank: adjusted,
        retrieval: {
          method: "structured_context_and_evidence",
          success_probability: null,
          state_distance: state.distance,
          route_overlap: routeOverlap,
          reasons: state.reasons,
          source_features: old.state ?? null,
          transfer_attempts: transferred.length,
          transfer_valid: transferred.filter((x) => x.valid).length,
        },
        scenario_difference: {
          order_count_distance: countDistance,
          horizon_delta_minutes: p.horizon - old.horizon,
        },
        evidence_ids: [r.id],
        observations: 1,
        valid_observations: Number(outcome.valid),
      };
      const sk = r.stage + ":" + strategyKey(strategy),
        prior = aggregated.get(sk);
      if (!prior) aggregated.set(sk, item);
      else {
        const chosen = item.rank > prior.rank ? item : prior;
        chosen.observations = prior.observations + 1;
        chosen.valid_observations =
          prior.valid_observations + Number(outcome.valid);
        chosen.evidence_ids = [...new Set([...prior.evidence_ids, r.id])].slice(
          -8,
        );
        aggregated.set(sk, chosen);
      }
    }
    const candidates = [...aggregated.values()].sort(
      (a, b) =>
        b.rank - a.rank ||
        (b.outcome.gain_minutes ?? 0) / Math.max(1, b.outcome.cost ?? 1) -
          (a.outcome.gain_minutes ?? 0) / Math.max(1, a.outcome.cost ?? 1),
    );
    const selected: any[] = [],
      counts = new Map<string, number>();
    for (const c of candidates) {
      const family = [
        c.strategy.engine,
        c.strategy.admission,
        c.strategy.vehicle_policy,
        c.strategy.direction,
      ].join(":");
      if ((counts.get(family) ?? 0) >= 2) continue;
      selected.push(c);
      counts.set(family, (counts.get(family) ?? 0) + 1);
      if (selected.length >= max) break;
    }
    for (const c of candidates)
      if (selected.length < max && !selected.includes(c)) selected.push(c);
    return selected.map(({ rank, ...c }) => c);
  }
  transfer(
    sourceIds: string[],
    trial: string,
    context: ReturnType<typeof profile>,
    outcome: Record<string, unknown>,
  ) {
    const clean = memoryFacts(outcome);
    for (const source of sourceIds) {
      if (
        !this.db
          .prepare("SELECT id FROM experiences WHERE id=? AND compatible=?")
          .get(source, context.compatible)
      )
        throw Error("Unknown transfer evidence");
      this.db
        .prepare("INSERT OR IGNORE INTO transfers VALUES(?,?,?,?,?)")
        .run(
          hash({ source, trial }),
          source,
          trial,
          JSON.stringify(context),
          JSON.stringify(clean),
        );
    }
  }

  use(evidence: string[], trial: string) {
    for (const id of evidence)
      this.db
        .prepare("INSERT OR IGNORE INTO uses VALUES(?,?,?)")
        .run(hash([id, trial]), id, trial);
  }
  facts(compatible: string, max = 60) {
    return (
      this.db
        .prepare(
          "SELECT id,strategy,outcome,profile,stage FROM experiences WHERE compatible=? ORDER BY created DESC LIMIT ?",
        )
        .all(compatible, max) as any[]
    ).map((r) => ({
      id: r.id,
      stage: r.stage,
      strategy: Candidate.parse(JSON.parse(r.strategy)),
      outcome: memoryFacts(JSON.parse(r.outcome)),
      context: JSON.parse(r.profile).state ?? null,
    }));
  }
  cards(compatible: string, state?: ProductionFeatures, stage?: MemoryStage) {
    return (
      this.db
        .prepare(
          "SELECT * FROM cards WHERE compatible=? AND retired=0 AND review_state='verified_scope'" +
            (stage ? " AND stage=?" : ""),
        )
        .all(...(stage ? [compatible, stage] : [compatible])) as any[]
    )
      .map((r) => ({
        ...JSON.parse(r.body),
        id: r.id,
        version: r.version,
        stage: r.stage,
      }))
      .map((c) => ({
        ...c,
        context_distance: Math.min(
          ...(c.source_contexts?.length ? c.source_contexts : [null]).map(
            (x: ProductionFeatures) => stateDistance(state, x).distance,
          ),
        ),
      }))
      .sort(
        (a, b) =>
          a.context_distance - b.context_distance ||
          (b.updated_at ?? "").localeCompare(a.updated_at ?? ""),
      )
      .slice(0, 12);
  }
  patches(compatible: string, input: unknown[], allowedIds: string[]) {
    const allowed = new Set(allowedIds),
      changes: unknown[] = [];
    for (const raw of input) {
      const p = CardPatch.parse(raw);
      if (p.evidence_ids.some((id) => !allowed.has(id)))
        throw Error("Unknown evidence in Memory patch");
      const evidence = p.evidence_ids.map(
        (id) =>
          this.db
            .prepare("SELECT * FROM experiences WHERE id=? AND compatible=?")
            .get(id, compatible) as any,
      );
      if (evidence.some((x) => !x))
        throw Error("Evidence must exist in this compatible context");
      const stages = new Set(evidence.map((r) => r.stage));
      if (stages.size !== 1 || stages.has("unclassified"))
        throw Error(
          "Memory claim must cite ONE executed stage: construct, cp_complete or cp_improve; do not mix successes.",
        );
      const stage = evidence[0].stage;
      if (p.stage && p.stage !== stage)
        throw Error("Claim stage disagrees with executed evidence stage");
      const old = this.db
        .prepare("SELECT * FROM cards WHERE id=?")
        .get(p.id) as any;
      if (
        p.op === "add"
          ? Boolean(old) || p.expected_version !== 0
          : !old ||
            old.compatible !== compatible ||
            old.version !== p.expected_version
      )
        throw Error("Stale or invalid Memory patch");
      if (old && old.stage !== stage)
        throw Error(
          "Cannot change a card's executed stage; add a separate card with matching evidence.",
        );
      if (old && p.op === "update") {
        const originalSuccesses = JSON.parse(old.body).evidence_ids.filter(
          (id: string) => {
            const fact = this.evidence(id, compatible);
            return fact && stageSuccess(fact.outcome, fact.stage);
          },
        );
        const missing = originalSuccesses.filter(
          (id: string) => !p.evidence_ids.includes(id),
        );
        if (missing.length)
          throw Error(
            "Update must retain earlier successful evidence; add a separate narrower card if needed. Missing: " +
              missing.join(", "),
          );
      }
      if (p.op === "retire") {
        if (!p.retirement)
          throw Error(
            "Retirement requires explicit reason, explanation and evidence. UNKNOWN alone is not grounds.",
          );
        const retirement = p.retirement;
        for (const id of retirement.evidence_ids)
          if (!allowed.has(id) || !this.evidence(id, compatible))
            throw Error(
              "Retirement evidence not provided or incompatible: " + id,
            );
        const previous = JSON.parse(old.body);
        if (!previous.evidence_ids.every((id: string) => allowed.has(id)))
          throw Error(
            "Retirement requires reading ALL original evidence alongside new evidence.",
          );
        if (retirement.reason === "superseded") {
          const replacement = this.db
            .prepare(
              "SELECT * FROM cards WHERE id=? AND compatible=? AND retired=0 AND review_state='verified_scope'",
            )
            .get(retirement.replacement_id ?? "", compatible) as any;
          if (
            !replacement ||
            replacement.id === old.id ||
            replacement.stage !== stage
          )
            throw Error(
              "Supersession requires an active, reviewed, same-stage replacement.",
            );
          const rb = JSON.parse(replacement.body);
          if (
            canonical(rb.conditions) !== canonical(previous.conditions) ||
            !previous.evidence_ids.every((id: string) =>
              rb.evidence_ids.includes(id),
            )
          )
            throw Error(
              "Replacement must preserve the original conditions and supporting evidence; narrowing is an update, not retirement.",
            );
        } else {
          const issues = retirement.verified_issue_ids ?? [];
          if (
            !issues.length ||
            issues.some((id) => {
              const issue = this.db
                .prepare(
                  "SELECT * FROM memory_issues WHERE id=? AND compatible=? AND kind=?",
                )
                .get(id, compatible, retirement.reason) as any;
              return (
                !issue ||
                !previous.evidence_ids.includes(issue.evidence_id) ||
                !retirement.evidence_ids.includes(issue.evidence_id)
              );
            })
          )
            throw Error(
              "Factual error/obsolete scope requires an independently verified issue record, not an LLM assertion.",
            );
        }
      }
      const version = (old?.version ?? 0) + 1;
      // Facts/counters are computed from evidence, never accepted from the model.
      const outcomes = evidence.map((x) => JSON.parse(x.outcome));
      const body = {
        updated_at: new Date().toISOString(),
        conditions: p.conditions,
        recommendation: p.recommendation,
        exceptions: p.exceptions,
        evidence_ids: p.evidence_ids,
        stage,
        retirement: p.retirement ?? null,
        epistemic_status: "conditional hypothesis, not a physical rule",
        attribution: {
          measured_parent_comparisons: outcomes
            .map((x) => x.edit_contrast)
            .filter(Boolean),
          status: "observational",
          note: "Adoption, retrieval or positive final score alone is not proof that an edit caused improvement",
        },
        source_contexts: [
          ...new Map(
            evidence.map((x) => {
              const p = JSON.parse(x.profile);
              return [x.exact, p.state ?? null];
            }),
          ).values(),
        ],
        measured: {
          trials: outcomes.length,
          valid: outcomes.filter((x) => x.valid).length,
          gain_minutes: outcomes.reduce((n, x) => n + (x.gain_minutes ?? 0), 0),
          cost: outcomes.reduce((n, x) => n + (x.cost ?? 0), 0),
        },
      };
      this.db.exec("BEGIN IMMEDIATE");
      try {
        this.db
          .prepare(
            "INSERT INTO cards(id,compatible,version,retired,body,stage,review_state) VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET version=excluded.version,retired=excluded.retired,body=excluded.body,stage=excluded.stage,review_state=excluded.review_state",
          )
          .run(
            p.id,
            compatible,
            version,
            Number(p.op === "retire"),
            JSON.stringify(body),
            stage,
            "verified_scope",
          );
        this.db
          .prepare("INSERT INTO card_history VALUES(?,?,?)")
          .run(p.id, version, JSON.stringify({ ...p, ...body }));
        this.db.exec("COMMIT");
      } catch (e) {
        this.db.exec("ROLLBACK");
        throw e;
      }
      changes.push({ id: p.id, version, op: p.op });
    }
    return changes;
  }
  stats(compatible: string) {
    const groups: Record<string, any> = {};
    for (const fact of this.facts(compatible, 10000)) {
      const key = fact.stage + ":" + strategyKey(fact.strategy),
        x = fact.outcome;
      const a = (groups[key] ??= {
        strategy_key: key,
        stage: fact.stage,
        trials: 0,
        valid: 0,
        gain_minutes: 0,
        cost: 0,
        failures: {},
      });
      a.trials++;
      a.valid += Number(x.valid === true);
      a.gain_minutes += x.gain_minutes ?? 0;
      a.cost += x.cost ?? 0;
      if (!x.valid) a.failures[x.status] = (a.failures[x.status] ?? 0) + 1;
    }
    return {
      strategies: Object.values(groups),
      transfers: this.db
        .prepare("SELECT source,trial,context,outcome FROM transfers")
        .all(),
      uses: this.db
        .prepare("SELECT evidence,count(*) AS n FROM uses GROUP BY evidence")
        .all(),
    };
  }
  count() {
    return Number(
      (this.db.prepare("SELECT count(*) as n FROM experiences").get() as any).n,
    );
  }
  close() {
    this.db.close();
  }
}
