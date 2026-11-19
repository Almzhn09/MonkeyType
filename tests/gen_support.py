"""Shared helpers for the fingen tests (not a test module itself)."""

from __future__ import annotations

import atexit
import datetime as dt
import os
import shutil
import tempfile
from collections import defaultdict
from decimal import ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal

from fingen.generate import generate_episode
from fingen.reader import load_private, load_public

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXAMPLE = os.path.join(REPO, "configs", "examples", "clean_small.json")

_TMP = tempfile.mkdtemp(prefix="fingen-tests-")
atexit.register(shutil.rmtree, _TMP, True)
_CACHE: dict[str, dict] = {}

# A spread of configurations exercised by most tests.
CONFIGS: dict[str, dict] = {
    "almaty": {"seed": 1001, "timezone": "Asia/Almaty", "base_currency": "KZT",
               "foreign_currencies": ["EUR", "USD"], "rounding_mode": "half_up"},
    "bogota_jpy": {"seed": 1002, "timezone": "America/Bogota", "base_currency": "USD",
                   "foreign_currencies": ["JPY", "KWD"], "rounding_mode": "half_even",
                   "distractors": {"split_payments": 3, "refunds": 2, "repeated_amounts": 2,
                                   "utc_boundary": 4, "advances": 3}},
    "kolkata_kwd": {"seed": 1003, "timezone": "Asia/Kolkata", "base_currency": "KWD",
                    "foreign_currencies": ["KZT", "JPY", "GBP"], "period": {"start_month": "2026-12", "months": 2}},
    "utc_base_only": {"seed": 1004, "timezone": "Etc/UTC", "base_currency": "EUR",
                      "foreign_currencies": []},
    "small": {"seed": 1005, "size": {"invoices": 12, "background_entries": 0}},
    "large": {"seed": 1006, "size": {"invoices": 95, "background_entries": 400},
              "distractors": {"split_payments": 2, "refunds": 1, "repeated_amounts": 2,
                              "utc_boundary": 2, "advances": 1}},
    "tokyo_seeded": {"seed": 77},
}


def episode(name: str) -> dict:
    """Generate (once) and load the episode for CONFIGS[name]."""
    if name not in _CACHE:
        ep, pub, priv = generate_episode(CONFIGS[name], os.path.join(_TMP, name))
        _CACHE[name] = {"ep": ep, "pub": pub, "priv": priv, "public": load_public(pub),
                        "private": load_private(priv)}
    return _CACHE[name]


def tmpdir() -> str:
    return tempfile.mkdtemp(dir=_TMP)


def read_tree(root: str) -> dict[str, bytes]:
    out = {}
    for base, _dirs, files in os.walk(root):
        for f in files:
            p = os.path.join(base, f)
            with open(p, "rb") as fh:
                out[os.path.relpath(p, root)] = fh.read()
    return out


def tz_of(offset: str) -> dt.timezone:
    sign = -1 if offset[0] == "-" else 1
    return dt.timezone(sign * dt.timedelta(hours=int(offset[1:3]), minutes=int(offset[4:6])))


def local_date(ts: str, offset: str) -> dt.date:
    return dt.datetime.fromisoformat(ts).astimezone(tz_of(offset)).date()


def convert(amount: int, src: str, base: str, rate: str | None, mode: str, exps: dict) -> int:
    if src == base:
        return amount
    value = Decimal(amount) * Decimal(rate) * (Decimal(10) ** (exps[base] - exps[src]))
    rounding = {"half_up": ROUND_HALF_UP, "half_even": ROUND_HALF_EVEN}[mode]
    return int(value.quantize(Decimal(1), rounding=rounding))


def recompute_problems(pub: dict) -> list[str]:
    """Independently re-derive every posting from documents + fx + policy and
    return a list of mismatches (empty for a clean baseline)."""
    m, pol = pub["manifest"], pub["policy"]
    base, off, mode = m["base_currency"], m["utc_offset"], pol["rounding"]["mode"]
    exps = m["currency_exponents"]
    acc = pol["accounts"]
    fx = {(r["date"], r["source_currency"]): r["rate"] for r in pub["fx"]}
    entries = defaultdict(list)
    for r in pub["ledger"]:
        entries[r["entry_id"]].append(r)
    problems: list[str] = []
    seen: set[str] = set()

    def conv(amount, cur, ts):
        rate = None if cur == base else fx.get((local_date(ts, off).isoformat(), cur))
        if cur != base and rate is None:
            problems.append(f"missing fx {cur} {ts}")
            return 0
        return convert(amount, cur, base, rate, mode, exps)

    def norm(rows):
        return sorted((r["account"], r["debit_minor"], r["credit_minor"], r["invoice_id"] or "",
                       r["payment_id"] or "", r["entity_id"] or "") for r in rows)

    def check_entry(doc_id, ts, rows, expected):
        ids = {r["entry_id"] for r in rows}
        if len(ids) != 1:
            problems.append(f"{doc_id}: expected one entry, got {len(ids)}")
            return
        seen.update(ids)
        if any(dt.datetime.fromisoformat(r["timestamp"]) != dt.datetime.fromisoformat(ts)
               for r in rows):
            problems.append(f"{doc_id}: entry timestamp differs from document instant")
        if norm(rows) != sorted(expected):
            problems.append(f"{doc_id}: postings {norm(rows)} != expected {sorted(expected)}")

    for inv in pub["invoices"]:
        rows = [r for r in pub["ledger"] if r["invoice_id"] == inv["invoice_id"]
                and r["payment_id"] is None]
        b = conv(inv["total_minor"], inv["currency"], inv["issued_at"])
        e, iid = inv["entity_id"], inv["invoice_id"]
        check_entry(inv["doc_id"], inv["issued_at"], rows,
                    [(acc["ar"], b, 0, iid, "", e), (acc["revenue"], 0, b, iid, "", e)])
    for p in pub["payments"]:
        rows = [r for r in pub["ledger"] if r["payment_id"] == p["payment_id"]]
        pid, e, ts, cur = p["payment_id"], p["entity_id"], p["timestamp"], p["currency"]
        total = conv(abs(p["amount_minor"]), cur, ts)
        if p["amount_minor"] < 0:
            exp = [(acc["unapplied"], total, 0, "", pid, e), (acc["bank"], 0, total, "", pid, e)]
        else:
            exp = [(acc["bank"], total, 0, "", pid, e)]
            refs = p["invoice_refs"]
            s = sum(r["amount_minor"] for r in refs)
            running = 0
            for i, ref in enumerate(refs):
                if s == p["amount_minor"] and i == len(refs) - 1:
                    b = total - running
                else:
                    b = conv(ref["amount_minor"], cur, ts)
                running += b
                exp.append((acc["ar"], 0, b, ref["invoice_id"], pid, e))
            if s < p["amount_minor"]:
                exp.append((acc["unapplied"], 0, total - running, "", pid, e))
        check_entry(p["doc_id"], ts, rows, exp)
    for eid, rows in entries.items():
        if eid in seen:
            continue
        for r in rows:
            if r["invoice_id"] or r["payment_id"] or r["entity_id"]:
                problems.append(f"{eid}: background row with document refs")
            if r["debit_minor"] and r["account"] not in acc["expense"]:
                problems.append(f"{eid}: background debit not on an expense account")
            if r["credit_minor"] and r["account"] != acc["bank"]:
                problems.append(f"{eid}: background credit not on bank")
    return problems
