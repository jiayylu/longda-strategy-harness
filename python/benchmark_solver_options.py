"""Bounded solver-option comparison using a declared known fixture, not an LLM run."""

import argparse, json, time
from pathlib import Path
import worker

p = argparse.ArgumentParser()
p.add_argument(
    "--variant", choices=["default", "no_probe", "no_presolve"], required=True
)
p.add_argument("--out", required=True)
a = p.parse_args()
sc = json.loads((worker.ROOT / "configs/orders20.json").read_text())
out = worker.ROOT / a.out
out.mkdir(parents=True, exist_ok=False)
seed_path = worker.ROOT / "runs/offline-smoke20/best_schedule.json"
result = worker.solve(
    sc,
    dict(
        mode="seeded_search",
        seed_path=str(seed_path),
        preference="none",
        threads=16,
        seconds=45,
        target_minutes=720,
        stop_at_target=True,
        probing_level=0 if a.variant == "no_probe" else 2,
        presolve="off" if a.variant == "no_presolve" else "standard",
    ),
    out,
)
result["variant"] = a.variant
worker.save(out / "result.json", result)
print(json.dumps(result))
