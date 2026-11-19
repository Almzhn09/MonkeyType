"""A04/A06: baseline history consistency, references, FX coverage and exact
recomputation of every posting from public documents + fx + policy."""

import datetime as dt
import unittest
from collections import defaultdict

from fingen.config import resolve_config
from fingen.history import build_history, invoice_lines, payment_lines
from fingen.money import convert
from fingen.rng import EpisodeRng
from tests.gen_support import CONFIGS, episode, local_date, recompute_problems


class BaselineHistoryTest(unittest.TestCase):
    def test_entries_balance_and_share_timestamp(self):
        for name in CONFIGS:
            pub = episode(name)["public"]
            by_entry = defaultdict(list)
            for r in pub["ledger"]:
                by_entry[r["entry_id"]].append(r)
            for eid, rows in by_entry.items():
                with self.subTest(config=name, entry=eid):
                    self.assertEqual(sum(r["debit_minor"] for r in rows),
                                     sum(r["credit_minor"] for r in rows))
                    self.assertEqual(len({r["timestamp"] for r in rows}), 1)
            for r in pub["ledger"]:
                self.assertTrue((r["debit_minor"] > 0) != (r["credit_minor"] > 0), r)
                self.assertEqual(min(r["debit_minor"], r["credit_minor"]), 0)
                self.assertEqual(r["currency"], pub["manifest"]["base_currency"])
                self.assertTrue(r["timestamp"].endswith(pub["manifest"]["utc_offset"]))

    def test_line_items_sum_to_total(self):
        for name in CONFIGS:
            for inv in episode(name)["public"]["invoices"]:
                with self.subTest(config=name, invoice=inv["doc_id"]):
                    self.assertTrue(inv["line_items"])
                    for li in inv["line_items"]:
                        self.assertEqual(li["amount_minor"], li["quantity"] * li["unit_price_minor"])
                        self.assertGreater(li["amount_minor"], 0)
                    self.assertEqual(sum(li["amount_minor"] for li in inv["line_items"]),
                                     inv["total_minor"])

    def test_all_references_exist(self):
        for name in CONFIGS:
            pub = episode(name)["public"]
            invoices = {i["invoice_id"]: i for i in pub["invoices"]}
            payments = {p["payment_id"] for p in pub["payments"]}
            entities = {d["entity_id"] for d in pub["invoices"] + pub["payments"]}
            self.assertEqual(len(invoices), len(pub["invoices"]))
            self.assertEqual(len(payments), len(pub["payments"]))
            accounts = pub["policy"]["accounts"]
            known_accounts = {accounts["ar"], accounts["revenue"], accounts["bank"],
                              accounts["unapplied"], *accounts["expense"]}
            for r in pub["ledger"]:
                with self.subTest(config=name, row=r["row_id"]):
                    self.assertIn(r["account"], known_accounts)
                    if r["invoice_id"] is not None:
                        self.assertIn(r["invoice_id"], invoices)
                    if r["payment_id"] is not None:
                        self.assertIn(r["payment_id"], payments)
                    if r["entity_id"] is not None:
                        self.assertIn(r["entity_id"], entities)
            for p in pub["payments"]:
                with self.subTest(config=name, payment=p["doc_id"]):
                    self.assertNotEqual(p["amount_minor"], 0)
                    refs_total = sum(ref["amount_minor"] for ref in p["invoice_refs"])
                    self.assertLessEqual(refs_total, abs(p["amount_minor"]))
                    if p["amount_minor"] < 0:
                        self.assertEqual(p["invoice_refs"], [])
                    for ref in p["invoice_refs"]:
                        self.assertIn(ref["invoice_id"], invoices)
                        inv = invoices[ref["invoice_id"]]
                        self.assertEqual(inv["currency"], p["currency"])
                        self.assertEqual(inv["entity_id"], p["entity_id"])
                        self.assertGreater(ref["amount_minor"], 0)
                        self.assertGreater(p["timestamp"] and dt.datetime.fromisoformat(p["timestamp"]),
                                           dt.datetime.fromisoformat(inv["issued_at"]))

    def test_invoices_not_overpaid_by_refs(self):
        for name in CONFIGS:
            pub = episode(name)["public"]
            applied = defaultdict(int)
            for p in pub["payments"]:
                for ref in p["invoice_refs"]:
                    applied[ref["invoice_id"]] += ref["amount_minor"]
            for inv in pub["invoices"]:
                self.assertLessEqual(applied[inv["invoice_id"]], inv["total_minor"])

    def test_fx_table_complete(self):
        for name in CONFIGS:
            pub = episode(name)["public"]
            m = pub["manifest"]
            off = m["utc_offset"]
            start = local_date(m["extract_start"], off)
            end = local_date(m["extract_end"], off)
            foreign = sorted(set(m["currency_exponents"]) - {m["base_currency"]})
            keys = {(r["date"], r["source_currency"]) for r in pub["fx"]}
            self.assertEqual(len(keys), len(pub["fx"]))
            expected = set()
            d = start
            while d < end:
                for cur in foreign:
                    expected.add((d.isoformat(), cur))
                d += dt.timedelta(days=1)
            self.assertEqual(keys, expected, name)
            for r in pub["fx"]:
                self.assertEqual(r["target_currency"], m["base_currency"])
            for doc in pub["invoices"] + pub["payments"]:
                ts = doc.get("issued_at") or doc.get("timestamp")
                if doc["currency"] != m["base_currency"]:
                    self.assertIn((local_date(ts, off).isoformat(), doc["currency"]), keys)

    def test_documents_inside_extract_window(self):
        for name in CONFIGS:
            pub = episode(name)["public"]
            m = pub["manifest"]
            lo = dt.datetime.fromisoformat(m["extract_start"])
            hi = dt.datetime.fromisoformat(m["extract_end"])
            for doc in pub["invoices"] + pub["payments"]:
                ts = dt.datetime.fromisoformat(doc.get("issued_at") or doc.get("timestamp"))
                self.assertTrue(lo <= ts < hi, doc["doc_id"])
            for r in pub["ledger"]:
                self.assertTrue(lo <= dt.datetime.fromisoformat(r["timestamp"]) < hi)

    def test_postings_recompute_exactly(self):
        for name in CONFIGS:
            with self.subTest(config=name):
                self.assertEqual(recompute_problems(episode(name)["public"]), [])

    def test_distractor_shapes_present(self):
        pub = episode("bogota_jpy")["public"]
        payments = pub["payments"]
        self.assertTrue(any(p["amount_minor"] < 0 for p in payments), "refund")
        self.assertTrue(any(p["amount_minor"] > 0 and
                            sum(r["amount_minor"] for r in p["invoice_refs"]) < p["amount_minor"]
                            for p in payments), "advance")
        per_invoice = defaultdict(int)
        for p in payments:
            for r in p["invoice_refs"]:
                per_invoice[r["invoice_id"]] += 1
        self.assertTrue(any(v >= 2 for v in per_invoice.values()), "split payment")
        totals = defaultdict(int)
        for inv in pub["invoices"]:
            totals[(inv["currency"], inv["total_minor"])] += 1
        self.assertTrue(any(v >= 2 for v in totals.values()), "repeated amounts")
        self.assertTrue(any(len(p["invoice_refs"]) >= 2 for p in payments), "multi-invoice payment")

    def test_utc_boundary_documents_posted_on_local_date(self):
        for name in ("almaty", "bogota_jpy", "kolkata_kwd"):
            pub = episode(name)["public"]
            m = pub["manifest"]
            off = m["utc_offset"]
            ps, pe = dt.datetime.fromisoformat(m["period_start"]), dt.datetime.fromisoformat(m["period_end"])
            docs = pub["invoices"] + pub["payments"]
            stamps = [d.get("issued_at") or d.get("timestamp") for d in docs]
            self.assertTrue(any(s.endswith("+00:00") for s in stamps))
            self.assertTrue(any(s.endswith(off) for s in stamps))
            crossing = []
            for s in stamps:
                t = dt.datetime.fromisoformat(s)
                utc_d = t.astimezone(dt.timezone.utc).date()
                loc_d = local_date(s, off)
                in_local = ps <= t < pe
                in_utc_naive = ps.date() <= utc_d < pe.date()
                if utc_d != loc_d and in_local != in_utc_naive:
                    crossing.append(s)
            self.assertTrue(crossing, f"{name}: no document whose UTC date crosses the period")

    def test_internal_posting_helpers_match_ledger(self):
        cfg = resolve_config({"seed": 5150})
        h = build_history(cfg, EpisodeRng(cfg["seed"]))
        for inv in h.invoices.values():
            rows = h.doc_rows(inv.doc_id)
            self.assertEqual(sorted((r.role, r.debit_minor, r.credit_minor) for r in rows),
                             sorted((role, d, c) for role, d, c, _i, _p in invoice_lines(h, inv)))
        for pay in h.payments.values():
            rows = h.doc_rows(pay.doc_id)
            self.assertEqual(sorted((r.role, r.debit_minor, r.credit_minor) for r in rows),
                             sorted((role, d, c) for role, d, c, _i, _p in payment_lines(h, pay)))


class MoneyTest(unittest.TestCase):
    def test_convert_rules(self):
        self.assertEqual(convert(12345, "EUR", "KZT", "514.23", "half_up"), 6348169)
        self.assertEqual(convert(150, "JPY", "KZT", "3.1", "half_up"), 46500)
        self.assertEqual(convert(500, "KZT", "KZT", None, "half_up"), 500)
        # 0.5 ties: half_up away from zero, half_even to even.
        self.assertEqual(convert(1, "USD", "KZT", "0.5", "half_up"), 1)
        self.assertEqual(convert(1, "USD", "KZT", "0.5", "half_even"), 0)
        self.assertEqual(convert(3, "USD", "KZT", "0.5", "half_even"), 2)
        # exponent shift: 1.000 KWD (1000 minor) at 1500.25 JPY/KWD -> 1500 JPY
        self.assertEqual(convert(1000, "KWD", "JPY", "1500.25", "half_up"), 1500)
        with self.assertRaises(ValueError):
            convert(1, "USD", "KZT", "1.1234567", "half_up")
        with self.assertRaises(TypeError):
            convert(1.5, "USD", "KZT", "1", "half_up")


if __name__ == "__main__":
    unittest.main()
