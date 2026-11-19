"""A13/A15 (shared): helpers for the injection tests plus the cross-type checks
(clean episodes, distractors, leakage, determinism, provenance reversal).

Every generated episode is written to disk and checked three ways:
fincheck (independent verifier: PASS, reference reconciler F1 = 1), an
explicit in-test recomputation of each truth amount from the public files,
and structural/leakage assertions.
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import json
import os
import re
import unittest
from collections import defaultdict

from fincheck.load import load_public as fc_load_public
from fincheck.recompute import reconcile
from fincheck.verify import verify_episode
from fingen.generate import build_episode, generate_episode
from fingen.reader import load_private, load_public
from tests.gen_support import REPO, convert, local_date, recompute_problems, tmpdir

TIMEZONES = ("Asia/Almaty", "Asia/Tokyo", "America/Bogota", "Asia/Kolkata", "Asia/Dubai",
             "Etc/UTC")
SEEDS = (11, 23, 37, 41, 59, 73)
CAUSE_RE = re.compile(r"^(\w+)\[([\w/]+)\]")
EXAMPLES = os.path.join(REPO, "configs", "examples")


def config_for(seed: int, tz: str, **extra) -> dict:
    cfg = {"seed": seed, "timezone": tz, "base_currency": "KZT",
           "foreign_currencies": ["EUR", "USD"] if seed % 2 else ["JPY", "KWD"],
           "rounding_mode": "half_up" if seed % 3 else "half_even"}
    cfg.update(extra)
    return cfg


def make(cfg: dict) -> dict:
    """Generate ``cfg`` on disk, verify with fincheck, and load everything."""
    ep, pub_dir, priv_dir = generate_episode(cfg, tmpdir())
    rep = verify_episode(pub_dir, priv_dir)
    return {"ep": ep, "pub_dir": pub_dir, "priv_dir": priv_dir, "rep": rep,
            "public": load_public(pub_dir), "private": load_private(priv_dir)}


def variants(e: dict) -> dict[str, str]:
    """issue_id -> variant label parsed from the private provenance causes."""
    out = {}
    for ch in e["private"]["provenance"]["changes"]:
        m = CAUSE_RE.match(ch["cause"])
        if m:
            out.setdefault(ch["issue_id"], m.group(2).split("/")[0])
    return out


class Public:
    """Tiny independent view over public files for explicit recomputation."""

    def __init__(self, pub: dict) -> None:
        self.m, self.pol = pub["manifest"], pub["policy"]
        self.base = self.m["base_currency"]
        self.off = self.m["utc_offset"]
        self.mode = self.pol["rounding"]["mode"]
        self.exps = self.m["currency_exponents"]
        self.acc = self.pol["accounts"]
        self.fx = {(r["date"], r["source_currency"]): r["rate"] for r in pub["fx"]}
        self.ledger = pub["ledger"]
        self.invoices = {d["doc_id"]: d for d in pub["invoices"]}
        self.payments = {d["doc_id"]: d for d in pub["payments"]}
        self.start = dt.datetime.fromisoformat(self.m["period_start"])
        self.end = dt.datetime.fromisoformat(self.m["period_end"])
        self.xstart = dt.datetime.fromisoformat(self.m["extract_start"])
        self.xend = dt.datetime.fromisoformat(self.m["extract_end"])

    def in_period(self, ts: str) -> bool:
        return self.start <= dt.datetime.fromisoformat(ts) < self.end

    def in_extract(self, ts: str) -> bool:
        return self.xstart <= dt.datetime.fromisoformat(ts) < self.xend

    def ldate(self, ts: str) -> dt.date:
        return local_date(ts, self.off)

    def rate(self, day: dt.date, cur: str) -> str:
        return self.fx[(day.isoformat(), cur)]

    def conv(self, amount: int, cur: str, ts: str) -> int:
        if cur == self.base:
            return amount
        return convert(amount, cur, self.base, self.rate(self.ldate(ts), cur), self.mode,
                       self.exps)

    def entries(self, rows) -> dict[str, list]:
        out = defaultdict(list)
        for r in rows:
            out[r["entry_id"]].append(r)
        return dict(out)

    def invoice_rows(self, inv) -> list:
        return [r for r in self.ledger if r["invoice_id"] == inv["invoice_id"]
                and r["payment_id"] is None]

    def payment_rows(self, pay) -> list:
        return [r for r in self.ledger if r["payment_id"] == pay["payment_id"]]

    def doc(self, doc_id):
        return self.invoices.get(doc_id) or self.payments.get(doc_id)


class InjectCase(unittest.TestCase):
    """Assertions shared by the per-type and combo tests."""

    def assertVerified(self, e: dict, n_issues: int) -> None:
        rep = e["rep"]
        self.assertTrue(rep.ok, "\n".join(rep.errors))
        self.assertEqual(rep.f1, 1.0)
        self.assertEqual(rep.issues, n_issues)
        self.assertEqual(rep.findings, n_issues)
        self.assertEqual(len(e["private"]["truth"]["issues"]), n_issues)

    def assertNoLeak(self, e: dict) -> None:
        blob = b""
        for f in sorted(os.listdir(e["pub_dir"])):
            with open(os.path.join(e["pub_dir"], f), "rb") as fh:
                blob += fh.read()
        self.assertEqual(sorted(os.listdir(e["pub_dir"])),
                         ["fx.csv", "invoices.jsonl", "ledger.csv", "manifest.json",
                          "payments.jsonl", "policy.json"])
        for issue in e["private"]["truth"]["issues"]:
            self.assertNotIn(issue["issue_id"].encode(), blob)
        for ch in e["private"]["provenance"]["changes"]:
            self.assertNotIn(ch["cause"].encode(), blob)
        for word in (b"inject", b"duplicate", b"wrong", b"distractor", b"twin", b"trap"):
            self.assertNotIn(word, blob.lower())
        # Same structure as any clean episode: headers and document keys.
        pub = e["public"]
        self.assertEqual(pub["ledger_header"],
                         ["row_id", "entry_id", "timestamp", "account", "debit_minor",
                          "credit_minor", "currency", "entity_id", "invoice_id", "payment_id"])
        self.assertEqual(pub["fx_header"], ["date", "source_currency", "target_currency", "rate"])
        for inv in pub["invoices"]:
            self.assertEqual(sorted(inv), ["currency", "doc_id", "due_at", "entity_id",
                                           "invoice_id", "issued_at", "line_items",
                                           "total_minor"])
        for pay in pub["payments"]:
            self.assertEqual(sorted(pay), ["amount_minor", "currency", "doc_id", "entity_id",
                                           "invoice_refs", "payment_id", "timestamp"])
        # Injected rows take IDs of the baseline style (same prefix and length).
        changed = {c["target"] for c in e["private"]["provenance"]["changes"]}
        base_ids = [r["row_id"] for r in pub["ledger"] if r["row_id"] not in changed]

        def shape(i: str) -> tuple:
            return re.sub(r"[0-9A-Z]", "X", i)

        for rid in changed:
            self.assertIn(shape(rid), {shape(b) for b in base_ids})
        rows = pub["ledger"]
        keys = [(dt.datetime.fromisoformat(r["timestamp"]), r["row_id"]) for r in rows]
        self.assertEqual(keys, sorted(keys))

    def assertReversible(self, e: dict) -> None:
        """Undo provenance changes in reverse order -> baseline ledger hash."""
        prov = e["private"]["provenance"]
        with open(os.path.join(e["pub_dir"], "ledger.csv"), encoding="utf-8", newline="") as fh:
            records = list(csv.reader(fh))
        header, body = records[0], {r[0]: r for r in records[1:]}

        def as_rec(row):
            return ["" if row[c] is None else str(row[c]) for c in header]

        for c in sorted(prov["changes"], key=lambda c: -c["order"]):
            if c["after"] is not None:
                self.assertEqual(body.pop(c["after"]["row_id"]), as_rec(c["after"]))
            if c["before"] is not None:
                body[c["before"]["row_id"]] = as_rec(c["before"])
        rows = sorted(body.values(), key=lambda r: (dt.datetime.fromisoformat(r[2]), r[0]))
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\n")
        w.writerow(header)
        w.writerows(rows)
        self.assertEqual(hashlib.sha256(buf.getvalue().encode()).hexdigest(),
                         prov["baseline_sha256"]["ledger.csv"])

    def assertIssueShape(self, e: dict) -> None:
        prov = e["private"]["provenance"]
        distractor_docs = {d for rec in prov["distractors"] for d in rec["doc_ids"]}
        tol = {"duplicate_invoice": 0, "payment_allocation": 0, "fx_conversion": 1,
               "wrong_period": 0}
        for issue in e["private"]["truth"]["issues"]:
            self.assertEqual(len(issue["affected_ids"]), 1)
            self.assertNotIn(issue["affected_ids"][0], distractor_docs)
            self.assertEqual(issue["tolerance_minor"], tol[issue["type"]])
            self.assertGreater(issue["expected_amount_minor"], 0)
            self.assertEqual(issue["currency"], e["public"]["manifest"]["base_currency"])
            for s in issue["evidence_sets"]:
                self.assertLessEqual(len(set(s)), 10)
                self.assertEqual(s[0], issue["affected_ids"][0])
            types = {c["type"] for c in prov["changes"] if c["issue_id"] == issue["issue_id"]}
            self.assertEqual(types, {issue["type"]})

    def assertAmountsRecompute(self, e: dict) -> None:
        """Explicit recomputation of every truth amount from public files."""
        p = Public(e["public"])
        for issue in e["private"]["truth"]["issues"]:
            doc = p.doc(issue["affected_ids"][0])
            got = RECOMPUTE[issue["type"]](self, p, doc)
            self.assertLessEqual(abs(got - issue["expected_amount_minor"]),
                                 issue["tolerance_minor"], (issue, got))

    def assertAll(self, e: dict, n_issues: int) -> None:
        self.assertVerified(e, n_issues)
        self.assertIssueShape(e)
        self.assertAmountsRecompute(e)
        self.assertNoLeak(e)
        self.assertReversible(e)


# --------------------------------------------------------------------------- recompute

def recompute_duplicate(tc, p: Public, inv) -> int:
    entries = p.entries(p.invoice_rows(inv))
    tc.assertEqual(len(entries), 2)
    issued = p.ldate(inv["issued_at"])
    correct = p.conv(inv["total_minor"], inv["currency"], inv["issued_at"])

    def ar(rows):
        return sum(r["debit_minor"] for r in rows if r["account"] == p.acc["ar"])

    originals = [e for e, rows in entries.items()
                 if p.ldate(rows[0]["timestamp"]) == issued and ar(rows) == correct]
    tc.assertEqual(len(originals), 1)
    (extra,) = [rows for e, rows in entries.items() if e != originals[0]]
    # duplicate stays in the period, on a later local date (not also wrong_period)
    tc.assertTrue(p.in_period(extra[0]["timestamp"]))
    tc.assertGreater(p.ldate(extra[0]["timestamp"]), issued)
    rev = sum(r["credit_minor"] for r in extra if r["account"] == p.acc["revenue"])
    tc.assertEqual(rev, ar(extra))
    return ar(extra)


def recompute_allocation(tc, p: Public, pay) -> int:
    tc.assertEqual(sum(r["amount_minor"] for r in pay["invoice_refs"]), pay["amount_minor"])
    rows = p.payment_rows(pay)
    tc.assertEqual(len(p.entries(rows)), 1)
    bank = sum(r["debit_minor"] for r in rows if r["account"] == p.acc["bank"])
    ar = sum(r["credit_minor"] for r in rows if r["account"] == p.acc["ar"])
    un = [r for r in rows if r["account"] == p.acc["unapplied"]]
    for r in un:
        tc.assertIsNone(r["invoice_id"])
        tc.assertEqual(r["payment_id"], pay["payment_id"])
    tc.assertEqual(sum(r["credit_minor"] for r in un), bank - ar)
    return bank - ar


def recompute_fx(tc, p: Public, doc) -> int:
    tc.assertNotEqual(doc["currency"], p.base)
    if "invoice_id" in doc and "total_minor" in doc:
        rows = p.invoice_rows(doc)
        expected = p.conv(doc["total_minor"], doc["currency"], doc["issued_at"])
        recorded = sum(r["debit_minor"] for r in rows if r["account"] == p.acc["ar"])
        tc.assertEqual(p.ldate(rows[0]["timestamp"]), p.ldate(doc["issued_at"]))
    else:
        rows = p.payment_rows(doc)
        expected = p.conv(abs(doc["amount_minor"]), doc["currency"], doc["timestamp"])
        recorded = sum(r["debit_minor"] for r in rows if r["account"] == p.acc["bank"])
        # allocations follow the same (wrong) conversion: Σ Cr AR + Cr unapplied = Dr bank
        tc.assertEqual(sum(r["credit_minor"] for r in rows), recorded)
    tc.assertEqual(len(p.entries(rows)), 1)
    return abs(recorded - expected)


def recompute_wrong_period(tc, p: Public, inv) -> int:
    rows = p.invoice_rows(inv)
    tc.assertEqual(len(p.entries(rows)), 1)
    ts = rows[0]["timestamp"]
    tc.assertNotEqual(p.in_period(ts), p.in_period(inv["issued_at"]))
    tc.assertTrue(p.in_extract(ts))
    ar = sum(r["debit_minor"] for r in rows if r["account"] == p.acc["ar"])
    # amounts unchanged: still the event-date conversion (no fx error)
    tc.assertEqual(ar, p.conv(inv["total_minor"], inv["currency"], inv["issued_at"]))
    return ar


RECOMPUTE = {"duplicate_invoice": recompute_duplicate, "payment_allocation": recompute_allocation,
             "fx_conversion": recompute_fx, "wrong_period": recompute_wrong_period}


# --------------------------------------------------------------------------- tests

FULL_DISTRACTORS = {"split_payments": 3, "refunds": 2, "repeated_amounts": 3,
                    "utc_boundary": 4, "advances": 3}


class CleanAndDistractorTest(InjectCase):
    def test_clean_configs_have_empty_truth(self):
        for i, tz in enumerate(TIMEZONES):
            with self.subTest(tz=tz):
                e = make(config_for(SEEDS[i], tz, distractors=FULL_DISTRACTORS))
                self.assertVerified(e, 0)
                self.assertEqual(e["private"]["truth"]["issues"], [])
                self.assertEqual(e["private"]["provenance"]["changes"], [])
                self.assertEqual(reconcile(fc_load_public(e["pub_dir"])), [])
                self.assertEqual(recompute_problems(e["public"]), [])

    def test_every_distractor_kind_present_and_correct(self):
        for i, tz in enumerate(TIMEZONES[:4]):
            cfg = config_for(SEEDS[i], tz, distractors=FULL_DISTRACTORS,
                             size={"invoices": 60, "background_entries": 80},
                             errors={"duplicate_invoice": 2, "payment_allocation": 2,
                                     "fx_conversion": 2, "wrong_period": 2})
            with self.subTest(tz=tz):
                e = make(cfg)
                self.assertVerified(e, 8)
                prov = e["private"]["provenance"]
                kinds = {d["kind"] for d in prov["distractors"]}
                self.assertTrue({"split_payments", "refunds", "repeated_amounts",
                                 "utc_boundary", "advances",
                                 "multi_invoice_payment"} <= kinds, kinds)
                docs = {d for rec in prov["distractors"] for d in rec["doc_ids"]}
                affected = {i["affected_ids"][0] for i in e["private"]["truth"]["issues"]}
                self.assertFalse(docs & affected)
                # No change record touches a distractor document's rows ...
                p = Public(e["public"])
                ids = set()
                for d in docs:
                    doc = p.doc(d)
                    ids |= {doc.get("invoice_id"), doc.get("payment_id")} - {None}
                for ch in prov["changes"]:
                    for row in (ch["before"], ch["after"]):
                        if row is not None:
                            self.assertNotIn(row["invoice_id"] if row["payment_id"] is None
                                             else row["payment_id"], ids)
                # ... their postings still recompute exactly per policy ...
                problems = recompute_problems(e["public"])
                self.assertFalse([x for x in problems if x.split(":")[0] in docs], problems)
                # ... and the reference reconciler flags none of them.
                found = {f["affected_ids"][0] for f in e["rep"].findings_list}
                self.assertFalse(found & docs)
                self.assertEqual(found, affected)

    def test_duplicate_targets_have_same_customer_twin(self):
        for i, tz in enumerate(TIMEZONES):
            cfg = config_for(SEEDS[i], tz, errors={"duplicate_invoice": 2},
                             size={"invoices": 40, "background_entries": 60})
            with self.subTest(tz=tz):
                e = make(cfg)
                self.assertAll(e, 2)
                p = Public(e["public"])
                twins = [r for r in e["private"]["provenance"]["distractors"]
                         if r.get("subtype") == "same_customer_twin"]
                self.assertEqual(len(twins), 2)
                for issue in e["private"]["truth"]["issues"]:
                    inv = p.invoices[issue["affected_ids"][0]]
                    same = [o for o in p.invoices.values() if o["doc_id"] != inv["doc_id"]
                            and o["entity_id"] == inv["entity_id"]
                            and o["currency"] == inv["currency"]
                            and o["total_minor"] == inv["total_minor"]]
                    self.assertTrue(same, "duplicate target without a twin")
                    for o in same:  # the twin is posted exactly once, correctly
                        self.assertEqual(len(p.entries(p.invoice_rows(o))), 1)


class DeterminismTest(InjectCase):
    def test_two_runs_byte_identical_with_injections(self):
        cfg = config_for(97, "Asia/Tokyo", size={"invoices": 60, "background_entries": 60},
                         errors={"duplicate_invoice": 2, "payment_allocation": 2,
                                 "fx_conversion": 2, "wrong_period": 2},
                         combos={"fx_partial_payment_boundary": 1, "duplicate_plus_period": 1})
        a, b = build_episode(cfg), build_episode(cfg)
        self.assertEqual(a.public, b.public)
        self.assertEqual(a.private, b.private)
        self.assertEqual(len(json.loads(a.private["truth.json"])["issues"]), 12)
        c = build_episode(dict(cfg, seed=98))
        self.assertNotEqual(a.public["ledger.csv"], c.public["ledger.csv"])


class ExampleConfigsTest(InjectCase):
    def test_example_configs_generate_and_verify(self):
        names = ["duplicate_invoice", "payment_allocation", "fx_conversion", "wrong_period",
                 "fx_partial_payment_boundary", "duplicate_plus_period", "all_types"]
        for name in names:
            with self.subTest(example=name):
                with open(os.path.join(EXAMPLES, name + ".json"), encoding="utf-8") as fh:
                    cfg = json.load(fh)
                e = make(cfg)
                n = sum(cfg["errors"].values()) + 2 * sum(cfg["combos"].values())
                self.assertGreater(n, 0)
                self.assertAll(e, n)
                types = {i["type"] for i in e["private"]["truth"]["issues"]}
                if name in RECOMPUTE:
                    self.assertEqual(types, {name})
                elif name == "all_types":
                    self.assertEqual(types, set(RECOMPUTE))


if __name__ == "__main__":
    unittest.main()
