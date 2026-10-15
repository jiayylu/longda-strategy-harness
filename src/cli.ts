import { parseArgs } from "node:util";
import { fileURLToPath } from "node:url";
import { dirname, join, resolve } from "node:path";
import {
  readFileSync,
  existsSync,
  mkdirSync,
  cpSync,
  unlinkSync,
} from "node:fs";
import { randomUUID } from "node:crypto";
import { availableParallelism } from "node:os";
import { Events, save, terminal } from "./events.js";
import { ModelClient } from "./client.js";
import { Memory, hash, profile } from "./memory.js";
import {
  Candidate,
  parseProposal,
  expand,
  type Scenario,
  type Strategy,
} from "./contracts.js";
import { SYSTEM } from "./prompts.js";
import { worker } from "./worker.js";
import { sourceManifest } from "./provenance.js";
import { Allocator, bootstrap, strategyKey } from "./learning.js";
import { compactFacts, summarize } from "./auxiliary.js";
import { acquire, readCheckpoint, checkpoint } from "./checkpoint.js";
type Task = {
  id: string;
  action: "construct" | "solve";
  arm: string;
  strategy: Strategy;
  config?: any;
  seed_path?: string;
  partial_path?: string;
  attempt: number;
};
const root = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
export async function main() {
  const { values: a } = parseArgs({
    options: {
      orders: { type: "string", default: "configs/orders20.json" },
      out: { type: "string" },
      resume: { type: "boolean", default: false },
      vehicles: { type: "string" },
      memory: { type: "string", default: "read-write" },
      "memory-db": { type: "string", default: "data/experience-v2.sqlite" },
      model: { type: "string", default: "qwen3.8-max" },
      "aux-model": { type: "string", default: "qwen-flash" },
      "aux-timeout": { type: "string", default: "30" },
      "base-url": {
        type: "string",
        default: "https://dashscope.aliyuncs.com/apps/anthropic",
      },
      effort: { type: "string", default: "high" },
      "max-tokens": { type: "string", default: "32768" },
      target: { type: "string", default: "720" },
      rounds: { type: "string", default: "4" },
      "wall-seconds": { type: "string", default: "600" },
      "solve-seconds": { type: "string", default: "30" },
      "construct-seconds": { type: "string", default: "3" },
      "api-timeout": { type: "string", default: "240" },
      threads: { type: "string", default: "8" },
      "cp-workers": { type: "string", default: "2" },
      "construct-workers": { type: "string", default: "1" },
      "probing-level": { type: "string", default: "0" },
      presolve: { type: "string", default: "auto" },
      preference: { type: "string", default: "none" },
      python: { type: "string", default: ".venv/bin/python" },
      "bootstrap-limit": { type: "string", default: "40" },
      "max-trials": { type: "string", default: "160" },
      allocation: { type: "string", default: "adaptive" },
      "program-file": { type: "string" },
      help: { type: "boolean", default: false },
    },
  });
  if (a.help) {
    console.log(
      "start.py --out runs/NAME [--vehicles 1|2] [--cp-workers 2 --threads 8 --construct-workers 1] [--model qwen3.8-max|off] [--aux-model qwen-flash|off] [--memory read-write|read-only|off] [--wall-seconds 600 --rounds 4 --target 720] [--resume]. Source rules remain frozen. Resume explicitly restarts interrupted tasks, not internal CP search trees.",
    );
    return;
  }
  if (!a.out) throw Error("--out required");
  const num = (k: keyof typeof a, lo: number, hi: number, int = false) => {
    const x = Number(a[k]);
    if (
      !Number.isFinite(x) ||
      x < lo ||
      x > hi ||
      (int && !Number.isInteger(x))
    )
      throw Error("Invalid " + k);
    return x;
  };
  const wall = num("wall-seconds", 1, 86400),
    budget = num("solve-seconds", 0.1, 3600),
    construction = num("construct-seconds", 0.01, 60),
    rounds = num("rounds", 0, 100, true),
    threads = num("threads", 1, 128, true),
    cpWorkers = num("cp-workers", 1, 16, true),
    constructWorkers = num("construct-workers", 1, 8, true),
    target = num("target", 0, 1440),
    limit = num("max-trials", 1, 100000, true),
    bootstrapLimit = num("bootstrap-limit", 0, 500, true),
    apiTimeout = num("api-timeout", 1, 1800),
    auxTimeout = num("aux-timeout", 1, 120),
    maxTokens = num("max-tokens", 1024, 65536, true),
    probing = num("probing-level", 0, 2, true);
  if (cpWorkers * threads + constructWorkers > availableParallelism())
    throw Error("Requested workers exceed available CPUs");
  if (
    !["none", "hold-near-min"].includes(a.preference!) ||
    !["off", "read-only", "read-write"].includes(a.memory!) ||
    !["adaptive", "round-robin"].includes(a.allocation!) ||
    !["auto", "standard", "off"].includes(a.presolve!)
  )
    throw Error("Invalid policy option");
  if (!a["base-url"]!.startsWith("https://")) throw Error("HTTPS required");
  const token = process.env.ANTHROPIC_AUTH_TOKEN;
  if ((a.model !== "off" || a["aux-model"] !== "off") && !token)
    throw Error(
      "Use start.py for hidden API token input, or disable both models",
    );
  const sc = JSON.parse(
    readFileSync(resolve(root, a.orders!), "utf8"),
  ) as Scenario;
  if (a.vehicles !== undefined)
    sc.transport.vehicle_count = num("vehicles", 1, 2, true);
  if (
    sc.furnace_count !== 6 ||
    sc.capacity !== 5 ||
    sc.ticks_per_minute !== 3 ||
    ![1, 2].includes(Number(sc.transport.vehicle_count)) ||
    sc.transport.travel_minutes !== 5 ||
    sc.transport.enabled !== true
  )
    throw Error(
      "Supported model: six furnaces, five slots, one/two cars, five-minute transfers, original three ticks/minute",
    );
  const out = resolve(root, a.out),
    sources = sourceManifest(root),
    ruleHash = hash(
      JSON.parse(readFileSync(join(root, "reference-manifest.json"), "utf8")),
    );
  const identity = hash({
    scenario: sc,
    rules: ruleHash,
    sources,
    preference: a.preference,
  });
  if (existsSync(out) && !a.resume)
    throw Error("Use a new directory or explicit --resume");
  const old = a.resume
    ? readCheckpoint(join(out, "checkpoint.json"), identity)
    : null;
  const release = acquire(out, a.resume!);
  // Explicit resume is user authorization to clear this run's prior stop marker.
  if (a.resume && existsSync(join(out, "STOP"))) unlinkSync(join(out, "STOP"));
  const start = performance.now(),
    id = old?.id ?? randomUUID(),
    previousElapsed = old?.report?.elapsed_s ?? 0;
  const events = new Events(
    join(out, "events.jsonl"),
    id,
    start - previousElapsed * 1000,
  );
  events.on("event", terminal);
  const ctrl = new AbortController(),
    plannerCtrl = new AbortController(),
    auxCtrl = new AbortController();
  let reason = "running";
  const elapsed = () => previousElapsed + (performance.now() - start) / 1000,
    remaining = () => wall - (performance.now() - start) / 1000;
  const stop = (r: string) => {
    if (reason === "running") reason = r;
    ctrl.abort();
    plannerCtrl.abort();
    auxCtrl.abort();
  };
  const onSignal = () => stop("user_stop");
  process.on("SIGINT", onSignal);
  process.on("SIGTERM", onSignal);
  const timer = setInterval(() => {
    if (existsSync(join(out, "STOP"))) stop("user_stop");
    else if (remaining() <= 0) stop("budget");
  }, 200);
  const db =
      a.memory === "off" ? null : new Memory(resolve(root, a["memory-db"]!)),
    context = profile(sc, ruleHash, a.preference!),
    memories = db?.retrieve(context) ?? [];
  const report: any = old?.report ?? {
    schema_version: 2,
    run_id: id,
    scenario_hash: hash(sc),
    rule_hash: ruleHash,
    source_hash: hash(sources),
    memory_hits: memories.length,
    first_valid_s: null,
    target_elapsed_s: null,
    best: null,
    history: [],
    errors: [],
    api_calls: 0,
    aux_calls: 0,
    completed_trials: 0,
    cp_solver_calls: 0,
    construction_trials: 0,
    timing: {
      planner_wall_s: 0,
      aux_wall_s: 0,
      constructor_s: 0,
      cp_s: 0,
      audit_s: 0,
    },
    optimal: false,
  };
  report.configuration = { ...a, transport: sc.transport };
  report.status = "running";
  report.invocations = (report.invocations ?? 0) + 1;
  const allocator = new Allocator(
      old?.arms ?? {},
      old?.selections ?? 0,
      old?.allocations ?? {},
    ),
    queue: Task[] = old ? [...old.running, ...old.queue] : [],
    running = new Map<string, Task>(),
    jobs = new Map<string, Promise<void>>();
  const seen = new Set<string>(old?.seen ?? []);
  let seq = old?.seq ?? 0,
    callCount = 0,
    planner: Promise<void> | null = null,
    aux: Promise<void> | null = null,
    summary: any = old?.summary ?? null,
    lastSummaryN = old?.lastSummaryN ?? 0,
    lastAuxAt = -100000;
  const facts: any[] = old?.facts ?? [];
  const client =
    a.model === "off"
      ? null
      : new ModelClient(a["base-url"]!, a.model!, token!, a.effort!, maxTokens);
  const helper =
    a["aux-model"] === "off"
      ? null
      : new ModelClient(a["base-url"]!, a["aux-model"]!, token!, "off", 4096);
  save(join(out, "scenario.json"), sc);
  save(join(out, "source-manifest.json"), sources);
  save(join(out, "memory-retrieved.json"), memories);
  const persist = () => {
    report.elapsed_s = elapsed();
    report.active_processes = running.size;
    report.pending_tasks = queue.length;
    report.allocation = allocator.arms;
    save(join(out, "progress.json"), report);
    checkpoint(join(out, "checkpoint.json"), {
      identity,
      id,
      seq,
      report,
      queue,
      running: [...running.values()],
      seen: [...seen],
      arms: allocator.arms,
      selections: allocator.selections,
      allocations: allocator.allocations,
      facts,
      summary,
      lastSummaryN,
    });
  };
  const enqueue = (strategy: Strategy) => {
    for (const config of expand(strategy, sc)) {
      const key = hash(config);
      if (seen.has(key)) continue;
      seen.add(key);
      queue.push({
        id: `task-${++seq}`,
        action: "construct",
        arm: `construct:${strategy.engine}:${strategy.admission}:${strategy.vehicle_policy}:${strategy.program_source ? hash(strategy.program_source).slice(0, 10) : "builtin"}`,
        strategy,
        config,
        attempt: 0,
      });
    }
  };
  const publish = (result: any, strategy: Strategy, evidence: string) => {
    if (!result.valid || !result.audit_valid) return 0;
    if (result.primary_optimal === true) {
      report.optimal = true;
      reason = "proved_global_optimum";
    }
    const prev = report.best,
      better =
        !prev ||
        result.metrics.score_ticks > prev.metrics.score_ticks ||
        (a.preference === "hold-near-min" &&
          result.metrics.score_ticks === prev.metrics.score_ticks &&
          result.metrics.hold_excess_ticks < prev.metrics.hold_excess_ticks);
    if (!better) return 0;
    const gain = prev ? Math.max(0, result.day_minutes - prev.day_minutes) : 0;
    report.best = { ...result, strategy, evidence_id: evidence };
    report.first_valid_s ??= elapsed();
    save(join(out, "best.json"), report.best);
    events.send("incumbent_updated", { day_minutes: result.day_minutes });
    if (result.day_minutes >= target) {
      report.target_elapsed_s ??= elapsed();
      reason = "target_reached";
    }
    return gain;
  };
  const launch = (task: Task) => {
    task.attempt++;
    running.set(task.id, task);
    persist();
    const dir = join(out, "tasks", task.id, `attempt-${task.attempt}`),
      base = report.best;
    const c = task.strategy,
      localFailures = allocator.arms[task.arm]?.failures ?? 0;
    const direction = localFailures >= 3 ? "broad" : c.direction;
    const req: any = {
      action: task.action,
      scenario_path: join(out, "scenario.json"),
      preference: a.preference,
      target_minutes: target,
      seconds: Math.min(
        task.action === "construct" ? construction : budget,
        Math.max(0.1, remaining()),
      ),
      threads,
      probing_level: probing,
      presolve: a.presolve,
      seed: c.seeds[0],
      direction,
      fraction: Math.min(1, c.fraction * (localFailures >= 2 ? 2 : 1)),
      focus_orders: localFailures >= 3 ? [] : c.focus_orders,
      incumbent_path: base?.schedule_path,
      incumbent_metrics: base?.metrics,
      seed_path: task.seed_path ?? base?.schedule_path,
      partial_path: task.partial_path,
      mode: base
        ? direction === "broad"
          ? "broad_improve"
          : "local_improve"
        : "seeded_search",
      configs: task.config ? [task.config] : undefined,
      stop_at_target: true,
    };
    events.send("task_started", {
      action: task.action,
      name: c.name,
      active: running.size,
    });
    if (a.memory === "read-write") db?.use(c.evidence_ids, `${id}/${task.id}`);
    const started = performance.now();
    const job = worker(
      root,
      resolve(root, a.python!),
      req,
      dir,
      events,
      ctrl.signal,
    )
      .then((result) => {
        if (
          ctrl.signal.aborted &&
          !result.valid &&
          ["UNKNOWN", "CANCELLED"].includes(result.status)
        ) {
          queue.unshift(task);
          return;
        }
        report.completed_trials++;
        report.cp_solver_calls += Number(result.cp_solver_called ?? false);
        report.construction_trials += result.trials ?? 0;
        report.timing.constructor_s += result.construction_s ?? 0;
        report.timing.cp_s += result.solver_wall_s ?? 0;
        report.timing.audit_s += result.audit_s ?? 0;
        const evidence = `${id}/${task.id}/${task.attempt}`,
          gain = publish(result, c, evidence),
          cost =
            ((performance.now() - started) / 1000) *
            (task.action === "solve" ? threads : 1);
        allocator.update(task.arm, Boolean(result.valid), gain, cost);
        const outcome = {
          status: result.status ?? "construction_deadend",
          source: task.action,
          valid: Boolean(result.valid),
          audit_valid: Boolean(result.audit_valid),
          cp_solver_called: Boolean(result.cp_solver_called),
          baseline_before_minutes: base?.day_minutes ?? null,
          baseline_at_publication_minutes:
            gain > 0 ? result.day_minutes - gain : null,
          day_minutes: result.day_minutes ?? null,
          gain_minutes: gain,
          cost,
          hold_excess_ticks: result.metrics?.hold_excess_ticks,
          proof_scope: result.proof_scope ?? "none",
          error: result.error,
          failures: (result.rows ?? [])
            .filter((x: any) => x.error)
            .map((x: any) => ({
              error: x.error,
              completed_operations: x.completed_operations,
            })),
          phase: base ? "improve" : "cold_start",
          candidate_gain_from_start:
            result.valid && base ? result.day_minutes - base.day_minutes : null,
          hypothesis_status: "unproven",
        };
        const fact = { id: evidence, strategy: c, outcome };
        facts.push(fact);
        report.history.push({
          id: evidence,
          result: join(dir, "result.json"),
          ...outcome,
        });
        if (a.memory === "read-write")
          db!.record(evidence, context, c, outcome);
        if (task.action === "construct") {
          if (
            c.program_source &&
            (result.rows ?? []).some(
              (row: any) => row.status === "program_or_construction_error",
            )
          )
            enqueue({
              ...c,
              name: c.name + "-fallback",
              program_source: null,
              hypothesis:
                "Explicit builtin fallback after generated program failure; original program is not credited.",
            });
          events.send("construction_finished", {
            name: c.name,
            trials: result.trials,
            complete: result.valid ? 1 : 0,
          });
          queue.push({
            id: `task-${++seq}`,
            action: "solve",
            arm: `cp:${c.direction}`,
            strategy: c,
            seed_path: result.seed_path,
            partial_path: result.partial_path,
            attempt: 0,
          });
        } else {
          events.send("solve_finished", {
            status: result.status,
            scope: result.scope,
            valid: result.valid,
            day_minutes: result.day_minutes,
          });
          // Widen/retry attempts; UNKNOWN is retained as an unresolved task, not classified infeasible.
          if (reason === "running" && report.completed_trials < limit && base)
            queue.push({
              id: `task-${++seq}`,
              action: "solve",
              arm: `cp:${direction}`,
              strategy: {
                ...c,
                direction,
                fraction: req.fraction,
                focus_orders: req.focus_orders,
                seeds: [c.seeds[0] + 1],
              },
              attempt: 0,
            });
          if (result.proof_scope === "no_improvement_global") {
            report.optimal = true;
            reason = "proved_no_better";
          }
        }
      })
      .catch((e) => {
        if (ctrl.signal.aborted) {
          queue.unshift(task);
          return;
        }
        report.completed_trials++;
        report.errors.push({ task: task.id, error: String(e) });
        if (c.program_source)
          enqueue({
            ...c,
            name: c.name + "-fallback",
            program_source: null,
            hypothesis:
              "Explicit builtin fallback after a program worker error; no credit to the failed program.",
          });
        allocator.update(
          task.arm,
          false,
          0,
          (performance.now() - started) / 1000,
        );
        const fact = {
          id: `${id}/${task.id}/${task.attempt}`,
          strategy: c,
          outcome: {
            status: "worker_error",
            source: task.action,
            valid: false,
            audit_valid: false,
            gain_minutes: 0,
            cost: (performance.now() - started) / 1000,
            error: String(e),
          },
        };
        facts.push(fact);
        if (a.memory === "read-write")
          db!.record(fact.id, context, c, fact.outcome);
        events.send("error", { message: String(e) });
      })
      .finally(() => {
        running.delete(task.id);
        jobs.delete(task.id);
        persist();
      });
    jobs.set(task.id, job);
  };
  const plan = () => {
    if (!client || planner || callCount >= rounds || reason !== "running")
      return;
    callCount++;
    report.api_calls++;
    const call = report.api_calls,
      dir = join(out, "calls", String(call));
    mkdirSync(dir, { recursive: true });
    const payload = {
      scenario: sc,
      objective: {
        primary: "maximize first-day net forging minutes",
        preference: a.preference,
        target_minutes: target,
      },
      memories,
      cards: db?.cards(context.compatible) ?? [],
      best_complete: report.best
        ? {
            day_minutes: report.best.day_minutes,
            metrics: report.best.metrics,
            strategy: report.best.strategy,
          }
        : null,
      recent_experiments: compactFacts(facts, 16),
      feedback_summary: summary,
      failed_programs: facts
        .filter((f) => f.strategy.program_source && !f.outcome.valid)
        .slice(-3)
        .map((f) => ({
          id: f.id,
          program_source: f.strategy.program_source,
          failures: f.outcome.failures,
          error: f.outcome.error,
        })),
      avoid_configuration_hashes: [...seen].slice(-100),
      planning_brief:
        "Return exactly two concise candidates, including one new rank program of at most 20 lines. Do not simulate a calendar in reasoning; these are cheap executable hypotheses, not a proof. Include a control. Work from current order data, not a saved schedule.",
      available_capabilities: {
        engines: ["legacy", "extended"],
        rolling: true,
        independent_pickup_cars: true,
        bounded_backtracking: true,
        generated_rank_program: true,
      },
    };
    save(join(dir, "request.json"), { system: SYSTEM, payload });
    events.send("model_started", { call });
    const started = performance.now();
    planner = client
      .generate(
        SYSTEM,
        payload,
        Math.min(apiTimeout, remaining()),
        events,
        plannerCtrl.signal,
      )
      .then((answer) => {
        save(join(dir, "response.json"), answer);
        const proposal = parseProposal(answer.text, sc);
        save(join(dir, "plan.json"), proposal);
        for (const c of proposal.candidates) enqueue(c);
        events.send("model_finished", { seconds: answer.wall_s });
      })
      .catch((e) => {
        save(join(dir, "error.json"), { error: String(e) });
        if (!plannerCtrl.signal.aborted) {
          report.errors.push({ model_call: call, error: String(e) });
          events.send("proposal_rejected", { reason: String(e) });
        }
      })
      .finally(() => {
        report.timing.planner_wall_s += (performance.now() - started) / 1000;
        planner = null;
        persist();
      });
  };
  const updateAux = () => {
    if (
      !helper ||
      aux ||
      facts.length - lastSummaryN < 8 ||
      performance.now() - lastAuxAt < 20000 ||
      reason !== "running"
    )
      return;
    lastSummaryN = facts.length;
    lastAuxAt = performance.now();
    report.aux_calls++;
    const path = join(out, `aux-${report.aux_calls}.json`);
    // Summarizer runs independently; the scheduler and primary model never await it.
    aux = summarize(
      helper,
      facts,
      db?.cards(context.compatible) ?? [],
      db,
      context.compatible,
      events,
      auxCtrl.signal,
      Math.min(auxTimeout, remaining()),
      path,
      a.memory === "read-write",
    )
      .then((result) => {
        summary = result;
        report.timing.aux_wall_s += result.wall_s;
      })
      .finally(() => {
        aux = null;
        persist();
      });
  };
  try {
    events.send("run_started", {
      orders: sc.orders.length,
      target_minutes: target,
      memory_mode: a.memory,
    });
    events.send("memory_retrieved", { count: memories.length });
    if (old) {
      events.send("checkpoint", {
        message: `恢复 ${queue.length} 个待办任务，重新校验当前运行的基线`,
      });
      if (report.best) {
        const r = await worker(
          root,
          resolve(root, a.python!),
          {
            action: "audit",
            scenario_path: join(out, "scenario.json"),
            schedule_path: report.best.schedule_path,
            seconds: 30,
          },
          join(out, `resume-audit-${report.invocations}`),
          events,
          ctrl.signal,
        );
        if (!r.valid) throw Error("Saved incumbent failed re-audit");
      }
    } else {
      for (const m of memories) {
        try {
          parseProposal(
            JSON.stringify({
              summary: "retrieved algorithm",
              candidates: [m.strategy],
            }),
            sc,
          );
          enqueue({ ...m.strategy, evidence_ids: [m.id] });
        } catch {
          /* Incompatible counts are observations only. */
        }
      }
      for (const c of bootstrap(sc).slice(0, bootstrapLimit)) enqueue(c);
      if (a["program-file"]) {
        const c = bootstrap(sc)[0];
        enqueue({
          ...c,
          name: "user-program",
          engine: "extended",
          program_source: readFileSync(
            resolve(root, a["program-file"]),
            "utf8",
          ),
        });
      }
    }
    persist();
    plan();
    let lastPlanFacts = facts.length;
    while (reason === "running") {
      if (remaining() <= 0) {
        stop("budget");
        break;
      }
      if (report.completed_trials >= limit) {
        stop("trial_limit");
        break;
      }
      for (const action of ["construct", "solve"] as const) {
        const cap = action === "construct" ? constructWorkers : cpWorkers;
        while (
          [...running.values()].filter((x) => x.action === action).length < cap
        ) {
          const eligible = queue.filter((x) => x.action === action);
          if (!eligible.length) break;
          const ix = a.allocation === "adaptive" ? allocator.pick(eligible) : 0,
            task = eligible[ix];
          queue.splice(queue.indexOf(task), 1);
          launch(task);
        }
      }
      updateAux();
      if (!planner && facts.length - lastPlanFacts >= 6) {
        lastPlanFacts = facts.length;
        plan();
      }
      if (!jobs.size && !queue.length && !planner) {
        if (client && callCount < rounds) plan();
        else {
          reason = "tasks_exhausted";
          break;
        }
      }
      await new Promise((r) => setTimeout(r, 50));
    }
  } catch (e) {
    report.errors.push(String(e));
    reason = ctrl.signal.aborted ? reason : "error";
  } finally {
    ctrl.abort();
    plannerCtrl.abort();
    auxCtrl.abort();
    await Promise.allSettled([
      ...jobs.values(),
      ...(planner ? [planner] : []),
      ...(aux ? [aux] : []),
    ]);
    clearInterval(timer);
    process.off("SIGINT", onSignal);
    process.off("SIGTERM", onSignal);
    report.status = [
      "target_reached",
      "proved_no_better",
      "proved_global_optimum",
    ].includes(reason)
      ? "completed"
      : "paused";
    report.stop_reason = reason;
    report.elapsed_s = elapsed();
    if (report.best) {
      cpSync(dirname(report.best.schedule_path), join(out, "best"), {
        recursive: true,
      });
      report.best_delivery = "best/views";
    }
    if (db) save(join(out, "memory-stats.json"), db.stats(context.compatible));
    persist();
    save(join(out, "report.json"), report);
    db?.close();
    release();
    events.send("run_finished", { reason, output: out });
    console.log(
      JSON.stringify({
        reason,
        day_minutes: report.best?.day_minutes ?? null,
        first_valid_s: report.first_valid_s,
        elapsed_s: report.elapsed_s,
        completed_trials: report.completed_trials,
        model_calls: report.api_calls,
        aux_calls: report.aux_calls,
      }),
    );
  }
  if (reason === "error") process.exitCode = 1;
}
if (
  process.argv[1] &&
  resolve(process.argv[1]) === fileURLToPath(import.meta.url)
)
  main().catch((e) => {
    console.error(String(e));
    process.exitCode = 1;
  });
