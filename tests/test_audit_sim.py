from collections import defaultdict
from pathlib import Path
import errno
import json
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest import mock

from audit_sim.calculator import calculate
from audit_sim.data import Episode
from audit_sim.environment import AuditEnv
from audit_sim.fixtures import DEV_CASES, dump_json, make_dev, make_episode
from audit_sim.runner import run_policy
from audit_sim.scoring import maximum_matches, oracle_submission, score_submission


class TestFixtures(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        output = TemporaryDirectory()
        cls.addClassCleanup(output.cleanup)
        cls.directory = Path(output.name)
        make_dev(cls.directory)
        cls.mixed = cls.directory / "demo-mixed"
        cls.clean = cls.directory / "demo-clean"
        cls.episode = Episode.load(cls.mixed)

    def reset_env(self, directory=None, **options):
        env = AuditEnv(**options)
        env.reset(400, {"episode": directory or self.mixed})
        return env

    def test_schema_currency_and_lookup(self):
        env = self.reset_env()
        row, reward, terminated, truncated, info = env.step({
            "action": "get_record", "table": "ledger", "id": "R-row-001",
        })
        self.assertIs(type(row["result"]["debit_minor"]), int)
        self.assertIsNone(row["result"]["payment_id"])
        self.assertEqual((reward, terminated, truncated, info), (0.0, False, False, {}))

        invoice, *_ = env.step({
            "action": "get_record", "table": "invoices", "id": row["result"]["invoice_id"],
        })
        doc, *_ = env.step({"action": "get_document", "doc_id": invoice["result"]["doc_id"]})
        self.assertEqual(doc["result"], invoice["result"])
        fx, *_ = env.step({"action": "get_record", "table": "fx", "id": "2026-11-30:EUR:USD"})
        self.assertEqual(fx["result"]["rate"], "1.25")

    def test_reset_isolated_and_forwarded_seed(self):
        seen_seeds = []

        def loader(seed, config):
            seen_seeds.append(seed)
            return Episode.load(config["episode"])

        env = AuditEnv(loader=loader)
        initial, info = env.reset(400, {"episode": self.mixed})
        self.assertEqual(info, {})
        self.assertEqual(initial["steps_remaining"], 100)
        first_page, *_ = env.step({"action": "list_records", "table": "invoices"})
        first_page["result"][0]["line_items"][0]["amount_minor"] = 1
        env.step({"action": "submit", "findings": oracle_submission(env.episode)})
        self.assertEqual(env.score.tp, 2)
        final_initial, _ = env.reset(401, {"episode": self.clean})
        self.assertEqual(final_initial["manifest"]["episode_id"], "demo-clean")
        self.assertEqual(final_initial["steps_remaining"], 100)
        self.assertIsNone(env.score)
        self.assertFalse(env.terminated)
        self.assertEqual(seen_seeds, [400, 401])
        env.reset(400, {"episode": self.mixed})
        restored, *_ = env.step({"action": "list_records", "table": "invoices"})
        self.assertNotEqual(restored["result"][0]["line_items"][0]["amount_minor"], 1)

    def test_pagination_limits_and_cursors(self):
        env = self.reset_env()
        action = {"action": "list_records", "table": "ledger", "limit": 2}
        first, *_ = env.step(action)
        repeated, *_ = env.step(action)
        self.assertEqual(first["result"], repeated["result"])
        self.assertEqual(first["next_cursor"], repeated["next_cursor"])
        second, *_ = env.step(action | {"cursor": first["next_cursor"], "limit": 50})
        self.assertEqual(second["result"], self.episode.tables["ledger"][2:])
        self.assertIsNone(second["next_cursor"])
        invalid, *_ = env.step(action | {"limit": 51})
        self.assertEqual(invalid["error"], "invalid_limit")
        for cursor in ("ledger:-1", "invoices:2", "ledger:1000", False):
            invalid, *_ = env.step(action | {"cursor": cursor})
            self.assertEqual(invalid["error"], "invalid_cursor")

    def test_unknown_actions_consume_budget_and_hide_private_files(self):
        env = self.reset_env()
        probes = (
            ({"action": "missing"}, "unknown_action"),
            ({"action": "get_record", "table": "truth", "id": "R-issue-1"}, "unknown_table"),
            ({"action": "get_document", "doc_id": "private/truth.json"}, "unknown_id"),
            (["calculator", "1+1"], "unknown_action"),
        )
        for index, (action, error) in enumerate(probes, 1):
            observed, reward, ended, truncated, info = env.step(action)
            self.assertEqual(observed, {"error": error, "steps_remaining": 100 - index})
            self.assertEqual((reward, ended, truncated, info), (0.0, False, False, {}))

    def test_submit_at_100_and_repeat_submission(self):
        env = self.reset_env(mode="train")
        for _ in range(99):
            env.step({"action": "calculator", "expression": "1"})
        valid = {"action": "submit", "findings": oracle_submission(self.episode)}
        observed, reward, terminated, truncated, _ = env.step(valid)
        self.assertEqual(observed["steps_remaining"], 0)
        self.assertAlmostEqual(reward, 1 - 99 * 0.002)
        self.assertTrue(terminated)
        self.assertFalse(truncated)
        original = env.score
        observed, reward, terminated, truncated, _ = env.step({"action": "submit"})
        self.assertIs(env.score, original)
        self.assertEqual(observed["error"], "episode_finished")
        self.assertEqual((reward, terminated, truncated), (0.0, True, False))

    def test_step_limit_and_timeout_have_empty_results(self):
        env = self.reset_env(mode="train")
        for _ in range(100):
            _, reward, ended, truncated, _ = env.step({"action": "calculator", "expression": "0"})
        self.assertEqual((reward, ended, truncated), (-1, False, True))
        self.assertEqual(env.reason, "step_limit")
        self.assertIsNone(env.submission)

        ticks = iter((10, 50))
        env = self.reset_env(clock=lambda: next(ticks), timeout=1)
        result = env.step({"action": "submit", "findings": oracle_submission(self.episode)})
        self.assertEqual(result[1:], (0.0, False, True, {}))
        self.assertEqual(env.steps, 0)
        self.assertEqual(env.reason, "timeout")
        self.assertIsNone(env.submission)

    def test_worker_deadline(self):
        report, trajectory = run_policy(self.mixed, "random", timeout=0.000001)
        self.assertEqual(report["reason"], "timeout")
        self.assertTrue(report["truncated"])
        self.assertEqual(report["submitted_findings"], [])
        self.assertEqual(trajectory, [])

    def test_modes_and_invalid_structure(self):
        payload = {"action": "submit", "findings": oracle_submission(self.episode)}
        closed = self.reset_env()
        train = self.reset_env(mode="train")
        closed_transition = closed.step(payload)
        self.assertEqual(closed_transition, ({"result": "episode_finished", "steps_remaining": 99}, 0.0, True, False, {}))
        self.assertEqual(train.step(payload)[1], 1.0)
        self.assertEqual(train.score, closed.score)

        for invalid in (None, [], {"episode_id": "wrong", "findings": []}):
            env = self.reset_env(mode="train")
            self.assertEqual(env.step({"action": "submit", "findings": invalid})[1:4], (-1.0, True, False))
            self.assertEqual(env.reason, "invalid_submission")

    def test_submission_limits_and_types(self):
        payload = oracle_submission(self.episode)
        overrides = (
            {"claimed_amount_minor": True}, {"evidence_ids": ["x"] * 11},
            {"explanation": "x" * 1001}, {"affected_ids": []}, {"currency": "usd"},
        )
        for override in overrides:
            item = payload["findings"][0] | override
            invalid = payload | {"findings": [item]}
            self.assertTrue(score_submission(self.episode, invalid, 1).invalid_submission)
        self.assertTrue(score_submission(
            self.episode, payload | {"findings": payload["findings"] * 16}, 1,
        ).invalid_submission)

    def test_scoring_controls(self):
        payload = oracle_submission(self.episode)
        exact = score_submission(self.episode, payload, 1)
        silent = score_submission(self.episode, payload | {"findings": []}, 1)
        self.assertEqual((exact.tp, exact.fp, exact.fn, exact.f1), (2, 0, 0, 1))
        self.assertGreater(exact.reward, silent.reward)
        self.assertAlmostEqual(exact.reward - score_submission(self.episode, payload, 2).reward, 0.002)
        clean = Episode.load(self.clean)
        silent = score_submission(clean, {"episode_id": "demo-clean", "findings": []}, 0)
        self.assertEqual((silent.precision, silent.recall, silent.f1, silent.reward), (0, 0, 1, 1))
        false_positive = score_submission(clean, payload | {"episode_id": "demo-clean"}, 0)
        self.assertEqual((false_positive.fp, false_positive.f1, false_positive.reward), (2, 0, -1))

    def test_matching_proof_validation_duplicate_and_tolerances(self):
        payload = oracle_submission(self.episode)
        first, second = payload["findings"]
        cases = (
            {"affected_ids": ["guessed-id"]}, {"evidence_ids": first["evidence_ids"][:-1]},
            {"evidence_ids": [*first["evidence_ids"], "guessed-id"]}, {"currency": "EUR"},
            {"claimed_amount_minor": first["claimed_amount_minor"] + 1},
        )
        for override in cases:
            result = score_submission(self.episode, payload | {"findings": [first | override, second]}, 0)
            self.assertEqual((result.tp, result.fp, result.fn), (1, 1, 1), override)
        duplicate = first | {"evidence_ids": first["evidence_ids"][::-1], "explanation": "Rephrased"}
        result = score_submission(self.episode, payload | {"findings": [first, duplicate, second]}, 0)
        self.assertEqual((result.tp, result.fp, result.fn), (2, 1, 0))
        for issue, exact in zip(self.episode.issues, payload["findings"]):
            overrides = {issue["type"]: 1}
            for difference, expected in ((1, 1), (2, 0)):
                candidate = exact | {"claimed_amount_minor": exact["claimed_amount_minor"] + difference}
                result = score_submission(self.episode, payload | {"findings": [candidate]}, 0, overrides)
                self.assertEqual(result.tp, expected)
        self.assertEqual(maximum_matches([[0, 1], [0]]), 2)

    def test_fixture_balance_and_all_four_types_match_after_relabeling(self):
        with TemporaryDirectory() as output:
            pair = Path(output)
            for index, (name, errors) in enumerate(DEV_CASES.items()):
                original = Episode.load(self.directory / name)
                make_episode(pair, name, seed=400 + index, errors=errors, permute=True)
                changed = Episode.load(pair)
                self.assertEqual({issue["type"] for issue in original.issues}, set(errors))
                compared = []
                for episode in (original, changed):
                    totals = defaultdict(int)
                    for row in episode.tables["ledger"]:
                        totals[row["currency"]] += row["debit_minor"] - row["credit_minor"]
                    self.assertFalse(any(totals.values()))
                    compared.append(score_submission(episode, oracle_submission(episode), 0))
                self.assertEqual(compared[0], compared[1])
                self.assertEqual(compared[1].f1, 1.0)

    def test_alternate_evidence_and_unavailable_truth(self):
        folder = self.directory / "altered"
        make_episode(folder, "altered", seed=1, errors=("duplicate_invoice",))
        path = folder / "private" / "truth.json"
        truth = json.loads(path.read_text(encoding="utf-8"))
        alternate = ["R-doc-invoice-1", "R-row-002", "R-row-004"]
        truth["issues"][0]["evidence_sets"].append(alternate)
        dump_json(path, truth)
        episode = Episode.load(folder)
        payload = oracle_submission(episode)
        payload["findings"][0]["evidence_ids"] = alternate
        self.assertEqual(score_submission(episode, payload, 0).tp, 1)
        truth["issues"][0]["evidence_sets"].append(["not-an-exposed-record"])
        dump_json(path, truth)
        with self.assertRaisesRegex(ValueError, "unavailable"):
            Episode.load(folder)

    def test_public_projection_and_version_guard(self):
        folder = self.directory / "metadata"
        make_episode(folder, "metadata", seed=1234)
        manifest = folder / "public" / "manifest.json"
        data = json.loads(manifest.read_text(encoding="utf-8"))
        dump_json(manifest, data | {"seed": 1234, "truth_path": "/private/truth.json"})
        env = AuditEnv()
        observation, _ = env.reset(1234, {"episode": folder})
        self.assertNotIn("seed", observation["manifest"])
        self.assertNotIn("truth_path", observation["manifest"])
        dump_json(manifest, data | {"schema_version": 2})
        with self.assertRaisesRegex(ValueError, "Unsupported schema version"):
            env.reset(1234, {"episode": folder})

    def test_policy_results_replay_and_no_terminal_leak(self):
        reconcile, actions = run_policy(self.mixed, "reconcile")
        self.assertEqual((reconcile["tp"], reconcile["fp"], reconcile["fn"], reconcile["f1"]), (2, 0, 0, 1))
        self.assertEqual(len(actions), reconcile["steps"])
        self.assertTrue(actions[-1]["terminated"])
        self.assertTrue(all(row["reward"] == 0 for row in actions))
        self.assertNotIn("evidence_sets", json.dumps(actions))
        self.assertNotIn("provenance", json.dumps(actions))
        self.assertEqual(run_policy(self.mixed, "random", policy_seed=10)[1],
                         run_policy(self.mixed, "random", policy_seed=10)[1])
        clean, _ = run_policy(self.clean, "reconcile")
        broad, _ = run_policy(self.clean, "mark_all")
        self.assertEqual(clean["submitted_findings"], [])
        self.assertGreater(clean["reward"], broad["reward"])
        oracle, actions = run_policy(self.mixed, "oracle")
        self.assertEqual(oracle["f1"], 1)
        self.assertEqual(actions, [])
        self.assertNotIn("submitted_findings", oracle)

    def isolation_kinds(self):
        # The audit hook is the macOS/Windows path; exercise it everywhere so Linux CI covers it.
        return ("seccomp", "audit-hook") if sys.platform.startswith("linux") else ("audit-hook",)

    def test_worker_isolation_blocks_file_network_and_process_access(self):
        trusted_file = self.mixed / "private" / "truth.json"
        code = """
import os
import socket
import subprocess
import sys
from audit_sim import sandbox

assert os.path.isfile(sys.argv[1])
sandbox.isolation_kind = lambda: sys.argv[2]
sandbox.enter_worker_sandbox()
for call in (lambda: open(sys.argv[1]), lambda: socket.socket(),
             lambda: subprocess.run([sys.executable, "-c", "pass"])):
    try:
        call()
    except PermissionError as error:
        print(error.errno)
    else:
        raise AssertionError("Sandbox allowed the operation")
"""
        root = str(Path(__file__).resolve().parents[1])
        for kind in self.isolation_kinds():
            with self.subTest(kind=kind):
                completed = subprocess.run(
                    [sys.executable, "-B", "-c", code, str(trusted_file), kind],
                    capture_output=True, text=True, check=True, timeout=10, cwd=root,
                )
                self.assertEqual(completed.stdout.splitlines(), [str(errno.EPERM)] * 3)

    def test_policies_run_under_audit_hook_isolation(self):
        root = Path(__file__).resolve().parents[1]
        expected, expected_actions = run_policy(self.mixed, "reconcile")
        with TemporaryDirectory() as temp:
            shim = Path(temp) / "worker.py"
            shim.write_text(
                "import sys\n"
                f"sys.path.insert(0, {str(root)!r})\n"
                "from audit_sim import sandbox\n"
                "sandbox.isolation_kind = lambda: 'audit-hook'\n"
                "from audit_sim.worker import main\n"
                "main()\n",
                encoding="utf-8",
            )
            with mock.patch("audit_sim.runner.WORKER", shim):
                for policy in ("random", "reconcile", "mark_all"):
                    with self.subTest(policy=policy):
                        report, _ = run_policy(self.mixed, policy)
                        self.assertEqual(report["reason"], "submitted")
                actual, actions = run_policy(self.mixed, "reconcile")
        self.assertEqual(actual["submitted_findings"], expected["submitted_findings"])
        self.assertEqual(actual["f1"], expected["f1"])
        self.assertEqual(actions, expected_actions)


class TestCalculator(unittest.TestCase):
    def test_decimal_and_operators(self):
        self.assertEqual(calculate("0.1 + 0.2"), "0.3")
        self.assertEqual(calculate("-(6 + 2) / 4 * .5"), "-1.0")

    def test_invalid(self):
        for value in ("1/0", "2**8", "a", "1 2", "(1+", "", "1e9", "9" * 257, None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                calculate(value)


if __name__ == "__main__":
    unittest.main()
