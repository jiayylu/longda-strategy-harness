/** Bounded integration checks: stub model transport, real constructor + audits, no API calls. */
import {
  mkdtempSync,
  writeFileSync,
  readFileSync,
  mkdirSync,
  readdirSync,
} from "node:fs";
import { spawnSync } from "node:child_process";
import { resolve, join } from "node:path";
import assert from "node:assert/strict";
import { bootstrap } from "../dist/src/learning.js";
const root = resolve(import.meta.dirname, "..");
const out = process.argv[2]
  ? resolve(process.argv[2])
  : mkdtempSync("/tmp/longda-delivery-");
mkdirSync(out, { recursive: true });
const sc = JSON.parse(
  readFileSync(join(root, "configs/orders20.json"), "utf8"),
);
sc.orders = sc.orders.slice(0, 1);
const repeated = {
  ...bootstrap(sc)[0],
  policies: [0],
  direction: "vehicle_link",
  fraction: 0.5,
  name: "fixture-reused-constructor-new-cp",
  evidence_ids: [],
};
writeFileSync(join(out, "repeated-strategy.json"), JSON.stringify(repeated));
const scenario = join(out, "scenario.json");
writeFileSync(scenario, JSON.stringify(sc));
const mock = join(out, "model-stub.mjs");
writeFileSync(
  mock,
  `
import {readFileSync} from 'node:fs';
const sc=JSON.parse(readFileSync(process.env.DELIVERY_SCENARIO,'utf8'));
globalThis.fetch=async(_url,init)=>new Response(new ReadableStream({start(c){
 const send=x=>c.enqueue(new TextEncoder().encode('data: '+JSON.stringify(x)+'\\n\\n'));
 send({type:'content_block_delta',delta:{type:'thinking_delta',thinking:'private fixture thought'}});
 const timer=process.env.DELIVERY_MODE==='timeout'?null:setTimeout(()=>{
  const counts={};for(const o of sc.orders)counts[o.route]=(counts[o.route]||0)+1;
  let candidate={name:'fixture-model-strategy',hypothesis:'Integration fixture, not a model benchmark',groups:[Object.entries(counts).map(([route,count])=>({route,count}))],loading:'interleaved',policies:[0],seeds:[921],direction:'broad',fraction:1,focus_orders:[],engine:'legacy',admission:'batch',vehicle_policy:'same',backtrack_budget:0,wip_limit:5,program_source:null};
  if (process.env.DELIVERY_MODE==='duplicate') candidate=JSON.parse(readFileSync(process.env.DELIVERY_REPEATED,'utf8'));
  send({type:'content_block_delta',delta:{type:'text_delta',text:JSON.stringify({summary:'fixture',candidates:[candidate]})}});
  send({type:'message_delta',delta:{stop_reason:'end_turn'}});
 },Number(process.env.DELIVERY_DELAY||2500));
 init.signal.addEventListener('abort',()=>{if(timer)clearTimeout(timer);c.error(init.signal.reason)},{once:true});
},cancel(){}}),{headers:{'content-type':'text/event-stream'}});
`,
);
const rows = [];
for (const [name, limit, timeout, delay] of [
  ["target-waits-for-model", 2, 8, 2500],
  ["default-unlimited-trials", null, 8, 100],
  ["trial-reserved-for-model", 1, 8, 100],
  ["model-timeout-is-explicit", 2, 1, 0],
  ["reused-constructor-new-cp", 12, 8, 2500],
]) {
  const run = join(out, name);
  const r = spawnSync(
    process.execPath,
    [
      "--import",
      mock,
      join(root, "dist/src/cli.js"),
      "--orders",
      scenario,
      "--out",
      run,
      "--vehicles",
      "1",
      "--model",
      "fixture",
      "--summary-model",
      "off",
      "--memory-model",
      "off",
      "--memory",
      "off",
      "--cp-workers",
      name === "reused-constructor-new-cp" ? "1" : "0",
      "--solve-seconds",
      "2",
      "--construct-workers",
      "1",
      "--threads",
      "1",
      "--rounds",
      "1",
      "--wall-seconds",
      "15",
      "--api-timeout",
      String(timeout),
      ...(limit === null ? [] : ["--max-trials", String(limit)]),
      "--bootstrap-limit",
      "1",
      "--target",
      name === "reused-constructor-new-cp" ? "1440" : "0",
      "--require-planner-strategy",
    ],
    {
      cwd: root,
      timeout: 25000,
      encoding: "utf8",
      env: {
        ...process.env,
        ANTHROPIC_AUTH_TOKEN: "fixture-not-a-real-credential",
        DELIVERY_SCENARIO: scenario,
        DELIVERY_MODE: name.includes("timeout")
          ? "timeout"
          : name === "reused-constructor-new-cp"
            ? "duplicate"
            : "success",
        DELIVERY_REPEATED: join(out, "repeated-strategy.json"),
        DELIVERY_DELAY: String(delay),
      },
    },
  );
  writeFileSync(join(out, name + ".log"), r.stdout + "\n" + r.stderr);
  assert.equal(r.status, 0, r.stdout + r.stderr);
  const report = JSON.parse(readFileSync(join(run, "report.json"), "utf8"));
  if (name.includes("timeout")) {
    assert.equal(report.stop_reason, "planner_not_delivered");
    assert.equal(report.planner_delivery.evaluated_trials, 0);
    assert.equal(report.errors[0].kind, "api_timeout");
  } else if (name === "reused-constructor-new-cp") {
    assert.equal(report.planner_delivery.accepted_candidates, 1);
    assert.equal(report.planner_delivery.evaluated_trials, 1);
    const events = readFileSync(join(run, "events.jsonl"), "utf8")
      .trim()
      .split("\n")
      .map(JSON.parse);
    assert.ok(events.some((x) => x.kind === "strategy_reused"));
    assert.ok(
      events.some(
        (x) =>
          x.kind === "task_started" &&
          x.data.action === "solve" &&
          x.data.name === "fixture-reused-constructor-new-cp",
      ),
    );
    assert.ok(
      !events.some(
        (x) =>
          x.kind === "task_started" &&
          x.data.action === "construct" &&
          x.data.name === "fixture-reused-constructor-new-cp",
      ),
    );
    const solverRequests = readdirSync(join(run, "tasks"))
      .map((t) => join(run, "tasks", t, "attempt-1", "request.json"))
      .map((f) => JSON.parse(readFileSync(f, "utf8")))
      .filter((r) => r.action === "solve");
    assert.ok(
      solverRequests.some(
        (r) => r.direction === "vehicle_link" && r.fraction === 0.5,
      ),
    );
    assert.ok(report.best?.audit_valid);
  } else {
    assert.equal(report.stop_reason, "target_reached");
    assert.equal(report.planner_delivery.completed_calls, 1);
    assert.equal(report.planner_delivery.evaluated_trials, 1);
    assert.ok(report.best?.audit_valid);
    if (limit !== null) assert.ok(report.completed_trials <= limit);
    if (limit === 1) assert.equal(report.completed_trials, 1);
    if (delay === 2500) {
      const events = readFileSync(join(run, "events.jsonl"), "utf8")
        .trim()
        .split("\n")
        .map(JSON.parse);
      assert.ok(
        events.find((x) => x.kind === "incumbent_updated").elapsed_s <
          events.find((x) => x.kind === "model_finished").elapsed_s,
      );
    }
  }
  rows.push({
    name,
    reason: report.stop_reason,
    elapsed_s: report.elapsed_s,
    planner_delivery: report.planner_delivery,
    audit_valid: report.best?.audit_valid ?? false,
  });
}
writeFileSync(join(out, "smoke-summary.json"), JSON.stringify(rows, null, 2));
console.log(JSON.stringify(rows, null, 2));
