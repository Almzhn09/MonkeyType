from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from audit_sim.data import Episode
from audit_sim.fixtures import dump_json, dump_jsonl
from audit_sim.report import render_report, report_directory
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

    def test_progress_counts_each_record_once_and_matches_canonical_evidence(self):
        report, trajectory = run_policy(A08, "reconcile")
        findings = report["submitted_findings"]
        evidence, row_id, *_ = findings[0]["evidence_ids"]
        findings[0]["evidence_ids"] = [f" {evidence.lower()} ", row_id.lower()]
        report["submitted_findings"] = findings[:1]
        report["steps"] = 4
        report["reward"] = -0.4
        episode = Episode.load(A08)
        observations = [episode.documents[evidence], episode.documents[evidence], episode.records["ledger"][row_id]]
        actions = [
            {"action": "get_document", "doc_id": evidence},
            {"action": "get_document", "doc_id": evidence},
            {"action": "get_record", "table": "ledger", "id": row_id},
        ]
        trajectory = [
            dict(row, policy="reconcile", action=action, observation={"result": observed})
            for row, action, observed in zip(trajectory, actions, observations)
        ]
        html = render_report([report], trajectory)
        self.assertIn('data-progress="[[0,0],[1,1],[1,1],[2,2]]"', html)
        self.assertIn('data-reward="-0.4"', html)
        self.assertIn("Записано 3 из 4 шагов", html)
        self.assertIn(f"ledger · {row_id}", html)
        self.assertNotIn("evidence missing", html)

    def test_report_rejects_invalid_report_details_and_step_order(self):
        report, trajectory = run_policy(A08, "reconcile")
        with self.assertRaisesRegex(ValueError, "invalid duration"):
            render_report([dict(report, duration_seconds=-1)], [])
        with self.assertRaisesRegex(ValueError, "malformed submitted findings"):
            render_report([dict(report, submitted_findings=["not a finding"])], [])
        with self.assertRaisesRegex(ValueError, "steps must be consecutive"):
            render_report([report], [dict(trajectory[1], policy="reconcile")])

    def test_amount_bars_compare_within_currency_only(self):
        report, _ = run_policy(A08, "reconcile")
        first, second = report["submitted_findings"]
        self.assertEqual(render_report([report], []).count('class="amount-track"'), 2)
        report["submitted_findings"] = [first, dict(second, currency="USD")]
        self.assertNotIn('class="amount-track"', render_report([report], []))


if __name__ == "__main__":
    unittest.main()
