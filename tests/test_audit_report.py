from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from audit_sim.data import Episode
from audit_sim.fixtures import dump_json, dump_jsonl
from audit_sim.report import report_directory
from audit_sim.runner import run_policy
from audit_sim.scoring import explain_submission, oracle_submission

A08 = Path(__file__).resolve().parents[1] / "fincheck" / "fixtures" / "a08"


class TestAuditReport(unittest.TestCase):
    def test_mark_all_uses_actual_document_ids_and_still_scores_poorly(self):
        episode = Episode.load(A08)
        report, _ = run_policy(A08, "mark_all")
        self.assertTrue(all(
            item["affected_ids"][0] in episode.documents for item in report["submitted_findings"]
        ))
        self.assertGreater(report["tp"], 0)
        self.assertGreater(report["fp"], report["tp"])
        self.assertLess(report["reward"], 0)

    def test_private_diagnostics_match_the_score(self):
        episode = Episode.load(A08)
        answer = oracle_submission(episode)
        first = answer["findings"][0]
        answer["findings"] = [
            first, dict(first, explanation="Same finding"),
            dict(first, evidence_ids=["NOT-A-PUBLIC-RECORD"]),
            dict(first, currency="EUR"),
            dict(first, claimed_amount_minor=first["claimed_amount_minor"] + 100),
            dict(first, evidence_ids=first["evidence_ids"][:1]),
            dict(first, affected_ids=["NOT-AN-AFFECTED-ID"]),
        ]
        result = explain_submission(episode, answer, 2)
        self.assertEqual((result["score"]["tp"], result["score"]["fp"], result["score"]["fn"]), (1, 6, 1))
        self.assertEqual([item["status"] for item in result["findings"]], [
            "matched", "duplicate", "unknown_evidence", "wrong_currency",
            "amount_outside_tolerance", "incomplete_evidence", "no_matching_issue",
        ])
        self.assertEqual(result["findings"][1]["duplicate_of"], 0)
        self.assertEqual(len(result["missed"]), 1)

    def test_report_includes_observed_evidence_and_escapes_user_content(self):
        episode = Episode.load(A08)
        first = oracle_submission(episode)["findings"][0]
        first["explanation"] = 'A harmless literal </script><span>that must stay text</span>'
        report = {
            "episode_id": "EP-CHK-A08", "policy": "interactive", "mode": "train",
            "tp": 1, "fp": 0, "fn": 1, "precision": 1.0, "recall": 0.5, "f1": 2 / 3,
            "reward": 0.33, "steps": 1, "duration_seconds": 0.1, "submitted_findings": [first],
        }
        doc = episode.documents[first["evidence_ids"][0]]
        action = {
            "episode_id": report["episode_id"], "policy": report["policy"], "step": 1,
            "action": {"action": "get_document", "doc_id": doc["doc_id"]},
            "observation": {"result": doc}, "reward": 0, "terminated": False, "truncated": False,
        }
        with TemporaryDirectory() as tmp:
            output = Path(tmp)
            dump_json(output / "reports.json", [report])
            dump_jsonl(output / "trajectories.jsonl", [action])
            report_directory(output, output / "report.html")
            html = (output / "report.html").read_text(encoding="utf-8")
        self.assertIn("&lt;/script&gt;&lt;span&gt;", html)
        self.assertIn("Content-Security-Policy", html)
        self.assertIn("Ход расследования", html)
        self.assertIn(doc["doc_id"], html)
        self.assertNotIn("that must stay text</span>", html)


if __name__ == "__main__":
    unittest.main()
