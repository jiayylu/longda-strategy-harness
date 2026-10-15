import { spawn } from "node:child_process";
import {
  readFileSync,
  existsSync,
  mkdirSync,
  createWriteStream,
} from "node:fs";
import { join } from "node:path";
import { save, type Events } from "./events.js";
export async function worker(
  root: string,
  python: string,
  request: Record<string, unknown>,
  out: string,
  events: Events,
  signal: AbortSignal,
): Promise<Record<string, any>> {
  mkdirSync(out, { recursive: true });
  save(join(out, "request.json"), request);
  if (signal.aborted) throw Error("Run cancelled");
  return await new Promise((resolve, reject) => {
    const log = createWriteStream(join(out, "worker.log"));
    const child = spawn(
      python,
      [join(root, "python/worker.py"), join(out, "request.json"), out],
      {
        cwd: root,
        env: {
          ...process.env,
          ANTHROPIC_AUTH_TOKEN: undefined,
          PYTHONDONTWRITEBYTECODE: "1",
        },
        stdio: ["ignore", "pipe", "pipe"],
        detached: true,
      },
    );
    let pending = "",
      forced: NodeJS.Timeout | undefined;
    const stop = () => {
      if (child.pid) {
        try {
          process.kill(-child.pid, "SIGTERM");
        } catch {}
        forced ??= setTimeout(() => {
          try {
            process.kill(-child.pid!, "SIGKILL");
          } catch {}
        }, 10000);
      }
    };
    signal.addEventListener("abort", stop, { once: true });
    const watchdog = setTimeout(
      stop,
      (Number(request.seconds ?? 30) + 30) * 1000,
    );
    child.stdout.on("data", (chunk: Buffer) => {
      log.write(chunk);
      pending += chunk.toString();
      let e;
      while ((e = pending.indexOf("\n")) >= 0) {
        const line = pending.slice(0, e);
        pending = pending.slice(e + 1);
        try {
          const x = JSON.parse(line);
          if (x.event === "progress")
            events.send("solve_progress", {
              phase: x.phase,
              seconds: x.seconds,
            });
        } catch {
          /* Non-protocol lines stay in the worker log. */
        }
      }
    });
    child.stderr.pipe(log, { end: false });
    child.once("error", (error) => {
      clearTimeout(watchdog);
      if (forced) clearTimeout(forced);
      signal.removeEventListener("abort", stop);
      log.end();
      reject(error);
    });
    child.once("close", (code) => {
      clearTimeout(watchdog);
      if (forced) clearTimeout(forced);
      signal.removeEventListener("abort", stop);
      log.end();
      const file = join(out, "result.json");
      if (!existsSync(file)) {
        reject(
          Error(`Worker ended ${code} without result; see ${out}/worker.log`),
        );
        return;
      }
      const result = JSON.parse(readFileSync(file, "utf8"));
      if (code !== 0 || result.status === "WORKER_ERROR")
        reject(Error(result.error ?? `Worker exit ${code}`));
      else resolve(result);
    });
  });
}
