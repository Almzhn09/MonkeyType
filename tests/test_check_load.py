"""fincheck.load: strict schema validation with precise messages."""
import json
import os
import tempfile
import unittest

from fincheck.fixtures import builder as fx
from fincheck.load import SchemaError, load_private, load_public


class LoadCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.pub, self.prv = fx.write_episode(fx.a08_spec(), os.path.join(self.tmp.name, "p"),
                                              os.path.join(self.tmp.name, "q"))

    def path(self, name):
        return os.path.join(self.pub, name)

    def read(self, name):
        with open(self.path(name), encoding="utf-8") as fh:
            return fh.read()

    def write(self, name, text, rehash=True):
        with open(self.path(name), "w", encoding="utf-8") as fh:
            fh.write(text)
        if rehash:
            fx.rehash_manifest(self.pub)

    def edit_json(self, name, fn, rehash=True):
        obj = json.loads(self.read(name))
        fn(obj)
        self.write(name, json.dumps(obj, sort_keys=True, indent=2) + "\n", rehash)

    def assertSchemaError(self, needle):
        with self.assertRaises(SchemaError) as cm:
            load_public(self.pub)
        self.assertTrue(any(needle in e for e in cm.exception.errors),
                        f"{needle!r} not in {cm.exception.errors}")


class TestLoadGood(LoadCase):
    def test_loads(self):
        pub = load_public(self.pub)
        self.assertEqual(len(pub.ledger), 26)
        self.assertIsNone(pub.ledger[0]["payment_id"])  # empty CSV field -> None
        self.assertIsInstance(pub.ledger[0]["debit_minor"], int)
        self.assertIn("FX:2026-03-18:EUR:KZT", pub.id_space())
        prv = load_private(self.prv)
        self.assertEqual(len(prv.truth["issues"]), 2)


class TestLoadErrors(LoadCase):
    def test_sha_mismatch(self):
        self.write("fx.csv", self.read("fx.csv") + "", rehash=False)
        text = self.read("invoices.jsonl").replace("Grain storage", "Grain storagE", 1)
        self.write("invoices.jsonl", text, rehash=False)
        self.assertSchemaError("sha256 of invoices.jsonl")

    def test_counts_mismatch(self):
        self.edit_json("manifest.json", lambda m: m["counts"].update(ledger_rows=99), False)
        self.assertSchemaError("counts.ledger_rows = 99")

    def test_float_money_rejected(self):
        self.write("invoices.jsonl",
                   self.read("invoices.jsonl").replace('"total_minor": 2500000',
                                                       '"total_minor": 2500000.0', 1))
        self.assertSchemaError("float")

    def test_line_items_sum(self):
        self.write("invoices.jsonl", self.read("invoices.jsonl").replace(
            '"total_minor": 2500000', '"total_minor": 2500001', 1))
        self.assertSchemaError("Σ line amount_minor")

    def test_unsorted_json_keys(self):
        lines = self.read("payments.jsonl").splitlines()
        obj = json.loads(lines[0])
        lines[0] = json.dumps(dict(reversed(list(obj.items()))))  # keys in reverse order
        self.write("payments.jsonl", "\n".join(lines) + "\n")
        self.assertSchemaError("not sorted")

    def test_debit_and_credit(self):
        rows = self.read("ledger.csv").splitlines()
        parts = rows[1].split(",")
        parts[4] = parts[5] = "5"
        rows[1] = ",".join(parts)
        self.write("ledger.csv", "\n".join(rows) + "\n")
        self.assertSchemaError("exactly one of debit_minor/credit_minor")

    def test_unbalanced_entry(self):
        text = self.read("ledger.csv").replace(",2500000,0,", ",2500001,0,", 1)
        self.write("ledger.csv", text)
        self.assertSchemaError("unbalanced")

    def test_lowercase_id(self):
        self.write("invoices.jsonl", self.read("invoices.jsonl").replace('"DA1K9"', '"da1k9"'))
        self.assertSchemaError("invalid id 'da1k9'")

    def test_z_timestamp_rejected(self):
        self.write("payments.jsonl", self.read("payments.jsonl").replace(
            "2026-03-20T15:00:00+05:00", "2026-03-20T10:00:00Z"))
        self.assertSchemaError("±HH:MM offset")

    def test_ledger_offset_must_be_company(self):
        rows = self.read("ledger.csv").splitlines()
        rows[-1] = rows[-1].replace("+05:00", "+00:00")
        self.write("ledger.csv", "\n".join(rows) + "\n")
        self.assertSchemaError("company offset")

    def test_header(self):
        text = self.read("ledger.csv").replace("entry_id,", "entry,", 1)
        self.write("ledger.csv", text)
        self.assertSchemaError("header must be exactly")

    def test_missing_fx_date(self):
        lines = self.read("fx.csv").splitlines()
        self.write("fx.csv", "\n".join(lines[:5] + lines[6:]) + "\n")
        self.assertSchemaError("missing rate for")

    def test_bad_rate(self):
        self.write("fx.csv", self.read("fx.csv").replace("509.333333", "509.3333333"))
        self.assertSchemaError("≤6 decimals")

    def test_policy_inconsistent(self):
        self.edit_json("policy.json", lambda p: p["rounding"].update(mode="half_even"))
        self.assertSchemaError("rounding differs")

    def test_extra_field_rejected(self):
        self.edit_json("manifest.json", lambda m: m.update(seed=1))
        self.assertSchemaError("unexpected field(s) ['seed']")

    def test_ref_to_unknown_invoice(self):
        self.write("payments.jsonl", self.read("payments.jsonl").replace(
            '"INV-77-050"', '"INV-77-999"'))
        self.assertSchemaError("does not exist")

    def test_ledger_ref_unknown(self):
        self.write("ledger.csv", self.read("ledger.csv").replace("PAY-3102", "PAY-9999"))
        self.assertSchemaError("unknown payment_id PAY-9999")

    def test_truth_tolerance(self):
        p = os.path.join(self.prv, "truth.json")
        with open(p, encoding="utf-8") as fh:
            t = json.load(fh)
        t["issues"][0]["tolerance_minor"] = 5
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(t, fh, sort_keys=True)
        with self.assertRaises(SchemaError) as cm:
            load_private(self.prv)
        self.assertIn("tolerance_minor", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
