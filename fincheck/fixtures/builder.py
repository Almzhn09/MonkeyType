"""Hand-written fixture episodes for fincheck tests (independent of the generator).

All document amounts and ledger postings below are literal values computed by hand from
CONTRACT §4 (EUR conversions noted next to each). The builder only serialises them, applies
the listed provenance changes and fills manifest counts/sha256.

  python3.11 -m fincheck.fixtures.builder      # (re)writes fincheck/fixtures/a08/
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
from datetime import date, timedelta
from decimal import Decimal

from ..load import LEDGER_COLUMNS, render_ledger

ACCOUNTS = {"ar": "1210", "revenue": "6010", "bank": "1030", "unapplied": "3510",
            "expense": ["7210", "7410"]}
AR, REV, BANK, UNAP = "1210", "6010", "1030", "3510"


def tok(prefix: str, name: str) -> str:
    """Opaque, deterministic id token (fixture ids must not reveal their role)."""
    return prefix + hashlib.sha256(name.encode()).hexdigest()[:7].upper()


def R(name):  # row id
    return tok("R", name)


def J(name):  # entry id
    return tok("J", name)


def _jdump(obj) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False)


def _inv(doc, iid, ent, issued, due, cur, lines):
    items = [{"line_id": f"L{i + 1}", "description": d, "quantity": q,
              "unit_price_minor": u, "amount_minor": q * u}
             for i, (d, q, u) in enumerate(lines)]
    return {"doc_id": doc, "invoice_id": iid, "entity_id": ent, "issued_at": issued,
            "due_at": due, "currency": cur, "total_minor": sum(x["amount_minor"] for x in items),
            "line_items": items}


def _pay(doc, pid, ent, ts, cur, amount, refs):
    return {"doc_id": doc, "payment_id": pid, "entity_id": ent, "timestamp": ts,
            "currency": cur, "amount_minor": amount,
            "invoice_refs": [{"invoice_id": i, "amount_minor": a} for i, a in refs]}


def _row(rid, eid, ts, acct, dr, cr, ent=None, inv=None, pay=None, cur="KZT"):
    return {"row_id": rid, "entry_id": eid, "timestamp": ts, "account": acct,
            "debit_minor": dr, "credit_minor": cr, "currency": cur, "entity_id": ent,
            "invoice_id": inv, "payment_id": pay}


def _fx_rows(start: date, end_excl: date, cur: str, base: str, base_rate: str, overrides):
    out, d, i = [], start, 0
    while d < end_excl:
        r = overrides.get(d.isoformat())
        if r is None:
            r = f"{Decimal(base_rate) + Decimal(i) * Decimal('0.137531'):.6f}"
        out.append({"date": d.isoformat(), "source_currency": cur, "target_currency": base,
                    "rate": r})
        d += timedelta(days=1)
        i += 1
    return out


def _common(episode_id, company):
    return {
        "episode_id": episode_id, "company": company, "base": "KZT",
        "exps": {"EUR": 2, "KZT": 2}, "timezone": "Asia/Almaty", "utc_offset": "+05:00",
        "period_start": "2026-03-01T00:00:00+05:00", "period_end": "2026-04-01T00:00:00+05:00",
        "extract_start": "2026-02-26T00:00:00+05:00",
        "extract_end": "2026-04-04T00:00:00+05:00", "rounding_mode": "half_up",
    }


# --------------------------------------------------------------------------- clean episode

def clean_spec(episode_id="EP-CHK-CLEAN"):
    """Clean company with every distractor of CONTRACT §8: split payments, advance + refund,
    equal-amount invoices, UTC-boundary documents posted on the local date, foreign
    currency with last-ref rounding remainder, an EUR invoice issued late UTC evening."""
    s = _common(episode_id, "Kokterek Freight LLP")
    A, B, C, D = "INV-2603-118", "INV-2603-131", "INV-2603-152", "INV-2604-007"
    E, F, G, H, JJ = "INV-2603-001", "INV-2603-140", "INV-2603-120", "INV-2603-121", \
        "INV-2603-163"
    c1, c2, c3, c4 = "CP-4KQ8", "CP-9ZD1", "CP-2MW7", "CP-7HX3"
    s["invoices"] = [
        _inv("DQ1A7", A, c1, "2026-03-03T10:00:00+05:00", "2026-04-02T10:00:00+05:00", "KZT",
             [("Consulting hours", 20, 600000), ("Site travel", 1, 3000000)]),  # 15 000 000
        _inv("DQ2B4", B, c2, "2026-03-05T11:30:00+05:00", "2026-04-04T11:30:00+05:00", "KZT",
             [("Warehouse slots", 4, 2000000)]),  # 8 000 000
        _inv("DQ3C9", C, c3, "2026-03-12T15:45:00+05:00", "2026-04-11T15:45:00+05:00", "KZT",
             [("Warehouse slots", 4, 2000000)]),  # 8 000 000, same as B (other customer)
        _inv("DQ4D2", D, c4, "2026-03-31T20:30:00+00:00", "2026-05-01T01:30:00+05:00", "KZT",
             [("Pallet handling", 3, 1400000)]),  # local 2026-04-01 01:30 → April
        _inv("DQ5E8", E, c1, "2026-02-28T19:10:00+00:00", "2026-03-31T00:10:00+05:00", "KZT",
             [("Customs filing", 1, 990000)]),  # local 2026-03-01 00:10 → March
        _inv("DQ6F3", F, c2, "2026-03-10T09:00:00+05:00", "2026-04-09T09:00:00+05:00", "EUR",
             [("Air freight", 1, 100000), ("Fuel surcharge", 3, 7819)]),  # 1234.57 EUR
        _inv("DQ7G5", G, c3, "2026-03-08T12:00:00+05:00", "2026-04-07T12:00:00+05:00", "EUR",
             [("Labels", 1, 3333)]),  # 33.33 EUR
        _inv("DQ8H1", H, c3, "2026-03-09T12:00:00+05:00", "2026-04-08T12:00:00+05:00", "EUR",
             [("Labels", 1, 6667)]),  # 66.67 EUR
        _inv("DQ9J6", JJ, c4, "2026-03-17T21:00:00+00:00", "2026-04-18T02:00:00+05:00", "EUR",
             [("Cold storage", 2, 25000)]),  # 500 EUR, local date 2026-03-18
    ]
    s["payments"] = [
        _pay("DP1K2", "PAY-55101", c1, "2026-03-13T14:00:00+05:00", "KZT", 10000000,
             [(A, 10000000)]),  # split 1/2 of A
        _pay("DP2L7", "PAY-55102", c1, "2026-03-27T09:15:00+05:00", "KZT", 5000000,
             [(A, 5000000)]),  # split 2/2 of A
        _pay("DP3M4", "PAY-55103", c2, "2026-03-06T10:00:00+05:00", "KZT", 5000000,
             [(B, 3000000)]),  # advance: 2 000 000 unapplied
        _pay("DP4N8", "PAY-55104", c2, "2026-03-25T16:00:00+05:00", "KZT", -2000000, []),
        _pay("DP5P3", "PAY-55105", c3, "2026-03-31T19:30:00+00:00", "KZT", 8000000,
             [(C, 8000000)]),  # local 2026-04-01 00:30
        _pay("DP6Q9", "PAY-55106", c2, "2026-03-15T10:00:00+05:00", "EUR", 60000,
             [(F, 60000)]),  # split EUR 1/2
        _pay("DP7R5", "PAY-55107", c2, "2026-03-20T10:00:00+05:00", "EUR", 63457,
             [(F, 63457)]),  # split EUR 2/2
        _pay("DP8S2", "PAY-55108", c3, "2026-03-22T10:00:00+05:00", "EUR", 10000,
             [(G, 3333), (H, 6667)]),  # last-ref rounding
    ]
    s["fx_overrides"] = {"EUR": {
        "2026-03-08": "505.555555", "2026-03-09": "506.123457", "2026-03-10": "512.345678",
        "2026-03-15": "510.250000", "2026-03-17": "507.000000", "2026-03-18": "508.400000",
        "2026-03-20": "509.876543", "2026-03-22": "498.500000"}}
    s["fx_base_rate"] = {"EUR": "500.000000"}

    def inv_entry(key, ts, amount, ent, iid):
        return [_row(R(key + "-ar"), J(key), ts, AR, amount, 0, ent, iid),
                _row(R(key + "-rev"), J(key), ts, REV, 0, amount, ent, iid)]

    rows = []
    rows += inv_entry("A", "2026-03-03T10:00:00+05:00", 15000000, c1, A)
    rows += inv_entry("B", "2026-03-05T11:30:00+05:00", 8000000, c2, B)
    rows += inv_entry("C", "2026-03-12T15:45:00+05:00", 8000000, c3, C)
    rows += inv_entry("D", "2026-04-01T01:30:00+05:00", 4200000, c4, D)
    rows += inv_entry("E", "2026-03-01T00:10:00+05:00", 990000, c1, E)
    rows += inv_entry("F", "2026-03-10T09:00:00+05:00", 63252660, c2, F)  # 123457×512.345678
    rows += inv_entry("G", "2026-03-08T12:00:00+05:00", 1685017, c3, G)  # 3333×505.555555
    rows += inv_entry("H", "2026-03-09T12:00:00+05:00", 3374325, c3, H)  # 6667×506.123457
    rows += inv_entry("J", "2026-03-18T02:00:00+05:00", 25420000, c4, JJ)  # 50000×508.4

    def pay_entry(key, ts, pid, ent, bank, ars, unapplied=0):
        out = [_row(R(key + "-bank"), J(key), ts, BANK, bank, 0, ent, None, pid)]
        for i, (iid, a) in enumerate(ars):
            out.append(_row(R(f"{key}-ar{i}"), J(key), ts, AR, 0, a, ent, iid, pid))
        if unapplied:
            out.append(_row(R(key + "-un"), J(key), ts, UNAP, 0, unapplied, ent, None, pid))
        return out

    rows += pay_entry("P1", "2026-03-13T14:00:00+05:00", "PAY-55101", c1, 10000000,
                      [(A, 10000000)])
    rows += pay_entry("P2", "2026-03-27T09:15:00+05:00", "PAY-55102", c1, 5000000,
                      [(A, 5000000)])
    rows += pay_entry("P3", "2026-03-06T10:00:00+05:00", "PAY-55103", c2, 5000000,
                      [(B, 3000000)], unapplied=2000000)
    rows += [_row(R("P4-un"), J("P4"), "2026-03-25T16:00:00+05:00", UNAP, 2000000, 0, c2,
                  None, "PAY-55104"),
             _row(R("P4-bank"), J("P4"), "2026-03-25T16:00:00+05:00", BANK, 0, 2000000, c2,
                  None, "PAY-55104")]
    rows += pay_entry("P5", "2026-04-01T00:30:00+05:00", "PAY-55105", c3, 8000000,
                      [(C, 8000000)])
    rows += pay_entry("P6", "2026-03-15T10:00:00+05:00", "PAY-55106", c2, 30615000,
                      [(F, 30615000)])  # 60000×510.25
    rows += pay_entry("P7", "2026-03-20T10:00:00+05:00", "PAY-55107", c2, 32355236,
                      [(F, 32355236)])  # 63457×509.876543 = 32355235.99…
    # 10000×498.5 = 4985000; ref G 3333×498.5 = 1661500.5 → 1661501;
    # last ref H gets 4985000 − 1661501 = 3323499 (not round(6667×498.5) = 3323500)
    rows += pay_entry("P8", "2026-03-22T10:00:00+05:00", "PAY-55108", c3, 4985000,
                      [(G, 1661501), (H, 3323499)])
    for key, ts, acct, amt in (("X1", "2026-02-27T09:00:00+05:00", "7210", 300000),
                               ("X2", "2026-03-02T09:00:00+05:00", "7210", 25000000),
                               ("X3", "2026-03-16T18:00:00+05:00", "7410", 1250000),
                               ("X4", "2026-04-02T09:00:00+05:00", "7410", 410000)):
        rows += [_row(R(key + "-exp"), J(key), ts, acct, amt, 0),
                 _row(R(key + "-bank"), J(key), ts, BANK, 0, amt)]
    s["ledger"] = rows
    s["changes"] = []
    s["issues"] = []
    s["distractors"] = [
        {"kind": "split_payments", "doc_ids": ["DP1K2", "DP2L7", "DP6Q9", "DP7R5"]},
        {"kind": "advances", "doc_ids": ["DP3M4"]}, {"kind": "refunds", "doc_ids": ["DP4N8"]},
        {"kind": "repeated_amounts", "doc_ids": ["DQ2B4", "DQ3C9"]},
        {"kind": "utc_boundary", "doc_ids": ["DQ4D2", "DQ5E8", "DQ9J6", "DP5P3"]},
        {"kind": "last_ref_rounding", "doc_ids": ["DP8S2"]}]
    return s


def four_types_spec(episode_id="EP-CHK-FOUR"):
    """Clean company + one error of each §5 type (hand-computed amounts)."""
    s = clean_spec(episode_id)
    c3, c4 = "CP-2MW7", "CP-7HX3"
    dup_ts = "2026-03-12T17:05:00+05:00"
    s["changes"] = [
        # duplicate of C (8 000 000 — same amount as B, a legit repeated amount)
        ("ISS-1", "duplicate_invoice", "add_row", None,
         _row(R("C-dup-ar"), J("C-dup"), dup_ts, AR, 8000000, 0, c3, "INV-2603-152")),
        ("ISS-1", "duplicate_invoice", "add_row", None,
         _row(R("C-dup-rev"), J("C-dup"), dup_ts, REV, 0, 8000000, c3, "INV-2603-152")),
        # partial allocation of P8: ref H credited 3 000 000 instead of 3 323 499
        ("ISS-2", "payment_allocation", "modify_row", R("P8-ar1"), {"credit_minor": 3000000}),
        ("ISS-2", "payment_allocation", "add_row", None,
         _row(R("P8-un"), J("P8"), "2026-03-22T10:00:00+05:00", UNAP, 0, 323499, c3, None,
              "PAY-55108")),
        # FX of J taken on the UTC date 2026-03-17 (507.0): 25 350 000 vs 25 420 000
        ("ISS-3", "fx_conversion", "modify_row", R("J-ar"), {"debit_minor": 25350000}),
        ("ISS-3", "fx_conversion", "modify_row", R("J-rev"), {"credit_minor": 25350000}),
        # E posted at the UTC wall-clock time as if local → 2026-02-28 (outside the period)
        ("ISS-4", "wrong_period", "modify_row", R("E-ar"),
         {"timestamp": "2026-02-28T19:10:00+05:00"}),
        ("ISS-4", "wrong_period", "modify_row", R("E-rev"),
         {"timestamp": "2026-02-28T19:10:00+05:00"}),
    ]
    fx = "FX:2026-03-18:EUR:KZT"
    s["issues"] = [
        _issue("ISS-1", "duplicate_invoice", "DQ3C9", 8000000,
               [["DQ3C9", R("C-ar"), R("C-dup-ar")], ["DQ3C9", R("C-rev"), R("C-dup-rev")]]),
        _issue("ISS-2", "payment_allocation", "DP8S2", 323499,
               [["DP8S2", R("P8-un")],
                ["DP8S2", R("P8-bank"), R("P8-ar0"), R("P8-ar1")]]),
        _issue("ISS-3", "fx_conversion", "DQ9J6", 70000,
               [["DQ9J6", R("J-ar"), fx], ["DQ9J6", R("J-rev"), fx]]),
        _issue("ISS-4", "wrong_period", "DQ5E8", 990000,
               [["DQ5E8", R("E-ar")], ["DQ5E8", R("E-rev")]]),
    ]
    return s


def _issue(iid, typ, doc, amount, sets):
    tol = 1 if typ == "fx_conversion" else 0
    return {"issue_id": iid, "type": typ, "affected_ids": [doc], "expected_amount_minor": amount,
            "currency": "KZT", "tolerance_minor": tol, "evidence_sets": sets}


# --------------------------------------------------------------------------- A08 episode

def a08_spec(episode_id="EP-CHK-A08"):
    """A08 control: one duplicated invoice + one partially allocated EUR payment, with an
    equal-amount invoice and an advance as distractors."""
    s = _common(episode_id, "Altyn Dala Trading LLP")
    k1, k2, k3 = "CN-RT5W", "CN-QW2E", "CN-ZX9C"
    s["invoices"] = [
        _inv("DA1K9", "INV-77-041", k1, "2026-03-04T11:00:00+05:00",
             "2026-04-03T11:00:00+05:00", "KZT", [("Grain storage", 5, 500000)]),  # 2 500 000
        _inv("DA2M4", "INV-77-044", k2, "2026-03-02T09:30:00+05:00",
             "2026-04-01T09:30:00+05:00", "EUR", [("Seed lot", 1, 150000)]),  # 1500.00 EUR
        _inv("DA3P7", "INV-77-045", k2, "2026-03-03T09:30:00+05:00",
             "2026-04-02T09:30:00+05:00", "EUR", [("Seed bags", 4, 20000)]),  # 800.00 EUR
        _inv("DA4Q2", "INV-77-050", k3, "2026-03-11T13:00:00+05:00",
             "2026-04-10T13:00:00+05:00", "KZT", [("Grain storage", 2, 1250000)]),  # 2 500 000
        _inv("DA5S8", "INV-77-052", k1, "2026-03-19T10:00:00+05:00",
             "2026-04-18T10:00:00+05:00", "KZT", [("Drying service", 3, 600000)]),  # 1 800 000
    ]
    s["payments"] = [
        _pay("PB1T3", "PAY-3101", k2, "2026-03-18T12:00:00+05:00", "EUR", 230000,
             [("INV-77-044", 150000), ("INV-77-045", 80000)]),
        _pay("PB2V6", "PAY-3102", k3, "2026-03-20T15:00:00+05:00", "KZT", 2500000,
             [("INV-77-050", 2500000)]),
        _pay("PB3W1", "PAY-3103", k1, "2026-03-26T10:00:00+05:00", "KZT", 3000000,
             [("INV-77-052", 1800000)]),  # advance 1 200 000
    ]
    s["fx_overrides"] = {"EUR": {"2026-03-02": "505.120000", "2026-03-03": "506.780000",
                                 "2026-03-18": "509.333333"}}
    s["fx_base_rate"] = {"EUR": "501.000000"}
    t = lambda h: f"2026-03-{h}+05:00"  # noqa: E731
    rows = [
        _row(R("a1-ar"), J("a1"), t("04T11:00:00"), AR, 2500000, 0, k1, "INV-77-041"),
        _row(R("a1-rev"), J("a1"), t("04T11:00:00"), REV, 0, 2500000, k1, "INV-77-041"),
        # 150000 × 505.12 = 75 768 000
        _row(R("a2-ar"), J("a2"), t("02T09:30:00"), AR, 75768000, 0, k2, "INV-77-044"),
        _row(R("a2-rev"), J("a2"), t("02T09:30:00"), REV, 0, 75768000, k2, "INV-77-044"),
        # 80000 × 506.78 = 40 542 400
        _row(R("a3-ar"), J("a3"), t("03T09:30:00"), AR, 40542400, 0, k2, "INV-77-045"),
        _row(R("a3-rev"), J("a3"), t("03T09:30:00"), REV, 0, 40542400, k2, "INV-77-045"),
        _row(R("a4-ar"), J("a4"), t("11T13:00:00"), AR, 2500000, 0, k3, "INV-77-050"),
        _row(R("a4-rev"), J("a4"), t("11T13:00:00"), REV, 0, 2500000, k3, "INV-77-050"),
        _row(R("a5-ar"), J("a5"), t("19T10:00:00"), AR, 1800000, 0, k1, "INV-77-052"),
        _row(R("a5-rev"), J("a5"), t("19T10:00:00"), REV, 0, 1800000, k1, "INV-77-052"),
        # 230000 × 509.333333 = 117 146 666.59 → 117 146 667; ref1 150000 × … → 76 400 000;
        # last ref = 117 146 667 − 76 400 000 = 40 746 667
        _row(R("b1-bank"), J("b1"), t("18T12:00:00"), BANK, 117146667, 0, k2, None,
             "PAY-3101"),
        _row(R("b1-ar0"), J("b1"), t("18T12:00:00"), AR, 0, 76400000, k2, "INV-77-044",
             "PAY-3101"),
        _row(R("b1-ar1"), J("b1"), t("18T12:00:00"), AR, 0, 40746667, k2, "INV-77-045",
             "PAY-3101"),
        _row(R("b2-bank"), J("b2"), t("20T15:00:00"), BANK, 2500000, 0, k3, None, "PAY-3102"),
        _row(R("b2-ar0"), J("b2"), t("20T15:00:00"), AR, 0, 2500000, k3, "INV-77-050",
             "PAY-3102"),
        _row(R("b3-bank"), J("b3"), t("26T10:00:00"), BANK, 3000000, 0, k1, None, "PAY-3103"),
        _row(R("b3-ar0"), J("b3"), t("26T10:00:00"), AR, 0, 1800000, k1, "INV-77-052",
             "PAY-3103"),
        _row(R("b3-un"), J("b3"), t("26T10:00:00"), UNAP, 0, 1200000, k1, None, "PAY-3103"),
    ]
    for key, ts, acct, amt in (("x1", "2026-02-27T09:00:00+05:00", "7210", 800000),
                               ("x2", "2026-03-10T09:00:00+05:00", "7410", 150000),
                               ("x3", "2026-03-31T23:30:00+05:00", "7210", 1200000)):
        rows += [_row(R(key + "-exp"), J(key), ts, acct, amt, 0),
                 _row(R(key + "-bank"), J(key), ts, BANK, 0, amt)]
    s["ledger"] = rows
    s["changes"] = [
        ("ISS-7Q1", "duplicate_invoice", "add_row", None,
         _row(R("a1d-ar"), J("a1d"), t("04T16:20:00"), AR, 2500000, 0, k1, "INV-77-041")),
        ("ISS-7Q1", "duplicate_invoice", "add_row", None,
         _row(R("a1d-rev"), J("a1d"), t("04T16:20:00"), REV, 0, 2500000, k1, "INV-77-041")),
        ("ISS-9M4", "payment_allocation", "delete_row", R("b1-ar1"), None),
        ("ISS-9M4", "payment_allocation", "add_row", None,
         _row(R("b1-un"), J("b1"), t("18T12:00:00"), UNAP, 0, 40746667, k2, None, "PAY-3101")),
    ]
    s["issues"] = [
        _issue("ISS-7Q1", "duplicate_invoice", "DA1K9", 2500000,
               [["DA1K9", R("a1-ar"), R("a1d-ar")], ["DA1K9", R("a1-rev"), R("a1d-rev")]]),
        _issue("ISS-9M4", "payment_allocation", "PB1T3", 40746667,
               [["PB1T3", R("b1-un")], ["PB1T3", R("b1-bank"), R("b1-ar0")]]),
    ]
    s["distractors"] = [{"kind": "repeated_amounts", "doc_ids": ["DA1K9", "DA4Q2"]},
                        {"kind": "advances", "doc_ids": ["PB3W1"]}]
    return s


def a08_control_submission():
    return {"episode_id": "EP-CHK-A08", "findings": [
        {"type": "duplicate_invoice", "affected_ids": ["DA1K9"],
         "claimed_amount_minor": 2500000, "currency": "KZT",
         "evidence_ids": ["DA1K9", R("a1-ar"), R("a1d-ar")],
         "explanation": "Invoice INV-77-041 (2 500 000 KZT) has two AR/revenue postings on "
                        "2026-03-04; the second overstates AR by 2 500 000."},
        {"type": "payment_allocation", "affected_ids": ["PB1T3"],
         "claimed_amount_minor": 40746667, "currency": "KZT",
         "evidence_ids": ["PB1T3", R("b1-un")],
         "explanation": "PAY-3101 refs cover the full 2300.00 EUR, but the 40 746 667 KZT "
                        "for INV-77-045 was credited to unapplied instead of AR."},
    ]}


# --------------------------------------------------------------------------- writer

def apply_changes(rows, changes):
    """Apply (issue_id, type, op, target, payload) in order; return rows and provenance
    change records with full before/after rows."""
    cur = {r["row_id"]: copy.deepcopy(r) for r in rows}
    out = []
    for order, (iid, typ, op, target, payload) in enumerate(changes, start=1):
        if op == "add_row":
            before, after = None, copy.deepcopy(payload)
            cur[after["row_id"]] = after
            target = after["row_id"]
        elif op == "modify_row":
            before = copy.deepcopy(cur[target])
            after = {**before, **payload}
            cur[target] = after
        elif op == "delete_row":
            before, after = cur.pop(target), None
        else:
            raise ValueError(op)
        out.append({"issue_id": iid, "type": typ, "order": order, "op": op, "target": target,
                    "before": before, "after": after,
                    "cause": f"fixture change {order} for {iid}"})
    return list(cur.values()), out


def write_episode(spec, public_dir, private_dir):
    os.makedirs(public_dir, exist_ok=True)
    os.makedirs(private_dir, exist_ok=True)
    base = spec["base"]
    tz_off = spec["utc_offset"]
    start = date.fromisoformat(spec["extract_start"][:10])
    end = date.fromisoformat(spec["extract_end"][:10])
    fx_rows = []
    for cur in sorted(set(spec["exps"]) - {base}):
        fx_rows += _fx_rows(start, end, cur, base, spec["fx_base_rate"][cur],
                            spec["fx_overrides"].get(cur, {}))
    baseline = spec["ledger"]
    ledger, changes = apply_changes(baseline, spec["changes"])
    files = {}
    period = {"start": spec["period_start"], "end": spec["period_end"], "end_exclusive": True,
              "timezone": spec["timezone"], "utc_offset": tz_off}
    rounding = {"mode": spec["rounding_mode"], "scope": "per_conversion"}
    policy = {"schema_version": "1.0", "episode_id": spec["episode_id"], "base_currency": base,
              "period": period,
              "fx": {"date_rule": "event_local_date", "target_currency": base,
                     "rate_direction": "target_per_source"},
              "rounding": rounding,
              "recognition": {"invoice": "issued_at_local_date",
                              "payment": "timestamp_local_date"},
              "allocation": {"rule": "per_invoice_refs", "rounding_remainder": "last_ref",
                             "unapplied_account_role": "unapplied"},
              "accounts": ACCOUNTS}
    files["policy.json"] = (json.dumps(policy, sort_keys=True, indent=2, ensure_ascii=False)
                            + "\n").encode()
    files["ledger.csv"] = render_ledger(ledger)
    inv = sorted(spec["invoices"], key=lambda d: d["doc_id"])
    pay = sorted(spec["payments"], key=lambda d: d["doc_id"])
    files["invoices.jsonl"] = "".join(_jdump(d) + "\n" for d in inv).encode()
    files["payments.jsonl"] = "".join(_jdump(d) + "\n" for d in pay).encode()
    fx_text = "date,source_currency,target_currency,rate\n" + "".join(
        f"{r['date']},{r['source_currency']},{r['target_currency']},{r['rate']}\n"
        for r in fx_rows)
    files["fx.csv"] = fx_text.encode()
    sha = {k: hashlib.sha256(v).hexdigest() for k, v in files.items()}
    manifest = {"schema_version": "1.0", "episode_id": spec["episode_id"],
                "company": {"name": spec["company"]}, "base_currency": base,
                "currency_exponents": spec["exps"], "timezone": spec["timezone"],
                "utc_offset": tz_off, "period_start": spec["period_start"],
                "period_end": spec["period_end"], "extract_start": spec["extract_start"],
                "extract_end": spec["extract_end"],
                "counts": {"ledger_rows": len(ledger),
                           "entries": len({r["entry_id"] for r in ledger}),
                           "invoices": len(inv), "payments": len(pay),
                           "documents": len(inv) + len(pay), "fx_rates": len(fx_rows)},
                "rounding": rounding, "files": sha}
    files["manifest.json"] = (json.dumps(manifest, sort_keys=True, indent=2,
                                         ensure_ascii=False) + "\n").encode()
    for name, data in files.items():
        with open(os.path.join(public_dir, name), "wb") as fh:
            fh.write(data)
    truth = {"schema_version": "1.0", "episode_id": spec["episode_id"], "base_currency": base,
             "issues": sorted(spec["issues"], key=lambda i: i["issue_id"])}
    config = {"fixture": spec["episode_id"]}
    baseline_sha = {k: sha[k] for k in sha}
    baseline_sha["ledger.csv"] = hashlib.sha256(render_ledger(baseline)).hexdigest()
    prov = {"schema_version": "1.0", "episode_id": spec["episode_id"],
            "generator_version": "fincheck-fixture-1", "config": config,
            "config_sha256": hashlib.sha256(json.dumps(config, sort_keys=True,
                                                       separators=(",", ":")).encode()
                                            ).hexdigest(),
            "seed": 0, "baseline_sha256": baseline_sha, "changes": changes,
            "distractors": spec["distractors"]}
    for name, obj in (("truth.json", truth), ("provenance.json", prov)):
        with open(os.path.join(private_dir, name), "w", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(obj, sort_keys=True, indent=2, ensure_ascii=False) + "\n")
    assert set(LEDGER_COLUMNS) == set(ledger[0])
    return public_dir, private_dir


def rehash_manifest(public_dir):
    """After hand-editing public files in a test: refresh manifest sha256 and counts."""
    import csv
    mp = os.path.join(public_dir, "manifest.json")
    with open(mp, encoding="utf-8") as fh:
        m = json.load(fh)
    for name in m["files"]:
        with open(os.path.join(public_dir, name), "rb") as fh:
            m["files"][name] = hashlib.sha256(fh.read()).hexdigest()

    def lines(name):
        with open(os.path.join(public_dir, name), encoding="utf-8") as fh:
            return [x for x in fh.read().split("\n") if x]

    with open(os.path.join(public_dir, "ledger.csv"), encoding="utf-8", newline="") as fh:
        led = list(csv.DictReader(fh))
    c = m["counts"]
    c["ledger_rows"], c["entries"] = len(led), len({r.get("entry_id") for r in led})
    c["invoices"], c["payments"] = len(lines("invoices.jsonl")), len(lines("payments.jsonl"))
    c["documents"], c["fx_rates"] = c["invoices"] + c["payments"], len(lines("fx.csv")) - 1
    with open(mp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(m, sort_keys=True, indent=2, ensure_ascii=False) + "\n")


def build(root, spec, split="train"):
    """Write spec under root/{public,private}/<split>/<episode_id>."""
    ep = spec["episode_id"]
    return write_episode(spec, os.path.join(root, "public", split, ep),
                         os.path.join(root, "private", split, ep))


def write_a08(dest=None):
    dest = dest or os.path.join(os.path.dirname(os.path.abspath(__file__)), "a08")
    write_episode(a08_spec(), os.path.join(dest, "public"), os.path.join(dest, "private"))
    with open(os.path.join(dest, "control_submission.json"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(a08_control_submission(), sort_keys=True, indent=2,
                            ensure_ascii=False) + "\n")
    return dest


if __name__ == "__main__":
    print(write_a08())
