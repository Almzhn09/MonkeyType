"""A05-A07: manifest/policy consistency, file formats, private files."""

import csv
import datetime as dt
import hashlib
import io
import json
import os
import re
import unittest

from fingen.config import canonical_json
from fingen.money import CURRENCY_EXPONENTS
from fingen.timeutil import TIMEZONES
from tests.gen_support import CONFIGS, episode

TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$")
RATE_RE = re.compile(r"^\d+(\.\d{1,6})?$")
LEDGER_HEADER = ["row_id", "entry_id", "timestamp", "account", "debit_minor", "credit_minor",
                 "currency", "entity_id", "invoice_id", "payment_id"]
INVOICE_KEYS = {"doc_id", "invoice_id", "entity_id", "issued_at", "due_at", "currency",
                "total_minor", "line_items"}
LINE_KEYS = {"line_id", "description", "quantity", "unit_price_minor", "amount_minor"}
PAYMENT_KEYS = {"doc_id", "payment_id", "entity_id", "timestamp", "currency", "amount_minor",
                "invoice_refs"}
PUBLIC = ["fx.csv", "invoices.jsonl", "ledger.csv", "manifest.json", "payments.jsonl", "policy.json"]


def is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def raw(path):
    with open(path, "rb") as fh:
        return fh.read()


class ManifestPolicyTest(unittest.TestCase):
    def test_manifest_policy_consistent(self):
        for name in CONFIGS:
            pub = episode(name)["public"]
            m, p = pub["manifest"], pub["policy"]
            with self.subTest(config=name):
                self.assertEqual(m["schema_version"], "1.0")
                self.assertEqual(p["schema_version"], "1.0")
                self.assertEqual(m["episode_id"], p["episode_id"])
                self.assertTrue(m["episode_id"].startswith("EP-"))
                self.assertEqual(m["base_currency"], p["base_currency"])
                self.assertEqual(p["fx"]["target_currency"], m["base_currency"])
                self.assertEqual(p["fx"]["date_rule"], "event_local_date")
                self.assertEqual(p["fx"]["rate_direction"], "target_per_source")
                self.assertEqual(m["rounding"], p["rounding"])
                self.assertIn(m["rounding"]["mode"], ("half_up", "half_even"))
                self.assertEqual(p["period"], {"start": m["period_start"], "end": m["period_end"],
                                               "end_exclusive": True, "timezone": m["timezone"],
                                               "utc_offset": m["utc_offset"]})
                self.assertEqual(TIMEZONES[m["timezone"]], m["utc_offset"])
                for k in ("period_start", "period_end", "extract_start", "extract_end"):
                    self.assertRegex(m[k], TS_RE)
                    self.assertTrue(m[k].endswith(m["utc_offset"]))
                    self.assertTrue(m[k][11:19] == "00:00:00")
                ps, pe = (dt.datetime.fromisoformat(m[k]) for k in ("period_start", "period_end"))
                es, ee = (dt.datetime.fromisoformat(m[k]) for k in ("extract_start", "extract_end"))
                self.assertEqual(ps.day, 1)
                self.assertEqual(pe.day, 1)
                self.assertTrue(es < ps < pe < ee)
                for cur, e in m["currency_exponents"].items():
                    self.assertEqual(CURRENCY_EXPONENTS[cur], e)
                self.assertIn(m["base_currency"], m["currency_exponents"])
                self.assertEqual(set(p["accounts"]), {"ar", "revenue", "bank", "unapplied", "expense"})
                codes = [p["accounts"][r] for r in ("ar", "revenue", "bank", "unapplied")] \
                    + p["accounts"]["expense"]
                self.assertEqual(len(codes), len(set(codes)))
                self.assertEqual(p["allocation"], {"rule": "per_invoice_refs",
                                                   "rounding_remainder": "last_ref",
                                                   "unapplied_account_role": "unapplied"})

    def test_counts_and_hashes(self):
        for name in CONFIGS:
            e = episode(name)
            pub, m = e["public"], e["public"]["manifest"]
            with self.subTest(config=name):
                self.assertEqual(sorted(os.listdir(e["pub"])), PUBLIC)
                self.assertEqual(m["counts"], {
                    "ledger_rows": len(pub["ledger"]),
                    "entries": len({r["entry_id"] for r in pub["ledger"]}),
                    "invoices": len(pub["invoices"]), "payments": len(pub["payments"]),
                    "documents": len(pub["invoices"]) + len(pub["payments"]),
                    "fx_rates": len(pub["fx"])})
                self.assertEqual(set(m["files"]), set(PUBLIC) - {"manifest.json"})
                for fname, digest in m["files"].items():
                    self.assertEqual(hashlib.sha256(raw(os.path.join(e["pub"], fname))).hexdigest(),
                                     digest)


class FileFormatTest(unittest.TestCase):
    def test_encoding_and_line_endings(self):
        for name in CONFIGS:
            d = episode(name)["pub"]
            for fname in PUBLIC:
                data = raw(os.path.join(d, fname))
                data.decode("utf-8")
                self.assertNotIn(b"\r", data)
                self.assertTrue(data.endswith(b"\n"))
                self.assertNotIn(b"NaN", data)

    def test_ledger_csv(self):
        for name in CONFIGS:
            e = episode(name)
            text = raw(os.path.join(e["pub"], "ledger.csv")).decode("utf-8")
            records = list(csv.reader(io.StringIO(text)))
            self.assertEqual(records[0], LEDGER_HEADER)
            for rec in records[1:]:
                self.assertEqual(len(rec), len(LEDGER_HEADER))
                row = dict(zip(LEDGER_HEADER, rec))
                self.assertRegex(row["timestamp"], TS_RE)
                self.assertRegex(row["debit_minor"], r"^\d+$")
                self.assertRegex(row["credit_minor"], r"^\d+$")
                for k in ("row_id", "entry_id", "account"):
                    self.assertTrue(row[k])
                for k in ("row_id", "entry_id", "entity_id", "invoice_id", "payment_id"):
                    self.assertEqual(row[k], row[k].strip().upper())
                    self.assertNotIn(" ", row[k])
            # loader maps empty fields to None
            self.assertTrue(any(r["invoice_id"] is None for r in e["public"]["ledger"]))
            self.assertTrue(any(r["payment_id"] is None for r in e["public"]["ledger"]))
            # order: (timestamp, row_id)
            keys = [(dt.datetime.fromisoformat(r["timestamp"]), r["row_id"])
                    for r in e["public"]["ledger"]]
            self.assertEqual(keys, sorted(keys))
            self.assertEqual(len({r["row_id"] for r in e["public"]["ledger"]}), len(keys))

    def test_documents_jsonl(self):
        for name in CONFIGS:
            e = episode(name)
            for fname, keys in (("invoices.jsonl", INVOICE_KEYS), ("payments.jsonl", PAYMENT_KEYS)):
                lines = raw(os.path.join(e["pub"], fname)).decode("utf-8").split("\n")
                self.assertEqual(lines[-1], "")
                objs = [json.loads(line) for line in lines[:-1]]
                ids = [o["doc_id"] for o in objs]
                self.assertEqual(ids, sorted(ids))
                for o in objs:
                    self.assertEqual(set(o), keys)
                    self.assertIn(o["currency"], e["public"]["manifest"]["currency_exponents"])
                    if fname == "invoices.jsonl":
                        self.assertRegex(o["issued_at"], TS_RE)
                        self.assertRegex(o["due_at"], TS_RE)
                        self.assertTrue(is_int(o["total_minor"]) and o["total_minor"] > 0)
                        for li in o["line_items"]:
                            self.assertEqual(set(li), LINE_KEYS)
                            for k in ("quantity", "unit_price_minor", "amount_minor"):
                                self.assertTrue(is_int(li[k]))
                    else:
                        self.assertRegex(o["timestamp"], TS_RE)
                        self.assertTrue(is_int(o["amount_minor"]))
                        for ref in o["invoice_refs"]:
                            self.assertEqual(set(ref), {"invoice_id", "amount_minor"})
                            self.assertTrue(is_int(ref["amount_minor"]))
            all_ids = [o["doc_id"] for o in e["public"]["invoices"] + e["public"]["payments"]]
            self.assertEqual(len(all_ids), len(set(all_ids)))

    def test_fx_csv(self):
        for name in CONFIGS:
            pub = episode(name)["public"]
            self.assertEqual(pub["fx_header"], ["date", "source_currency", "target_currency", "rate"])
            keys = [(r["date"], r["source_currency"]) for r in pub["fx"]]
            self.assertEqual(keys, sorted(keys))
            for r in pub["fx"]:
                self.assertRegex(r["date"], r"^\d{4}-\d{2}-\d{2}$")
                self.assertRegex(r["rate"], RATE_RE)
                self.assertGreater(float(r["rate"]), 0)

    def test_no_private_metadata_in_public_files(self):
        for name in CONFIGS:
            e = episode(name)
            h = e["ep"].history
            blob = b"".join(raw(os.path.join(e["pub"], f)) for f in PUBLIC).decode("utf-8")
            for word in ("distractor", "near_boundary", "utc_edge", "\"role\"", "unapplied_minor",
                         "provenance", "truth", "issue_id", "seed", "tags", "vendor"):
                self.assertFalse(word in blob, f"{name}: {word}")
            for ent in h.entities.values():
                self.assertFalse(ent.name in blob, ent.name)
            for role in ("ar", "revenue", "bank", "unapplied"):
                acct_name = h.account_names[h.accounts[role]]
                self.assertFalse(acct_name in blob, acct_name)
            self.assertNotIn(str(h.seed), json.dumps(e["public"]["manifest"]))


class PrivateFilesTest(unittest.TestCase):
    def test_truth_and_provenance(self):
        for name in CONFIGS:
            e = episode(name)
            truth, prov = e["private"]["truth"], e["private"]["provenance"]
            m = e["public"]["manifest"]
            with self.subTest(config=name):
                self.assertEqual(sorted(os.listdir(e["priv"])), ["provenance.json", "truth.json"])
                self.assertEqual(truth, {"schema_version": "1.0", "episode_id": m["episode_id"],
                                         "base_currency": m["base_currency"], "issues": []})
                self.assertEqual(set(prov), {"schema_version", "episode_id", "generator_version",
                                             "config", "config_sha256", "seed", "baseline_sha256",
                                             "changes", "distractors"})
                self.assertEqual(prov["episode_id"], m["episode_id"])
                self.assertEqual(prov["seed"], CONFIGS[name]["seed"])
                self.assertEqual(prov["config"]["seed"], prov["seed"])
                self.assertEqual(hashlib.sha256(canonical_json(prov["config"])).hexdigest(),
                                 prov["config_sha256"])
                self.assertEqual(prov["changes"], [])
                # clean episode: baseline == published files
                for fname in PUBLIC:
                    self.assertEqual(prov["baseline_sha256"][fname],
                                     hashlib.sha256(raw(os.path.join(e["pub"], fname))).hexdigest())
                doc_ids = {d["doc_id"] for d in e["public"]["invoices"] + e["public"]["payments"]}
                for rec in prov["distractors"]:
                    self.assertIn("kind", rec)
                    for did in rec["doc_ids"]:
                        self.assertIn(did, doc_ids)
                kinds = {r["kind"] for r in prov["distractors"]}
                dis = prov["config"]["distractors"]
                for k in ("split_payments", "advances", "refunds", "repeated_amounts", "utc_boundary"):
                    self.assertEqual(sum(1 for r in prov["distractors"] if r["kind"] == k), dis[k], k)
                self.assertTrue(kinds <= {"split_payments", "advances", "refunds",
                                          "repeated_amounts", "utc_boundary",
                                          "multi_invoice_payment"})

    def test_public_and_private_dirs_separate(self):
        e = episode("almaty")
        self.assertIn(os.path.join("public", "train"), e["pub"])
        self.assertIn(os.path.join("private", "train"), e["priv"])
        self.assertEqual(os.path.basename(e["pub"]), os.path.basename(e["priv"]))


if __name__ == "__main__":
    unittest.main()
