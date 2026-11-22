from pathlib import Path
import unittest

from scripts.failure_analysis import TYPES, analyse, markdown

ROOT = Path(__file__).resolve().parents[1]


class TestFailureAnalysis(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = analyse(ROOT / "episodes", "dev")

    def test_counts_agree_with_grader(self):
        totals, found, runs = self.result["totals"], self.result["found"], self.result["runs"]
        planted = sum(totals[name] for name in TYPES)
        self.assertEqual(planted, totals["single"] + totals["interacting"])
        for policy, counts in runs.items():
            self.assertEqual(sum(found[policy][name] for name in TYPES), counts["tp"])
            self.assertEqual(counts["tp"] + counts["fn"], planted)
            self.assertEqual(sum(self.result["false_positives"][policy].values()), counts["fp"])

    def test_reference_finds_everything_and_simple_misses_rule_based_errors(self):
        totals, found = self.result["totals"], self.result["found"]
        self.assertEqual({name: found["reconcile"][name] for name in TYPES}, {name: totals[name] for name in TYPES})
        self.assertEqual(found["simple"]["fx_conversion"], 0)
        self.assertEqual(found["simple"]["wrong_period"], 0)
        self.assertIn("| fx_conversion |", markdown(self.result, "dev"))


if __name__ == "__main__":
    unittest.main()
