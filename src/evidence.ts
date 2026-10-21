import { existsSync, readFileSync, realpathSync } from "node:fs";
import { resolve, sep } from "node:path";
import { createHash } from "node:crypto";
/** Index is produced by an explicit import, never from model-provided paths. */
export class EvidenceCatalog {
  private entries: Record<string, any> = {};
  private roots: string[] = [];
  constructor(file?: string) {
    if (file && existsSync(file)) {
      const d = JSON.parse(readFileSync(file, "utf8"));
      this.entries = d.entries ?? {};
      this.roots = (d.roots ?? [])
        .filter((x: string) => existsSync(x))
        .map((x: string) => realpathSync(x));
    }
  }
  read(id: string, issued: Set<string>, fallback?: any) {
    if (!issued.has(id))
      throw Error(
        JSON.stringify({ code: "EVIDENCE_NOT_ISSUED", invalid_id: id }),
      );
    const row = this.entries[id];
    if (!row)
      return {
        evidence_id: id,
        availability: "summary_only",
        record: fallback ?? null,
        reason:
          "No registered raw artifact; no reconstruction of missing evidence.",
      };
    if (!existsSync(row.path))
      return {
        evidence_id: id,
        availability: "summary_only",
        record: fallback ?? row.summary,
        reason: "Registered artifact no longer exists",
      };
    const path = realpathSync(row.path);
    if (
      !this.roots.some((root) => path === root || path.startsWith(root + sep))
    )
      throw Error("Evidence path escaped its registered root");
    const bytes = readFileSync(path);
    if (createHash("sha256").update(bytes).digest("hex") !== row.sha256)
      throw Error(
        JSON.stringify({ code: "EVIDENCE_ARTIFACT_CHANGED", evidence_id: id }),
      );
    const raw = JSON.parse(bytes.toString());
    for (const k of [
      "schedule",
      "best_schedule",
      "schedule_path",
      "seed_path",
      "partial_path",
    ])
      delete raw[k];
    return {
      evidence_id: id,
      availability: "raw_result",
      record: fallback ?? row.summary,
      raw,
      artifact_sha256: row.sha256,
      instruction:
        "Historical measurements and strategy only. No timetable is injected as a current solution.",
    };
  }
}
