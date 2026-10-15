import copy, json, unittest, sys, tempfile, signal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import worker


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.sc = json.loads((worker.ROOT / "configs/orders20.json").read_text())

    def test_frozen_sources(self):
        self.assertTrue(worker.integrity())

    def test_real_solver_from_scratch_and_audit(self):
        sc = copy.deepcopy(self.sc)
        sc["orders"] = sc["orders"][:1]
        sc["routes"] = [
            dict(
                id=sc["orders"][0]["route"],
                operations=[
                    dict(fire=1, temp=1150, hold_min=60, hold_max=90, forge=10)
                ],
            )
        ]
        req = dict(
            mode="seeded_search",
            preference="none",
            threads=2,
            seconds=15,
            target_minutes=1440,
            stop_at_target=False,
        )
        with tempfile.TemporaryDirectory() as d:
            result = worker.solve(sc, req, Path(d))
            self.assertTrue(result["cp_solver_called"])
            self.assertTrue(result["valid"])
            self.assertEqual(result["day_minutes"], 10)
            self.assertFalse(result["fixed_hint_values"])
            self.assertEqual(result["scope"], "global")

    def test_invalid_holding_hint_repaired_by_solver(self):
        sc = copy.deepcopy(self.sc)
        sc["orders"] = sc["orders"][:1]
        sc["routes"] = [
            dict(
                id=sc["orders"][0]["route"],
                operations=[
                    dict(fire=1, temp=1150, hold_min=60, hold_max=90, forge=10)
                ],
            )
        ]
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            worker.save(
                p / "partial.json",
                dict(heat_start=[300], forge_start=[210], furnace=[0]),
            )
            req = dict(
                mode="seeded_search",
                preference="none",
                threads=2,
                seconds=15,
                target_minutes=1440,
                stop_at_target=False,
                partial_path=str(p / "partial.json"),
            )
            result = worker.solve(sc, req, p)
            self.assertTrue(result["valid"])
            self.assertEqual(result["hint_values"], 3)
            s = json.loads((p / "best_schedule.json").read_text())
            self.assertGreaterEqual(s["heat_end"][0] - s["heat_start"][0], 180)

    def test_cp_improves_complete_seed_instead_of_only_verifying(self):
        sc = copy.deepcopy(self.sc)
        sc["orders"] = sc["orders"][:1]
        sc["routes"] = [
            dict(
                id=sc["orders"][0]["route"],
                operations=[
                    dict(fire=1, temp=1150, hold_min=60, hold_max=90, forge=10)
                ],
            )
        ]
        seed, err = worker.construct(sc, [[sc["orders"][0]["id"]]])
        self.assertIsNone(err)
        for k in ("heat_start", "forge_start", "heat_end", "forge_end"):
            seed[k] = [t + 4320 for t in seed[k]]
        for t in seed["transport"]:
            t["start"] += 4320
            t["end"] += 4320
        self.assertTrue(worker.audit(sc, seed)["valid"])
        self.assertEqual(worker.evaluate_schedule(sc, seed)["score_ticks"], 0)
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            worker.save(p / "seed.json", seed)
            req = dict(
                mode="broad_improve",
                seed_path=str(p / "seed.json"),
                incumbent_path=str(p / "seed.json"),
                preference="none",
                threads=2,
                seconds=15,
                target_minutes=1440,
                stop_at_target=False,
            )
            result = worker.solve(sc, req, p)
            self.assertTrue(result["valid"])
            self.assertEqual(result["day_minutes"], 10)
            self.assertGreater(result["solution_callbacks"], 0)

    def test_target_seed_fast_path_still_calls_solver_and_audits(self):
        sc = copy.deepcopy(self.sc)
        sc["orders"] = sc["orders"][:1]
        sc["routes"] = [
            dict(
                id=sc["orders"][0]["route"],
                operations=[
                    dict(fire=1, temp=1150, hold_min=60, hold_max=90, forge=10)
                ],
            )
        ]
        seed, err = worker.construct(sc, [[sc["orders"][0]["id"]]])
        self.assertIsNone(err)
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            worker.save(p / "seed.json", seed)
            result = worker.solve(
                sc,
                dict(
                    mode="seeded_search",
                    seed_path=str(p / "seed.json"),
                    preference="none",
                    threads=2,
                    seconds=15,
                    target_minutes=10,
                    stop_at_target=True,
                    presolve="auto",
                    probing_level=0,
                ),
                p,
            )
            self.assertTrue(result["target_seed_fast_path"])
            self.assertFalse(result["presolve_enabled"])
            self.assertTrue(result["cp_solver_called"])
            self.assertTrue(result["audit_valid"])
            self.assertEqual(result["day_minutes"], 10)


if __name__ == "__main__":
    unittest.main()
