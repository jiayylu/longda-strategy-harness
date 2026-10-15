import { readdirSync, readFileSync, existsSync } from "node:fs";
import { join, relative } from "node:path";
import { createHash } from "node:crypto";
export function sourceManifest(root: string) {
  const files: Record<string, string> = {};
  const collect = (dir: string) => {
    for (const entry of readdirSync(dir, { withFileTypes: true })) {
      const path = join(dir, entry.name);
      if (entry.isDirectory()) {
        if (entry.name !== "__pycache__") collect(path);
      } else if (/\.(ts|js|py|json)$/.test(entry.name)) {
        files[relative(root, path)] = createHash("sha256")
          .update(readFileSync(path))
          .digest("hex");
      }
    }
  };
  for (const dir of ["src", "python", "dist/src"])
    if (existsSync(join(root, dir))) collect(join(root, dir));
  for (const file of [
    "package.json",
    "package-lock.json",
    "requirements.lock",
    "reference-manifest.json",
  ])
    files[file] = createHash("sha256")
      .update(readFileSync(join(root, file)))
      .digest("hex");
  return files;
}
