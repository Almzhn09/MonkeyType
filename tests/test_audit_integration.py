from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from audit_sim.__main__ import episodes
from audit_sim.data import Episode
from audit_sim.environment import AuditEnv
from audit_sim.runner import run_policy
from audit_sim.scoring import oracle_submission, score_submission
from fingen.generate import generate_episode

A08 = Path(__file__).resolve().parents[1] / "fincheck" / "fixtures" / "a08"


class TestAuditIntegration(unittest.TestCase):
    def test_a08_isolated_reconciliation(self):
        report, trajectory = run_policy(A08, "reconcile")
        self.assertEqual((report["tp"], report["fp"], report["fn"]), (2, 0, 0))
        self.assertTrue(trajectory[-1]["terminated"])
        self.assertEqual(trajectory[-1]["reward"], 0)
        self.assertNotIn("seed", trajectory[0]["observation"])

    def test_generated_files_follow_published_contract(self):
        with TemporaryDirectory() as temp:
            _, public, private = generate_episode({
                "seed": 100001, "split": "dev", "errors": {"fx_conversion": 1},
                "base_currency": "KZT", "foreign_currencies": ["JPY"],
            }, temp)
            self.assertEqual(episodes(Path(temp), split="dev"), [(Path(public), Path(private))])
            with self.assertRaisesRegex(ValueError, "No episode manifests"):
                episodes(Path(temp), split="train")
            env = AuditEnv(mode="train")
            observation, _ = env.reset(100001, {"episode": public, "private": private})
            self.assertEqual(observation["manifest"]["schema_version"], "1.0")
            episode = Episode.load(public, private)
            answer = oracle_submission(episode)
            finding = answer["findings"][0]
            fx_id = next(value for value in finding["evidence_ids"] if value.startswith("FX:"))
            fx, *_ = env.step({"action": "get_record", "table": "fx", "id": fx_id})
            self.assertEqual(fx["result"]["source_currency"], "JPY")
            finding["affected_ids"] = [value.lower() for value in finding["affected_ids"]]
            finding["claimed_amount_minor"] += 1
            accepted = score_submission(episode, answer, 1)
            self.assertEqual((accepted.tp, accepted.fp, accepted.fn), (1, 0, 0))
            finding["claimed_amount_minor"] += 1
            rejected = score_submission(episode, answer, 1)
            self.assertEqual((rejected.tp, rejected.fp, rejected.fn), (0, 1, 1))
