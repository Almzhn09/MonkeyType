"""fincheck.matcher_stub on the A08 control episode and synthetic edge cases."""
import copy
import json
import os
import unittest

from fincheck.load import canon_ids, load_private, load_public
from fincheck.matcher_stub import match, match_reason, scores

A08 = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "fincheck", "fixtures", "a08")


class TestA08Control(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pub = load_public(os.path.join(A08, "public"))
        cls.issues = load_private(os.path.join(A08, "private")).truth["issues"]
        cls.ids = cls.pub.id_space()
        with open(os.path.join(A08, "control_submission.json"), encoding="utf-8") as fh:
            cls.control = json.load(fh)

    def run_match(self, findings):
        return match(findings, self.issues, self.ids)

    def findings(self):
        return copy.deepcopy(self.control["findings"])

    def test_control_answer_f1_is_one(self):
        res = self.run_match(self.findings())
        self.assertEqual((res.tp, res.fp, res.fn, res.f1), (2, 0, 0, 1.0))
        self.assertEqual(self.control["episode_id"], self.pub.manifest["episode_id"])

    def test_removing_required_evidence_breaks_match(self):
        for idx in range(2):
            for drop in self.control["findings"][idx]["evidence_ids"]:
                f = self.findings()
                f[idx]["evidence_ids"].remove(drop)
                res = self.run_match(f)
                self.assertEqual((res.tp, res.fp, res.fn), (1, 1, 1), drop)
                self.assertLess(res.f1, 1.0)

    def test_alternative_evidence_set_matches(self):
        f = self.findings()
        alt = self.issues[1]["evidence_sets"][1]
        f[1]["evidence_ids"] = list(alt)
        self.assertEqual(self.run_match(f).tp, 2)

    def test_extra_existing_evidence_allowed(self):
        f = self.findings()
        f[0]["evidence_ids"] += ["INV-77-041", "FX:2026-03-18:EUR:KZT", "CN-RT5W"]
        self.assertEqual(self.run_match(f).tp, 2)

    def test_wrong_currency(self):
        f = self.findings()
        f[0]["currency"] = "EUR"
        self.assertEqual(self.run_match(f).tp, 1)

    def test_amount_outside_tolerance(self):
        for delta in (1, -1):
            f = self.findings()
            f[0]["claimed_amount_minor"] += delta  # duplicate_invoice tolerance 0
            self.assertEqual(self.run_match(f).tp, 1)

    def test_nonexistent_evidence_id(self):
        f = self.findings()
        f[1]["evidence_ids"].append("R0000000")
        self.assertEqual(self.run_match(f).tp, 1)
        f = self.findings()
        f[1]["evidence_ids"].append("FX:2026-05-30:EUR:KZT")  # outside fx.csv
        self.assertEqual(self.run_match(f).tp, 1)

    def test_too_many_evidence_ids(self):
        f = self.findings()
        extra = sorted(r["row_id"] for r in self.pub.ledger)[:10]
        f[0]["evidence_ids"] += extra
        self.assertEqual(self.run_match(f).tp, 1)

    def test_wrong_affected_or_type(self):
        f = self.findings()
        f[0]["affected_ids"] = ["DA4Q2"]
        self.assertEqual(self.run_match(f).tp, 1)
        f = self.findings()
        f[0]["affected_ids"] = ["DA1K9", "DA4Q2"]
        self.assertEqual(self.run_match(f).tp, 1)
        f = self.findings()
        f[1]["type"] = "fx_conversion"
        self.assertEqual(self.run_match(f).tp, 1)

    def test_duplicate_findings_are_fp(self):
        f = self.findings()
        f.append(copy.deepcopy(f[0]))
        res = self.run_match(f)
        self.assertEqual((res.tp, res.fp, res.fn), (2, 1, 0))
        self.assertEqual(res.unmatched_findings, [2])  # earlier finding wins the tie

    def test_canonicalisation(self):
        f = self.findings()
        f[0]["affected_ids"] = [" da1k9 ", "DA1K9"]
        f[0]["evidence_ids"] = [x.lower() + "  " for x in f[0]["evidence_ids"]] + \
            [f[0]["evidence_ids"][0]]
        self.assertEqual(self.run_match(f).tp, 2)
        self.assertEqual(canon_ids([" b", "A", "a ", "B"]), ["A", "B"])

    def test_malformed_findings_do_not_crash(self):
        f = self.findings() + [None, {"type": "duplicate_invoice"},
                               {"type": "duplicate_invoice", "affected_ids": "DA1K9",
                                "claimed_amount_minor": "2500000", "currency": "KZT",
                                "evidence_ids": None}]
        res = self.run_match(f)
        self.assertEqual((res.tp, res.fp), (2, 3))

    def test_bool_amount_rejected(self):
        f = self.findings()
        iss = dict(self.issues[0], expected_amount_minor=1)
        f[0]["claimed_amount_minor"] = True
        self.assertIsNotNone(match_reason(f[0], iss, self.ids))


class TestSynthetic(unittest.TestCase):
    IDS = {"D1", "D2", "R1", "R2", "R3", "R4", "FX:2026-03-18:EUR:KZT"}

    def issue(self, iid, typ="fx_conversion", doc="D1", amount=100, sets=None, tol=None):
        return {"issue_id": iid, "type": typ, "affected_ids": [doc],
                "expected_amount_minor": amount, "currency": "KZT",
                "tolerance_minor": (1 if typ == "fx_conversion" else 0) if tol is None else tol,
                "evidence_sets": sets or [[doc, "R1", "FX:2026-03-18:EUR:KZT"]]}

    def finding(self, typ="fx_conversion", doc="D1", amount=100, ev=None):
        return {"type": typ, "affected_ids": [doc], "claimed_amount_minor": amount,
                "currency": "KZT", "evidence_ids": ev or [doc, "R1", "FX:2026-03-18:EUR:KZT"],
                "explanation": ""}

    def test_fx_tolerance_boundary(self):
        iss = [self.issue("I1")]
        for amount, tp in ((99, 1), (101, 1), (98, 0), (102, 0)):
            self.assertEqual(match([self.finding(amount=amount)], iss, self.IDS).tp, tp, amount)

    def test_maximum_matching_uses_augmenting_path(self):
        # finding 0 satisfies both issues, finding 1 only I1: greedy would give 1 TP.
        i1 = self.issue("I1", sets=[["D1", "R1"]])
        i2 = self.issue("I2", sets=[["D1", "R2"]])
        f0 = self.finding(ev=["D1", "R1", "R2"])
        f1 = self.finding(ev=["D1", "R1"])
        res = match([f0, f1], [i1, i2], self.IDS)
        self.assertEqual((res.tp, res.fp, res.fn), (2, 0, 0))
        self.assertEqual(res.pairs, [(0, "I2"), (1, "I1")])

    def test_deterministic(self):
        i1 = self.issue("I1", sets=[["D1", "R1"]])
        i2 = self.issue("I2", sets=[["D1", "R1"]])
        fs = [self.finding(ev=["D1", "R1"]) for _ in range(3)]
        a = match(fs, [i2, i1], self.IDS)
        b = match(fs, [i1, i2], self.IDS)
        self.assertEqual(a.pairs, b.pairs)
        self.assertEqual(a.pairs, [(0, "I1"), (1, "I2")])

    def test_clean_episode_scores(self):
        self.assertEqual(match([], [], self.IDS).f1, 1.0)
        res = match([self.finding()], [], self.IDS)
        self.assertEqual((res.fp, res.f1, res.precision, res.recall), (1, 0.0, 0.0, 0.0))
        self.assertEqual(match([], [self.issue("I1")], self.IDS).f1, 0.0)

    def test_scores_formula(self):
        p, r, f1 = scores(2, 1, 1)
        self.assertAlmostEqual(p, 2 / 3)
        self.assertAlmostEqual(r, 2 / 3)
        self.assertAlmostEqual(f1, 4 / 6)


if __name__ == "__main__":
    unittest.main()
