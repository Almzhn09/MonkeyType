from pathlib import Path
import json
import os
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from audit_sim.__main__ import episode_locations, evaluate
from audit_sim.fixtures import dump_json, dump_jsonl, make_episode
from audit_sim.replay import replay

A08 = Path(__file__).resolve().parents[1] / "fincheck" / "fixtures" / "a08"


class TestAuditRecordings(unittest.TestCase):
    def setUp(self):
        output = TemporaryDirectory()
        self.addCleanup(output.cleanup)
        self.output = Path(output.name)
        self.trace = self.output / "trajectories.jsonl"
        self.reports = self.output / "reports.json"
        self.locations = episode_locations(A08)

    def session(self, actions, *args):
        return subprocess.run(
            [sys.executable, "-m", "audit_sim", "session", str(A08), "--output", str(self.output), *args],
            input=actions, text=True, capture_output=True, timeout=5, check=True,
        )

    def test_interactive_session_replays_closed_and_open(self):
        for mode, reward in (("closed", 0.0), ("train", -1.0)):
            with self.subTest(mode=mode):
                response = self.session(
                    '{"action":"get_document","doc_id":"DA1K9"}\n'
                    '{"action":"calculator","expression":NaN}\n'
                    '{"action":"submit","findings":{"episode_id":"EP-CHK-A08","findings":[]}}\n',
                    "--mode", mode,
                )
                initial, document, invalid, terminal = map(json.loads, response.stdout.splitlines())
                self.assertEqual(initial["observation"]["steps_remaining"], 100)
                self.assertNotIn("seed", initial["observation"]["manifest"])
                self.assertEqual(document["observation"]["result"]["doc_id"], "DA1K9")
                self.assertEqual(invalid["observation"]["error"], "unknown_action")
                self.assertEqual(terminal["reward"], reward)
                self.assertTrue(terminal["terminated"])
                self.assertEqual(terminal["info"], {})
                result = replay(self.locations, self.trace, reports_path=self.reports)
                self.assertEqual((result["sessions"], result["steps"]), (1, 3))
                self.assertEqual(result["reports"][0]["reward"], -1)

    def test_replay_detects_forged_observations_and_scores(self):
        self.session(
            '{"action":"calculator","expression":"1+1"}\n'
            '{"action":"submit","findings":{"episode_id":"EP-CHK-A08","findings":[]}}\n',
        )
        trace = [json.loads(line) for line in self.trace.read_text().splitlines()]
        forged = dict(trace[0], observation={"result": "3", "steps_remaining": 99})
        dump_jsonl(self.trace, [forged, trace[1]])
        with self.assertRaisesRegex(ValueError, "observation differs at step 1"):
            replay(self.locations, self.trace, mode="closed")
        dump_jsonl(self.trace, trace)
        reports = json.loads(self.reports.read_text())
        original_fingerprint = reports[0]["initial_observation_sha256"]
        reports[0]["initial_observation_sha256"] = "0" * 64
        dump_json(self.reports, reports)
        with self.assertRaisesRegex(ValueError, "initial observation differs"):
            replay(self.locations, self.trace, reports_path=self.reports)
        reports[0]["initial_observation_sha256"] = original_fingerprint
        reports[0]["f1"] = 1
        dump_json(self.reports, reports)
        with self.assertRaisesRegex(ValueError, "evaluator report differs for f1"):
            replay(self.locations, self.trace, reports_path=self.reports)

    def test_eof_truncated_without_a_fictitious_action(self):
        result = self.session("")
        initial, terminal = map(json.loads, result.stdout.splitlines())
        self.assertEqual(initial["observation"]["steps_remaining"], 100)
        self.assertTrue(terminal["truncated"])
        replayed = replay(self.locations, self.trace, reports_path=self.reports)
        self.assertEqual(replayed["steps"], 0)
        self.assertEqual(replayed["reports"][0]["reason"], "input_closed")
        with self.assertRaisesRegex(ValueError, "No recorded"):
            replay(self.locations, self.trace, mode="closed")

    def test_escaped_unicode_is_recorded_and_forged_findings_are_rejected(self):
        response = self.session(
            '{"action":"calculator","expression":"\\ud800"}\n'
            '{"action":"submit","findings":{"episode_id":"EP-CHK-A08","findings":[]}}\n',
        )
        self.assertEqual(json.loads(response.stdout.splitlines()[1])["observation"]["error"], "invalid_expression")
        self.assertEqual(replay(self.locations, self.trace, reports_path=self.reports)["steps"], 2)
        reports = json.loads(self.reports.read_text())
        reports[0]["submitted_findings"] = ["not in the submitted action"]
        dump_json(self.reports, reports)
        with self.assertRaisesRegex(ValueError, "differs for submitted_findings"):
            replay(self.locations, self.trace, reports_path=self.reports)

    def test_disconnected_output_preserves_a_replayable_report(self):
        reader, writer = os.pipe()
        os.close(reader)
        try:
            subprocess.run(
                [sys.executable, "-m", "audit_sim", "session", str(A08), "--output", str(self.output)],
                stdin=subprocess.DEVNULL, stdout=writer, timeout=5, check=True,
            )
        finally:
            os.close(writer)
        result = replay(self.locations, self.trace, reports_path=self.reports)
        self.assertEqual((result["steps"], result["reports"][0]["reason"]), (0, "output_closed"))

    def test_watchdog_returns_a_public_terminal_without_an_action(self):
        with subprocess.Popen(
            [sys.executable, "-m", "audit_sim", "session", str(A08), "--timeout", "0.5",
             "--output", str(self.output)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
        ) as process:
            try:
                start = json.loads(process.stdout.readline())
                self.assertEqual(start["observation"]["steps_remaining"], 100)
                terminal = json.loads(process.stdout.readline())
                self.assertTrue(terminal["truncated"])
                self.assertEqual(terminal["reward"], 0)
                self.assertEqual(process.wait(timeout=3), 0)
            finally:
                if process.poll() is None:
                    process.kill()
        replayed = replay(self.locations, self.trace, reports_path=self.reports)
        self.assertEqual(replayed["reports"][0]["reason"], "timeout")

    def test_parallel_runs_preserve_log_order_and_results(self):
        dataset = self.output / "dataset"
        make_episode(dataset / "first", "first", seed=1)
        make_episode(dataset / "second", "second", seed=2, errors=("duplicate_invoice",))
        options = {"choices": ("random", "reconcile", "oracle"), "policy_seed": 123}
        sequential, _, first_trace = evaluate(dataset, jobs=1, **options)
        concurrent, _, second_trace = evaluate(dataset, jobs=2, **options)
        for group in (sequential, concurrent):
            for report in group:
                del report["duration_seconds"]
        self.assertEqual(sequential, concurrent)
        self.assertEqual(first_trace, second_trace)


if __name__ == "__main__":
    unittest.main()
