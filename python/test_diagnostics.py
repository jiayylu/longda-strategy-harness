import unittest,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"reference"))
from diagnostics import summarize


class DiagnosticsTests(unittest.TestCase):
    def test_day_boundary_and_gaps_are_measured(self):
        sc = {"ticks_per_minute": 3}
        ops = [
            dict(index=0, order="A", fire=1, route="R", lower=6),
            dict(index=1, order="B", fire=1, route="R", lower=6),
        ]
        schedule = dict(
            forge_start=[4290, 4350],
            forge_end=[4350, 4380],
            heat_start=[4280, 4340],
            heat_end=[4286, 4346],
        )
        facts = summarize(sc, schedule, ops)
        self.assertEqual(facts["day1_by_route"]["R"], 10)
        self.assertEqual(facts["largest_press_gaps"][0]["minutes"], 1430)
        self.assertEqual(facts["early_day2_operations"][0]["order"], "B")
        self.assertEqual(facts["largest_hold_excess"][0]["minutes"], 0)


if __name__ == "__main__":
    unittest.main()
