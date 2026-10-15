import { DatabaseSync } from "node:sqlite";
import { createHash } from "node:crypto";
import { mkdirSync } from "node:fs";
import { dirname } from "node:path";
import { CardPatch, strategyKey } from "./learning.js";
import { Candidate, type Scenario, type Strategy } from "./contracts.js";
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
      routes: sc.routes,
    }),
    counts: Object.fromEntries(
      sc.routes.map((r) => [
        r.id,
        orders.filter((o) => o.route === r.id).length,
      ]),
    ),
    horizon: sc.horizon_minutes,
  };
}
export class Memory {
  private db: DatabaseSync;
  constructor(file: string) {
    mkdirSync(dirname(file), { recursive: true });
    this.db = new DatabaseSync(file);
    this.db.exec("PRAGMA busy_timeout=5000;");
    const version = Number(
      (this.db.prepare("PRAGMA user_version").get() as { user_version: number })
        .user_version,
    );
    if (version !== 0 && version !== 1 && version !== 2) {
      this.db.close();
      throw Error(`Unsupported Memory schema ${version}`);
    }
    this.db.exec(
      "PRAGMA journal_mode=WAL; CREATE TABLE IF NOT EXISTS experiences (id TEXT PRIMARY KEY, created TEXT NOT NULL, compatible TEXT NOT NULL, exact TEXT NOT NULL, profile TEXT NOT NULL, strategy TEXT NOT NULL, outcome TEXT NOT NULL); CREATE INDEX IF NOT EXISTS experience_context ON experiences(compatible); PRAGMA user_version=2; CREATE TABLE IF NOT EXISTS cards(id TEXT PRIMARY KEY, compatible TEXT NOT NULL, version INTEGER NOT NULL, retired INTEGER NOT NULL, body TEXT NOT NULL); CREATE TABLE IF NOT EXISTS card_history(id TEXT NOT NULL, version INTEGER NOT NULL, body TEXT NOT NULL, PRIMARY KEY(id,version)); CREATE TABLE IF NOT EXISTS uses(id TEXT PRIMARY KEY, evidence TEXT NOT NULL, trial TEXT NOT NULL);",
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
      .prepare("INSERT OR IGNORE INTO experiences VALUES(?,?,?,?,?,?,?)")
      .run(
        id,
        new Date().toISOString(),
        p.compatible,
        p.exact,
        JSON.stringify(p),
        JSON.stringify(strategy),
        JSON.stringify(memoryFacts(outcome)),
      );
  }
  retrieve(p: ReturnType<typeof profile>, max = 6) {
    const rows = this.db
      .prepare(
        "SELECT * FROM experiences WHERE compatible=? ORDER BY created DESC LIMIT 100",
      )
      .all(p.compatible) as any[];
    const candidates = rows
      .map((r) => {
        const old = JSON.parse(r.profile);
        const outcome = memoryFacts(JSON.parse(r.outcome));
        // Evidence remains traceable in the database; the model gets its ID.
        delete outcome.artifact;
        if (outcome.scope === "local" && !outcome.mode) {
          outcome.mode = "local_improve";
          outcome.context_note =
            "Early record: source incumbent not stored. This score is not a standalone constructor score.";
        }
        const countDistance = Object.keys(p.counts).reduce(
          (s, k) => s + Math.abs((old.counts[k] ?? 0) - p.counts[k]),
          0,
        );
        return {
          id: r.id,
          exact_match: r.exact === p.exact,
          scenario_difference: {
            order_count_distance: countDistance,
            horizon_delta_minutes: p.horizon - old.horizon,
          },
          strategy: Candidate.parse(JSON.parse(r.strategy)),
          outcome,
          rank:
            (r.exact === p.exact ? 1000 : 0) +
            (outcome.valid ? 100 : 0) -
            countDistance -
            Math.abs(p.horizon - old.horizon) / 1440,
        };
      })
      .sort(
        (a, b) =>
          b.rank - a.rank ||
          (b.outcome.gain_minutes ?? 0) / Math.max(1, b.outcome.cost ?? 1) -
            (a.outcome.gain_minutes ?? 0) / Math.max(1, a.outcome.cost ?? 1),
      );
    const seen = new Set<string>();
    return candidates
      .filter((c) => {
        const key = strategyKey(c.strategy);
        if (seen.has(key)) return false;
        seen.add(key);
        return true;
      })
      .slice(0, max)
      .map(({ rank, ...c }) => c);
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
          "SELECT id,strategy,outcome FROM experiences WHERE compatible=? ORDER BY created DESC LIMIT ?",
        )
        .all(compatible, max) as any[]
    ).map((r) => ({
      id: r.id,
      strategy: Candidate.parse(JSON.parse(r.strategy)),
      outcome: memoryFacts(JSON.parse(r.outcome)),
    }));
  }
  cards(compatible: string) {
    return (
      this.db
        .prepare("SELECT * FROM cards WHERE compatible=? AND retired=0")
        .all(compatible) as any[]
    ).map((r) => ({ id: r.id, version: r.version, ...JSON.parse(r.body) }));
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
      const version = (old?.version ?? 0) + 1;
      // Facts/counters are computed from evidence, never accepted from the model.
      const outcomes = evidence.map((x) => JSON.parse(x.outcome));
      const body = {
        conditions: p.conditions,
        recommendation: p.recommendation,
        exceptions: p.exceptions,
        evidence_ids: p.evidence_ids,
        epistemic_status: "conditional hypothesis, not a physical rule",
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
            "INSERT INTO cards VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET version=excluded.version,retired=excluded.retired,body=excluded.body",
          )
          .run(
            p.id,
            compatible,
            version,
            Number(p.op === "retire"),
            JSON.stringify(body),
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
      const key = strategyKey(fact.strategy),
        x = fact.outcome;
      const a = (groups[key] ??= {
        strategy_key: key,
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
