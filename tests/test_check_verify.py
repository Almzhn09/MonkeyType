"""fincheck.verify end-to-end: good fixtures pass, every class of defect fails."""
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout

from fincheck.__main__ import main
from fincheck.fixtures import builder as fx
from fincheck.fixtures.builder import R
from fincheck.verify import verify_all, verify_episode

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
A08 = os.path.join(ROOT, "fincheck", "fixtures", "a08")


class VerifyCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def build(self, spec, split="train"):
        return fx.build(self.tmp.name, spec, split)

    def assertFails(self, rep, needle):
        self.assertFalse(rep.ok, "verify unexpectedly passed")
        self.assertTrue(any(needle in e for e in rep.errors), f"{needle!r} not in {rep.errors}")

    @staticmethod
    def edit(path, old, new, count=1):
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        assert old in text, old
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text.replace(old, new, count))

    @staticmethod
    def edit_truth(prv, fn):
        p = os.path.join(prv, "truth.json")
        with open(p, encoding="utf-8") as fh:
            t = json.load(fh)
        fn(t)
        with open(p, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(t, sort_keys=True, indent=2) + "\n")


class TestGoodEpisodes(VerifyCase):
    def test_committed_a08_fixture(self):
        rep = verify_episode(os.path.join(A08, "public"), os.path.join(A08, "private"))
        self.assertTrue(rep.ok, rep.errors)
        self.assertEqual((rep.issues, rep.findings, rep.f1), (2, 2, 1.0))
        self.assertEqual(rep.warnings, [])

    def test_committed_a08_fixture_is_current(self):
        fresh = fx.write_a08(os.path.join(self.tmp.name, "a08"))
        for sub in ("public", "private"):
            for name in sorted(os.listdir(os.path.join(A08, sub))):
                with open(os.path.join(A08, sub, name), "rb") as a, \
                        open(os.path.join(fresh, sub, name), "rb") as b:
                    self.assertEqual(a.read(), b.read(), f"{sub}/{name} is stale")

    def test_clean_with_distractors(self):
        rep = verify_episode(*self.build(fx.clean_spec()))
        self.assertTrue(rep.ok, rep.errors)
        self.assertEqual((rep.issues, rep.findings, rep.f1), (0, 0, 1.0))

    def test_four_types(self):
        rep = verify_episode(*self.build(fx.four_types_spec()))
        self.assertTrue(rep.ok, rep.errors)
        self.assertEqual((rep.issues, rep.findings, rep.f1), (4, 4, 1.0))

    def test_verify_all_and_cli(self):
        self.build(fx.clean_spec())
        self.build(fx.a08_spec(), "dev")
        reports, problems = verify_all(self.tmp.name)
        self.assertEqual(problems, [])
        self.assertTrue(all(r.ok for r in reports), [r.errors for r in reports])
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(["verify", "--all", self.tmp.name])
        self.assertEqual(code, 0, out.getvalue())
        self.assertIn("2/2 episodes passed", out.getvalue())


class TestBrokenEpisodes(VerifyCase):
    def test_broken_balance(self):
        pub, prv = self.build(fx.a08_spec())
        self.edit(os.path.join(pub, "ledger.csv"), ",2500000,0,", ",2500100,0,")
        fx.rehash_manifest(pub)
        rep = verify_episode(pub, prv)
        self.assertFails(rep, "unbalanced")
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(main(["verify", "--public", pub, "--private", prv]), 1)
        self.assertIn("FAIL", out.getvalue())

    def test_truth_amount_off_by_one(self):
        pub, prv = self.build(fx.a08_spec())
        self.edit_truth(prv, lambda t: t["issues"][0].update(expected_amount_minor=2500001))
        rep = verify_episode(pub, prv)
        self.assertFails(rep, "!= expected 2500001")
        self.assertFails(rep, "(d) FN issue ISS-7Q1")
        self.assertFails(rep, "(d) FP finding")

    def test_fx_truth_outside_tolerance(self):
        pub, prv = self.build(fx.four_types_spec())
        self.edit_truth(prv, lambda t: t["issues"][2].update(expected_amount_minor=70001))
        self.assertTrue(verify_episode(pub, prv).ok)  # within tolerance 1
        self.edit_truth(prv, lambda t: t["issues"][2].update(expected_amount_minor=70002))
        self.assertFails(verify_episode(pub, prv), "(c) ISS-3")

    def test_unlabeled_issue(self):
        spec = fx.a08_spec()
        spec["issues"] = spec["issues"][:1]
        spec["changes"] = [c for c in spec["changes"] if c[0] == "ISS-7Q1"] + \
                          [(i, "duplicate_invoice", op, t, p) for i, _, op, t, p in
                           spec["changes"] if i == "ISS-9M4"]
        rep = verify_episode(*self.build(spec))
        self.assertFails(rep, "FP finding")

    def test_unexplained_posting_mismatch(self):
        # KZT invoice posted at a wrong amount: no §5 type covers it -> unlabeled
        spec = fx.clean_spec()
        spec["changes"] = [("ISS-X", "fx_conversion", "modify_row", R("B-ar"),
                            {"debit_minor": 8000100}),
                           ("ISS-X", "fx_conversion", "modify_row", R("B-rev"),
                            {"credit_minor": 8000100})]
        rep = verify_episode(*self.build(spec))
        self.assertFails(rep, "(d) unlabeled discrepancy: invoice DQ2B4")

    def test_insufficient_evidence_set(self):
        pub, prv = self.build(fx.four_types_spec())
        self.edit_truth(prv, lambda t: t["issues"][2]["evidence_sets"].append(
            ["DQ9J6", R("J-ar"), "FX:2026-03-17:EUR:KZT"]))
        self.assertFails(verify_episode(pub, prv), "insufficient")

    def test_nonpublic_evidence_id(self):
        pub, prv = self.build(fx.a08_spec())
        self.edit_truth(prv, lambda t: t["issues"][1]["evidence_sets"][0].append("R-GHOST"))
        self.assertFails(verify_episode(pub, prv), "non-public ids ['R-GHOST']")

    def test_provenance_does_not_reproduce_baseline(self):
        pub, prv = self.build(fx.a08_spec())
        p = os.path.join(prv, "provenance.json")
        with open(p, encoding="utf-8") as fh:
            prov = json.load(fh)
        prov["changes"][3]["after"]["credit_minor"] = 1  # mismatch with published row
        with open(p, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(prov, sort_keys=True))
        self.assertFails(verify_episode(pub, prv), "does not equal `after`")

    def test_baseline_not_clean(self):
        # the 'error' already existed before injection -> baseline check fails
        spec = fx.a08_spec()
        spec["ledger"] = [r for r in spec["ledger"] if r["row_id"] != R("b1-ar1")]
        spec["ledger"].append(fx._row(R("b1-un0"), fx.J("b1"), "2026-03-18T12:00:00+05:00",
                                      fx.UNAP, 0, 40746667, "CN-QW2E", None, "PAY-3101"))
        spec["changes"] = spec["changes"][:2]
        spec["issues"] = spec["issues"][:1]
        rep = verify_episode(*self.build(spec))
        self.assertFails(rep, "(b) baseline")

    def test_clean_with_changes_fails(self):
        spec = fx.clean_spec()
        spec["changes"] = [("ISS-Z", "wrong_period", "modify_row", R("X2-exp"),
                            {"debit_minor": 25000000})]
        rep = verify_episode(*self.build(spec))
        self.assertFails(rep, "(e) clean episode has provenance changes")

    def test_leakage_words(self):
        pub, prv = self.build(fx.a08_spec())
        self.edit(os.path.join(pub, "invoices.jsonl"), "Drying service",
                  "Drying service (duplicate)")
        fx.rehash_manifest(pub)
        self.assertFails(verify_episode(pub, prv), "truth-like words")

    def test_leakage_issue_id_and_path(self):
        pub, prv = self.build(fx.a08_spec())
        self.edit(os.path.join(pub, "invoices.jsonl"), "Drying service", "Drying ISS-7Q1")
        self.edit(os.path.join(pub, "invoices.jsonl"), "Seed bags", "/home/gen/out")
        fx.rehash_manifest(pub)
        rep = verify_episode(pub, prv)
        self.assertFails(rep, "private string 'ISS-7Q1'")
        self.assertFails(rep, "filesystem paths")

    def test_extra_public_file(self):
        pub, prv = self.build(fx.a08_spec())
        shutil.copy(os.path.join(prv, "truth.json"), pub)
        self.assertFails(verify_episode(pub, prv), "extra entries ['truth.json']")

    def test_layout_mismatch(self):
        pub, prv = self.build(fx.a08_spec())
        shutil.rmtree(prv)
        _, problems = verify_all(self.tmp.name)
        self.assertTrue(any("present only in public" in p for p in problems))


if __name__ == "__main__":
    unittest.main()
