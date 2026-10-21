import { readFileSync, writeFileSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";
import { Memory } from "./memory.js";
const [database, reviewFile] = process.argv.slice(2);
const review = JSON.parse(readFileSync(reviewFile, "utf8"));
const read = new DatabaseSync(database, { readOnly: true });
const source = read
  .prepare("SELECT compatible FROM experiences WHERE id=?")
  .get(review.cards[0].evidence_ids[0]) as any;
if (!source) throw Error("Missing source experience");
const existing = new Map(
  (read.prepare("SELECT id,version FROM cards").all() as any[]).map((r) => [
    r.id,
    r.version,
  ]),
);
read.close();
const db = new Memory(database);
const patches = review.cards.map((c: any) =>
  existing.has(c.id)
    ? { ...c, op: "update", expected_version: existing.get(c.id) }
    : c,
);
const changes = db.patches(
  source.compatible,
  patches,
  patches.flatMap((c: any) => c.evidence_ids),
);
const visible = db.cards(source.compatible);
db.close();
writeFileSync(
  reviewFile.replace(".json", "-applied.json"),
  JSON.stringify(
    {
      changes,
      visible_review_cards: visible
        .filter((c) => c.id.startsWith("review-v9-"))
        .map((c) => c.id),
      no_timetables_imported: true,
    },
    null,
    2,
  ),
);
console.log(
  JSON.stringify({
    changes,
    visible_review_cards: visible
      .filter((c) => c.id.startsWith("review-v9-"))
      .map((c) => c.id),
  }),
);
