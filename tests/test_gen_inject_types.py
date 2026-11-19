"""A09-A12, A15: each error type alone (count 1 and 3) over several seeds and
time zones: fincheck PASS with reconciler F1 = 1, every truth amount
recomputed explicitly from public files, no leakage, reversible provenance,
and every variant of the type exercised."""

from __future__ import annotations

import unittest
from collections import Counter

from tests.test_gen_inject_common import (SEEDS, TIMEZONES, InjectCase, Public, config_for, make,
                                          variants)


class PerTypeCase(InjectCase):
    TYPE = ""
    VARIANTS: tuple = ()

    def run_type(self) -> Counter:
        seen: Counter = Counter()
        for count in (1, 3):
            for i, seed in enumerate(SEEDS):
                tz = TIMEZONES[(i + count) % len(TIMEZONES)]
                cfg = config_for(seed * 10 + count, tz, errors={self.TYPE: count})
                with self.subTest(count=count, seed=seed, tz=tz):
                    e = make(cfg)
                    self.assertAll(e, count)
                    issues = e["private"]["truth"]["issues"]
                    self.assertEqual({x["type"] for x in issues}, {self.TYPE})
                    self.assertEqual(len({x["affected_ids"][0] for x in issues}), count)
                    v = variants(e)
                    self.assertEqual(set(v), {x["issue_id"] for x in issues})
                    seen.update(v.values())
                    self.check_episode(e)
        self.assertEqual(set(seen), set(self.VARIANTS), seen)
        return seen

    def check_episode(self, e) -> None:
        pass


class DuplicateInvoiceTest(PerTypeCase):
    TYPE, VARIANTS = "duplicate_invoice", ("repost",)

    def test_duplicate_invoice(self):
        self.run_type()

    def check_episode(self, e):
        p = Public(e["public"])
        for issue in e["private"]["truth"]["issues"]:
            inv = p.invoices[issue["affected_ids"][0]]
            self.assertTrue(p.in_period(inv["issued_at"]))
            # both evidence sets name rows of two different entries
            for s in issue["evidence_sets"]:
                rows = [r for r in p.ledger if r["row_id"] in s[1:]]
                self.assertEqual(len({r["entry_id"] for r in rows}), 2)


class PaymentAllocationTest(PerTypeCase):
    TYPE, VARIANTS = "payment_allocation", ("missing", "partial")

    def test_payment_allocation(self):
        self.run_type()

    def check_episode(self, e):
        p = Public(e["public"])
        for issue in e["private"]["truth"]["issues"]:
            pay = p.payments[issue["affected_ids"][0]]
            self.assertTrue(p.in_period(pay["timestamp"]))
            self.assertGreater(pay["amount_minor"], 0)
        # other payments that settle the same invoices stay exactly correct
        affected = {i["affected_ids"][0] for i in e["private"]["truth"]["issues"]}
        refs = {r["invoice_id"] for d in affected for r in p.payments[d]["invoice_refs"]}
        for pay in p.payments.values():
            if pay["doc_id"] in affected or not refs & {r["invoice_id"]
                                                         for r in pay["invoice_refs"]}:
                continue
            rows = p.payment_rows(pay)
            self.assertFalse([r for r in rows if r["account"] == p.acc["unapplied"]
                              and r["credit_minor"]
                              and sum(x["amount_minor"] for x in pay["invoice_refs"])
                              == pay["amount_minor"]])


class FxConversionTest(PerTypeCase):
    TYPE, VARIANTS = "fx_conversion", ("utc_date", "adjacent_day", "bad_rate")

    def test_fx_conversion(self):
        self.run_type()

    def check_episode(self, e):
        p = Public(e["public"])
        for issue in e["private"]["truth"]["issues"]:
            doc = p.doc(issue["affected_ids"][0])
            ts = doc.get("issued_at") or doc["timestamp"]
            key = f"FX:{p.ldate(ts).isoformat()}:{doc['currency']}:{p.base}"
            for s in issue["evidence_sets"]:
                self.assertIn(key, s)  # FX key of the CORRECT (local) date


class WrongPeriodTest(PerTypeCase):
    TYPE, VARIANTS = "wrong_period", ("late_posting", "early_posting", "utc_trap")

    def test_wrong_period(self):
        seen = self.run_type()
        self.assertGreaterEqual(seen["utc_trap"], 2)

    def check_episode(self, e):
        p = Public(e["public"])
        v = variants(e)
        for issue in e["private"]["truth"]["issues"]:
            inv = p.invoices[issue["affected_ids"][0]]
            (row,) = [r for r in p.invoice_rows(inv) if r["account"] == p.acc["ar"]]
            if v[issue["issue_id"]] == "utc_trap":
                # issued_at written in +00:00, local and UTC dates on opposite
                # sides of the boundary; the entry is dated by the UTC date.
                self.assertTrue(inv["issued_at"].endswith("+00:00"))
                self.assertNotEqual(p.off, "+00:00")
                utc_d = inv["issued_at"][:10]
                self.assertNotEqual(utc_d, p.ldate(inv["issued_at"]).isoformat())
                self.assertEqual(p.ldate(row["timestamp"]).isoformat(), utc_d)
            elif v[issue["issue_id"]] == "late_posting":
                self.assertTrue(p.in_period(inv["issued_at"]))
            else:
                self.assertFalse(p.in_period(inv["issued_at"]))
                self.assertTrue(p.in_period(row["timestamp"]))


if __name__ == "__main__":
    unittest.main()
