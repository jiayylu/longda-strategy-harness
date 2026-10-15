"""Known fixture tests hints against real Solve. Never supplied to the model."""

import argparse, json, time
from pathlib import Path
import worker

p = argparse.ArgumentParser()
p.add_argument("--out", default="runs/offline-smoke20")
a = p.parse_args()
sc = json.loads((worker.ROOT / "configs/orders20.json").read_text())
out = worker.ROOT / a.out
out.mkdir(parents=True, exist_ok=False)
by = {
    r["id"]: [o["id"] for o in sc["orders"] if o["route"] == r["id"]]
    for r in sc["routes"]
}
start = time.monotonic()
seed, err = worker.construct(
    sc, [by["C430"] + by["C280I"], by["C280R"], by["CFLAT"]], policy=0, seed=0
)
assert seed is not None, err
worker.save(out / "seed.json", seed)
result = worker.solve(
    sc,
    dict(
        mode="seeded_search",
        seed_path=str(out / "seed.json"),
        preference="none",
        threads=16,
        seconds=45,
        target_minutes=720,
        stop_at_target=True,
    ),
    out,
)
result["total_s"] = time.monotonic() - start
worker.save(out / "result.json", result)
print(json.dumps(result, ensure_ascii=False))
assert result["valid"]
assert result["cp_solver_called"]
assert result["solution_callbacks"] > 0
assert not result["fixed_hint_values"]
