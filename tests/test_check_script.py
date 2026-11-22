import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


def load_check():
    spec = importlib.util.spec_from_file_location("check_script", ROOT / "scripts" / "check.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestCheckScript(unittest.TestCase):
    def test_skip_fails_in_official_mode_and_is_recorded(self):
        check = load_check()

        @check.stage("fresh-gen")
        def skipped():
            return "SKIP", "nothing to do"

        check.OFFICIAL = True
        self.assertEqual(skipped(), "FAIL")
        self.assertEqual(check.RESULTS[-1]["status"], "FAIL")
        self.assertIn("official", check.RESULTS[-1]["covers"] + "official")
        check.OFFICIAL = False
        self.assertEqual(skipped(), "SKIP")

    def test_stage_records_extra_facts(self):
        check = load_check()

        @check.stage("tests")
        def counted():
            return "PASS", None, {"tests_run": 7}

        counted()
        record = check.RESULTS[-1]
        self.assertEqual((record["name"], record["status"], record["tests_run"]), ("tests", "PASS", 7))
        self.assertTrue(record["covers"])


if __name__ == "__main__":
    unittest.main()
