import {
  existsSync,
  readFileSync,
  mkdirSync,
  openSync,
  writeFileSync,
  closeSync,
  unlinkSync,
} from "node:fs";
import { join } from "node:path";
import { hostname } from "node:os";
import { save } from "./events.js";
export function acquire(out: string, resume: boolean) {
  mkdirSync(out, { recursive: true });
  const file = join(out, "run.lock");
  if (existsSync(file)) {
    const old = JSON.parse(readFileSync(file, "utf8"));
    if (!resume || old.host !== hostname())
      throw Error("Run lock exists; cannot assume remote owner stopped");
    let alive = true;
    try {
      process.kill(old.pid, 0);
    } catch (e: any) {
      if (e.code === "ESRCH") alive = false;
    }
    if (alive) throw Error("Run is already active");
    unlinkSync(file);
  }
  const fd = openSync(file, "wx");
  writeFileSync(fd, JSON.stringify({ pid: process.pid, host: hostname() }));
  closeSync(fd);
  return () => {
    if (existsSync(file)) unlinkSync(file);
  };
}
export function readCheckpoint(file: string, identity: string) {
  const x = JSON.parse(readFileSync(file, "utf8"));
  if (x.schema_version !== 2 || x.identity !== identity)
    throw Error(
      "Resume requires identical scenario, hard rules, code and objective",
    );
  return x;
}
export function checkpoint(file: string, state: Record<string, unknown>) {
  save(file, { schema_version: 2, ...state });
}
