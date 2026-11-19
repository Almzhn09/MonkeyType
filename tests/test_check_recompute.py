"""fincheck.recompute: reference postings, reconciler, audit, evidence sufficiency."""
import os
import tempfile
import unittest
from decimal import Decimal

from fincheck.fixtures import builder as fx
from fincheck.fixtures.builder import R
from fincheck.load import load_public
from fincheck.recompute import (RecomputeError, amount_from_evidence, audit, convert,
                                expected_payment, reconcile)


def _build(spec):
    tmp = tempfile.TemporaryDirectory()
    pub, _ = fx.write_episode(spec, os.path.join(tmp.name, "pub"),
                              os.path.join(tmp.name, "prv"))
    return tmp, load_public(pub)


class TestConvert(unittest.TestCase):
    def test_half_up_vs_half_even(self):
        self.assertEqual(convert(3333, Decimal("498.5"), 2, 2, "half_up"), 1661501)
        self.assertEqual(convert(3333, Decimal("498.5"), 2, 2, "half_even"), 1661500)
        self.assertEqual(convert(3335, Decimal("498.5"), 2, 2, "half_even"), 1662498)

    def test_exponent_shift(self):
        # 1234 JPY (exp 0) at 3.456789 KZT/JPY (exp 2) = 1234 × 3.456789 × 100
        self.assertEqual(convert(1234, Decimal("3.456789"), 0, 2, "half_up"), 426568)
        # 1.234 KWD (exp 3) at 1500.5 KZT → 1234 × 1500.5 / 10 = 185161.7 → 185162
        self.assertEqual(convert(1234, Decimal("1500.5"), 3, 2, "half_up"), 185162)


class TestCleanDistractors(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp, cls.pub = _build(fx.clean_spec())

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_no_findings(self):
        self.assertEqual(reconcile(self.pub), [])

    def test_no_unexplained_discrepancy(self):
        self.assertEqual(audit(self.pub), [])

    def test_last_ref_rounding_remainder(self):
        exp = expected_payment(self.pub, self.pub.payment_by_pid["PAY-55108"])
        self.assertEqual(exp["bank_dr"], 4985000)
        self.assertEqual(exp["ar"], [("INV-2603-120", 1661501), ("INV-2603-121", 3323499)])
        self.assertEqual(exp["unapplied_cr"], 0)

    def test_advance_goes_to_unapplied(self):
        exp = expected_payment(self.pub, self.pub.payment_by_pid["PAY-55103"])
        self.assertEqual(exp["unapplied_cr"], 2000000)

    def test_refund(self):
        exp = expected_payment(self.pub, self.pub.payment_by_pid["PAY-55104"])
        self.assertEqual((exp["kind"], exp["bank_cr"]), ("refund", 2000000))

    def test_utc_boundary_uses_local_date(self):
        inv = self.pub.invoices_by_doc["DQ4D2"]
        self.assertFalse(self.pub.in_period(inv["issued_at"]))
        self.assertEqual(str(self.pub.local_date(inv["issued_at"])), "2026-04-01")
        self.assertEqual(str(self.pub.local_date(
            self.pub.invoices_by_doc["DQ9J6"]["issued_at"])), "2026-03-18")


class TestFourTypes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.spec = fx.four_types_spec()
        cls.tmp, cls.pub = _build(cls.spec)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_reconciler_finds_exactly_the_issues(self):
        got = sorted((f["type"], f["affected_ids"][0], f["claimed_amount_minor"])
                     for f in reconcile(self.pub))
        want = sorted((i["type"], i["affected_ids"][0], i["expected_amount_minor"])
                      for i in self.spec["issues"])
        self.assertEqual(got, want)

    def test_first_evidence_set_used(self):
        by_type = {f["type"]: f for f in reconcile(self.pub)}
        for iss in self.spec["issues"]:
            self.assertTrue(set(iss["evidence_sets"][0]) <= set(
                by_type[iss["type"]]["evidence_ids"]), iss["issue_id"])
            self.assertLessEqual(len(by_type[iss["type"]]["evidence_ids"]), 10)

    def test_every_evidence_set_sufficient(self):
        for iss in self.spec["issues"]:
            for s in iss["evidence_sets"]:
                self.assertEqual(amount_from_evidence(self.pub, iss["type"],
                                                      iss["affected_ids"][0], s),
                                 iss["expected_amount_minor"], (iss["issue_id"], s))

    def test_insufficient_sets_rejected(self):
        cases = [
            ("duplicate_invoice", "DQ3C9", ["DQ3C9", R("C-ar")]),  # one posting only
            ("fx_conversion", "DQ9J6", ["DQ9J6", R("J-ar"), "FX:2026-03-17:EUR:KZT"]),  # UTC date
            ("fx_conversion", "DQ9J6", ["DQ9J6", R("J-ar")]),  # no FX key
            ("wrong_period", "DQ5E8", [R("E-ar")]),  # no document
            ("wrong_period", "DQ1A7", ["DQ1A7", R("A-ar")]),  # correctly dated
            ("payment_allocation", "DP3M4", ["DP3M4", R("P3-un")]),  # legit advance
        ]
        for typ, doc, s in cases:
            with self.assertRaises(RecomputeError, msg=(typ, s)):
                amount_from_evidence(self.pub, typ, doc, s)


class TestA08(unittest.TestCase):
    def test_a08_findings(self):
        tmp, pub = _build(fx.a08_spec())
        self.addCleanup(tmp.cleanup)
        got = sorted((f["type"], f["affected_ids"][0], f["claimed_amount_minor"])
                     for f in reconcile(pub))
        self.assertEqual(got, [("duplicate_invoice", "DA1K9", 2500000),
                               ("payment_allocation", "PB1T3", 40746667)])
        self.assertEqual(audit(pub), [])


if __name__ == "__main__":
    unittest.main()
