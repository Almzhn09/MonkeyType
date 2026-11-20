"""A16/A17 combos and the mixed configuration: both issues of a combo are
found by the independent verifier with their own type/document, amounts
recompute independently, and the documented injection order holds."""

from __future__ import annotations

import unittest
from collections import Counter

from tests.test_gen_inject_common import (SEEDS, TIMEZONES, InjectCase, Public, config_for, make,
                                          variants)
from tests.gen_support import convert, local_date


class FxPartialPaymentBoundaryTest(InjectCase):
    def test_combo(self):
        kinds = Counter()
        for i, seed in enumerate(SEEDS):
            tz = TIMEZONES[i % len(TIMEZONES)]
            with self.subTest(seed=seed, tz=tz):
                e = make(config_for(seed, tz, combos={"fx_partial_payment_boundary": 1}))
                self.assertAll(e, 2)
                issues = {x["type"]: x for x in e["private"]["truth"]["issues"]}
                self.assertEqual(set(issues), {"fx_conversion", "payment_allocation"})
                (doc_id,) = {x["affected_ids"][0] for x in issues.values()}
                p = Public(e["public"])
                pay = p.payments[doc_id]
                self.assertNotEqual(pay["currency"], p.base)
                self.assertTrue(p.in_period(pay["timestamp"]))
                ts = pay["timestamp"]
                utc_d = local_date(ts, "+00:00")
                loc_d = p.ldate(ts)
                rows = p.payment_rows(pay)
                bank = sum(r["debit_minor"] for r in rows if r["account"] == p.acc["bank"])
                ar = sum(r["credit_minor"] for r in rows if r["account"] == p.acc["ar"])
                un = sum(r["credit_minor"] for r in rows if r["account"] == p.acc["unapplied"])
                # (1) fx: the bank line is the conversion at the UTC-date rate
                v = variants(e)[issues["fx_conversion"]["issue_id"]]
                kinds[v] += 1
                if tz != "Etc/UTC" and v == "utc_date":
                    self.assertNotEqual(utc_d, loc_d)
                    self.assertTrue(abs((loc_d - p.start.date()).days) <= 1 or
                                    abs((p.end.date() - loc_d).days) <= 1, "near boundary")
                    self.assertEqual(bank, convert(pay["amount_minor"], pay["currency"], p.base,
                                                   p.rate(utc_d, pay["currency"]), p.mode,
                                                   p.exps))
                correct = p.conv(pay["amount_minor"], pay["currency"], ts)
                self.assertEqual(abs(bank - correct),
                                 issues["fx_conversion"]["expected_amount_minor"])
                # (2) allocation on recorded values, independent of the fx step
                self.assertEqual(un, bank - ar)
                self.assertEqual(un, issues["payment_allocation"]["expected_amount_minor"])
                self.assertGreater(ar, 0)  # partial, not missing
                # order: every fx change precedes every allocation change
                ch = e["private"]["provenance"]["changes"]
                fx_o = [c["order"] for c in ch if c["type"] == "fx_conversion"]
                al_o = [c["order"] for c in ch if c["type"] == "payment_allocation"]
                self.assertLess(max(fx_o), min(al_o))
                self.assertTrue(all("step 1/2" in c["cause"] for c in ch
                                    if c["type"] == "fx_conversion"))
                self.assertTrue(all("step 2/2" in c["cause"] for c in ch
                                    if c["type"] == "payment_allocation"))
        self.assertGreaterEqual(kinds["utc_date"], 4, kinds)

    def test_flat_adjacent_quotes_use_another_published_day(self):
        cfg = {"seed": 3112018831, "size": {"invoices": 12, "background_entries": 700},
               "period": {"start_month": "2026-12", "months": 2}, "timezone": "Asia/Almaty",
               "base_currency": "USD", "foreign_currencies": ["KZT"],
               "combos": {"fx_partial_payment_boundary": 2},
               "distractors": {"advances": 8, "split_payments": 1, "repeated_amounts": 1, "utc_boundary": 1},
               "split": "audit"}
        episode = make(cfg)
        self.assertAll(episode, 4)
        self.assertIn("nearby_day", variants(episode).values())

    def test_separate_payment_errors_have_candidates_after_combinations(self):
        cfg = {"seed": 3112017682, "size": {"invoices": 15, "background_entries": 700},
               "period": {"start_month": "2026-01", "months": 2}, "timezone": "Etc/UTC",
               "base_currency": "GBP", "foreign_currencies": ["USD", "EUR"],
               "errors": {"payment_allocation": 1}, "combos": {"fx_partial_payment_boundary": 2},
               "distractors": {"advances": 10, "refunds": 10, "split_payments": 3, "utc_boundary": 6},
               "split": "audit"}
        episode = make(cfg)
        self.assertAll(episode, 5)
        counts = Counter(issue["type"] for issue in episode["private"]["truth"]["issues"])
        self.assertEqual(counts, {"fx_conversion": 2, "payment_allocation": 3})


class DuplicatePlusPeriodTest(InjectCase):
    def test_combo(self):
        equal = 0
        for i, seed in enumerate(SEEDS):
            tz = TIMEZONES[i % len(TIMEZONES)]
            with self.subTest(seed=seed, tz=tz):
                e = make(config_for(seed, tz, combos={"duplicate_plus_period": 1}))
                self.assertAll(e, 2)
                issues = {x["type"]: x for x in e["private"]["truth"]["issues"]}
                self.assertEqual(set(issues), {"duplicate_invoice", "wrong_period"})
                p = Public(e["public"])
                a = p.invoices[issues["duplicate_invoice"]["affected_ids"][0]]
                b = p.invoices[issues["wrong_period"]["affected_ids"][0]]
                self.assertNotEqual(a["doc_id"], b["doc_id"])
                self.assertEqual(a["entity_id"], b["entity_id"])
                equal += a["total_minor"] == b["total_minor"] and a["currency"] == b["currency"]
                # the verifier tells them apart: each finding has its own type and doc
                found = {(f["type"], f["affected_ids"][0]) for f in e["rep"].findings_list}
                self.assertEqual(found, {("duplicate_invoice", a["doc_id"]),
                                         ("wrong_period", b["doc_id"])})
                # A: two postings, both in the period; B: one posting across the boundary
                ea, eb = p.entries(p.invoice_rows(a)), p.entries(p.invoice_rows(b))
                self.assertEqual(len(ea), 2)
                self.assertTrue(all(p.in_period(r[0]["timestamp"]) for r in ea.values()))
                self.assertEqual(len(eb), 1)
                (rows_b,) = eb.values()
                self.assertFalse(p.in_period(rows_b[0]["timestamp"]))
                ch = e["private"]["provenance"]["changes"]
                d_o = [c["order"] for c in ch if c["type"] == "duplicate_invoice"]
                w_o = [c["order"] for c in ch if c["type"] == "wrong_period"]
                self.assertLess(max(d_o), min(w_o))
        self.assertEqual(equal, len(SEEDS))


class MixedConfigTest(InjectCase):
    def test_all_types_and_both_combos(self):
        for i, seed in enumerate(SEEDS[:5]):
            tz = TIMEZONES[i % len(TIMEZONES)]
            cfg = config_for(seed, tz, size={"invoices": 60, "background_entries": 100},
                             errors={"duplicate_invoice": 2, "payment_allocation": 2,
                                     "fx_conversion": 2, "wrong_period": 2},
                             combos={"fx_partial_payment_boundary": 1,
                                     "duplicate_plus_period": 1})
            with self.subTest(seed=seed, tz=tz):
                e = make(cfg)
                self.assertAll(e, 12)
                c = Counter(x["type"] for x in e["private"]["truth"]["issues"])
                self.assertEqual(c, {"duplicate_invoice": 3, "payment_allocation": 3,
                                     "fx_conversion": 3, "wrong_period": 3})
                pairs = Counter((x["type"], x["affected_ids"][0])
                                for x in e["private"]["truth"]["issues"])
                self.assertEqual(max(pairs.values()), 1)
                rows = e["public"]["manifest"]["counts"]["ledger_rows"]
                self.assertTrue(200 <= rows <= 2000)


if __name__ == "__main__":
    unittest.main()
