import { existsSync, mkdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { save } from "./events.js";
import { hash } from "./memory.js";

function canonical(x: any): any {
  if (Array.isArray(x)) return x.map(canonical);
  if (x && typeof x === "object")
    return Object.fromEntries(
      Object.keys(x)
        .sort()
        .map((k) => [k, canonical(x[k])]),
    );
  return x;
}
/** Execution identity includes all behavior-changing inputs, never just source code. */
export function executionKey(input: any) {
  return hash(canonical(input));
}
export class ToolReceipts {
  private active = new Map<string, Promise<any>>();
  constructor(readonly dir: string) {
    mkdirSync(dir, { recursive: true });
  }
  async run(identity: any, rerun: boolean, execute: () => Promise<any>) {
    const key = executionKey(identity),
      file = join(this.dir, key + ".json");
    if (this.active.has(key))
      return {
        status: "ALREADY_IN_PROGRESS",
        execution_key: key,
        valid: false,
        proof_scope: "none",
      };
    if (existsSync(file)) {
      const old = JSON.parse(readFileSync(file, "utf8"));
      if (old.state === "COMPLETED" && !rerun)
        return { ...old.result, receipt_replayed: true, execution_key: key };
      if (old.state === "STARTED") {
        let alive = false;
        try {
          process.kill(old.owner_pid, 0);
          alive = true;
        } catch {}
        if (alive)
          return {
            status: "ALREADY_IN_PROGRESS",
            execution_key: key,
            valid: false,
            proof_scope: "none",
          };
        if (!rerun)
          return {
            status: "OUTCOME_UNKNOWN",
            execution_key: key,
            valid: false,
            proof_scope: "none",
            note: "Interrupted attempt; inspect saved evidence or explicitly request rerun. Not infeasibility.",
          };
      }
    }
    const previous = existsSync(file)
      ? JSON.parse(readFileSync(file, "utf8"))
      : null;
    save(file, {
      state: "STARTED",
      owner_pid: process.pid,
      key,
      identity,
      started_at: Date.now(),
      previous_trial: previous?.result?.trial_id ?? null,
    });
    // Deferred body ensures the in-flight marker is installed before synchronous execution.
    const work = Promise.resolve().then(execute);
    this.active.set(key, work);
    try {
      const result = await work;
      save(file, { state: "COMPLETED", key, result });
      return { ...result, execution_key: key };
    } finally {
      this.active.delete(key);
    }
  }
}
