"""Strict loader for episode files (CONTRACT v1.0 §2, §3, §6).

``load_public(dir)`` returns a :class:`Public` or raises :class:`SchemaError` carrying every
problem found (file-qualified, row-qualified messages). ``load_private(dir)`` does the same
for truth/provenance. Money is ``int`` minor units, FX rates are ``Decimal``; floats are
rejected everywhere.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from functools import lru_cache

SCHEMA_VERSION = "1.0"

PUBLIC_FILES = ("manifest.json", "policy.json", "ledger.csv", "invoices.jsonl",
                "payments.jsonl", "fx.csv")
HASHED_FILES = ("policy.json", "ledger.csv", "invoices.jsonl", "payments.jsonl", "fx.csv")
LEDGER_COLUMNS = ["row_id", "entry_id", "timestamp", "account", "debit_minor",
                  "credit_minor", "currency", "entity_id", "invoice_id", "payment_id"]
FX_COLUMNS = ["date", "source_currency", "target_currency", "rate"]
ISSUE_TYPES = ("duplicate_invoice", "payment_allocation", "fx_conversion", "wrong_period")
TOLERANCE = {"duplicate_invoice": 0, "payment_allocation": 0, "fx_conversion": 1,
             "wrong_period": 0}
TZ_OFFSETS = {"Asia/Almaty": "+05:00", "Asia/Dubai": "+04:00", "Asia/Kolkata": "+05:30",
              "Asia/Tokyo": "+09:00", "America/Bogota": "-05:00", "Etc/UTC": "+00:00"}
KNOWN_EXPONENTS = {"KZT": 2, "USD": 2, "EUR": 2, "GBP": 2, "CNY": 2, "JPY": 0, "KWD": 3}
ROLES = ("ar", "revenue", "bank", "unapplied")

MANIFEST_KEYS = {"schema_version", "episode_id", "company", "base_currency",
                 "currency_exponents", "timezone", "utc_offset", "period_start", "period_end",
                 "extract_start", "extract_end", "counts", "rounding", "files"}
COUNT_KEYS = {"ledger_rows", "entries", "invoices", "payments", "documents", "fx_rates"}
POLICY_KEYS = {"schema_version", "episode_id", "base_currency", "period", "fx", "rounding",
               "recognition", "allocation", "accounts"}
INVOICE_KEYS = {"doc_id", "invoice_id", "entity_id", "issued_at", "due_at", "currency",
                "total_minor", "line_items"}
LINE_KEYS = {"line_id", "description", "quantity", "unit_price_minor", "amount_minor"}
PAYMENT_KEYS = {"doc_id", "payment_id", "entity_id", "timestamp", "currency", "amount_minor",
                "invoice_refs"}
PAYMENT_REQUIRED = PAYMENT_KEYS - {"entity_id"}  # entity_id is "дополнительное поле"
REF_KEYS = {"invoice_id", "amount_minor"}
TRUTH_KEYS = {"schema_version", "episode_id", "base_currency", "issues"}
ISSUE_KEYS = {"issue_id", "type", "affected_ids", "expected_amount_minor", "currency",
              "tolerance_minor", "evidence_sets"}
PROVENANCE_KEYS = {"schema_version", "episode_id", "generator_version", "config",
                   "config_sha256", "seed", "baseline_sha256", "changes", "distractors"}
CHANGE_KEYS = {"issue_id", "type", "order", "op", "target", "before", "after", "cause"}
OPS = ("add_row", "modify_row", "delete_row")

_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?[+-]\d{2}:\d{2}$")
_OFFSET_RE = re.compile(r"^([+-])(\d{2}):(\d{2})$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_RATE_RE = re.compile(r"^(0|[1-9]\d*)(\.\d{1,6})?$")
_INT_RE = re.compile(r"^(0|[1-9]\d*)$")
_CUR_RE = re.compile(r"^[A-Z]{3}$")
_ID_RE = re.compile(r"^[!-~]+$")  # printable ASCII, no whitespace
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
FX_KEY_RE = re.compile(r"^FX:(\d{4}-\d{2}-\d{2}):([A-Z]{3}):([A-Z]{3})$")


class SchemaError(Exception):
    def __init__(self, errors):
        self.errors = list(errors)
        super().__init__("; ".join(self.errors[:20]) + (
            f" (+{len(self.errors) - 20} more)" if len(self.errors) > 20 else ""))


# --------------------------------------------------------------------------- primitives

def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def is_id(v) -> bool:
    return isinstance(v, str) and bool(_ID_RE.match(v)) and v == v.upper()


@lru_cache(maxsize=None)
def parse_offset(s: str) -> timedelta:
    m = _OFFSET_RE.match(s or "")
    if not m:
        raise ValueError(f"bad utc offset {s!r}")
    sign = -1 if m.group(1) == "-" else 1
    return sign * timedelta(hours=int(m.group(2)), minutes=int(m.group(3)))


@lru_cache(maxsize=None)
def parse_ts(s: str) -> datetime:
    """ISO-8601 with explicit ±HH:MM offset (no 'Z', no naive)."""
    if not isinstance(s, str) or not _TS_RE.match(s):
        raise ValueError(f"not ISO-8601 with ±HH:MM offset: {s!r}")
    return datetime.fromisoformat(s)


def local_date(ts: str, offset: timedelta) -> date:
    return parse_ts(ts).astimezone(timezone(offset)).date()


def fx_key(d, src: str, tgt: str) -> str:
    return f"FX:{d.isoformat() if isinstance(d, date) else d}:{src}:{tgt}"


def canon_ids(ids) -> list:
    """CONTRACT §5: strip, upper, dedupe, sort."""
    return sorted({str(i).strip().upper() for i in ids})


def _strict_json(text: str, where: str, errors: list, warnings: list | None = None):
    """Strict JSON (no floats/NaN, no duplicate keys). Key order is not semantic
    (CONTRACT §2): unsorted keys are only a warning, once per file."""
    def hook(pairs):
        keys = [k for k, _ in pairs]
        if len(set(keys)) != len(keys):
            errors.append(f"{where}: duplicate JSON key in object {keys}")
        if keys != sorted(keys) and warnings is not None:
            msg = f"{where.split(':')[0]}: JSON keys not in sorted order (accepted)"
            if msg not in warnings:
                warnings.append(msg)
        return dict(pairs)

    def bad_const(c):
        raise ValueError(f"non-finite JSON constant {c}")

    def bad_float(s):
        raise ValueError(f"float {s} not allowed (use int minor units / decimal strings)")

    return json.loads(text, object_pairs_hook=hook, parse_constant=bad_const,
                      parse_float=bad_float)


def _read_text(path: str, errors: list):
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError as exc:
        errors.append(f"{os.path.basename(path)}: cannot read ({exc.__class__.__name__})")
        return None, None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        errors.append(f"{os.path.basename(path)}: not UTF-8")
        return raw, None
    if "\r" in text:
        errors.append(f"{os.path.basename(path)}: contains CR; line endings must be \\n")
    if text.startswith("﻿"):
        errors.append(f"{os.path.basename(path)}: has BOM")
    return raw, text


def _keys(obj, required, allowed, where, errors) -> bool:
    if not isinstance(obj, dict):
        errors.append(f"{where}: expected object, got {type(obj).__name__}")
        return False
    missing = sorted(set(required) - set(obj))
    extra = sorted(set(obj) - set(allowed))
    if missing:
        errors.append(f"{where}: missing field(s) {missing}")
    if extra:
        errors.append(f"{where}: unexpected field(s) {extra}")
    return not missing


def _ts_field(v, where, errors, offset=None):
    try:
        dt = parse_ts(v)
    except (ValueError, TypeError) as exc:
        errors.append(f"{where}: {exc}")
        return None
    if offset is not None and dt.utcoffset() != offset:
        errors.append(f"{where}: timestamp {v} must use the company offset")
    return dt


def _id_field(v, where, errors, nullable=False):
    if v is None:
        if not nullable:
            errors.append(f"{where}: required id is null")
        return
    if not is_id(v):
        errors.append(f"{where}: invalid id {v!r} (uppercase, no whitespace)")


# --------------------------------------------------------------------------- ledger rows

def normalize_row(raw: dict, where: str, errors: list, base: str | None = None,
                  offset: timedelta | None = None, accounts: set | None = None) -> dict | None:
    """Normalize a ledger row from CSV (strings) or provenance (JSON values)."""
    if not _keys(raw, LEDGER_COLUMNS, LEDGER_COLUMNS, where, errors):
        return None
    row = {}
    for col in LEDGER_COLUMNS:
        v = raw[col]
        if v == "":
            v = None
        row[col] = v
    for col in ("debit_minor", "credit_minor"):
        v = row[col]
        if isinstance(v, str) and _INT_RE.match(v):
            row[col] = int(v)
        elif is_int(v) and v >= 0:
            pass
        else:
            errors.append(f"{where}: {col} must be a non-negative integer, got {v!r}")
            return None
    if (row["debit_minor"] > 0) == (row["credit_minor"] > 0):
        errors.append(f"{where}: exactly one of debit_minor/credit_minor must be > 0 "
                      f"(got {row['debit_minor']}/{row['credit_minor']})")
    for col in ("row_id", "entry_id"):
        _id_field(row[col], f"{where}.{col}", errors)
    for col in ("entity_id", "invoice_id", "payment_id"):
        _id_field(row[col], f"{where}.{col}", errors, nullable=True)
    _ts_field(row["timestamp"], f"{where}.timestamp", errors, offset)
    if not isinstance(row["account"], str) or not row["account"]:
        errors.append(f"{where}: account must be a non-empty string")
    elif accounts is not None and row["account"] not in accounts:
        errors.append(f"{where}: account {row['account']!r} not declared in policy.accounts")
    if base is not None and row["currency"] != base:
        errors.append(f"{where}: currency {row['currency']!r} must be base currency {base}")
    return row


def row_sort_key(r):
    return (parse_ts(r["timestamp"]), r["row_id"])


def render_ledger(rows) -> bytes:
    """Canonical rendering used to try to reproduce baseline_sha256 (csv, \\n, sorted)."""
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(LEDGER_COLUMNS)
    for r in sorted(rows, key=row_sort_key):
        w.writerow(["" if r[c] is None else str(r[c]) for c in LEDGER_COLUMNS])
    return buf.getvalue().encode("utf-8")


def check_ledger_integrity(rows, invoice_ids, payment_ids, label="ledger.csv") -> list:
    """Accounting equalities that must hold for any ledger (baseline or published)."""
    errors = []
    seen = set()
    entries = {}
    for r in rows:
        if r["row_id"] in seen:
            errors.append(f"{label}: duplicate row_id {r['row_id']}")
        seen.add(r["row_id"])
        entries.setdefault(r["entry_id"], []).append(r)
        if r["invoice_id"] is not None and r["invoice_id"] not in invoice_ids:
            errors.append(f"{label}: row {r['row_id']} references unknown invoice_id "
                          f"{r['invoice_id']}")
        if r["payment_id"] is not None and r["payment_id"] not in payment_ids:
            errors.append(f"{label}: row {r['row_id']} references unknown payment_id "
                          f"{r['payment_id']}")
    for eid, ers in entries.items():
        dr = sum(r["debit_minor"] for r in ers)
        cr = sum(r["credit_minor"] for r in ers)
        if dr != cr:
            errors.append(f"{label}: entry {eid} unbalanced (debit {dr} != credit {cr})")
        if len({parse_ts(r["timestamp"]) for r in ers}) != 1:
            errors.append(f"{label}: entry {eid} rows have different timestamps")
        if len(ers) < 2:
            errors.append(f"{label}: entry {eid} has a single row")
    return errors


# --------------------------------------------------------------------------- Public

@dataclass
class Public:
    manifest: dict
    policy: dict
    ledger: list
    invoices: list
    payments: list
    fx_rows: list
    raw: dict = field(default_factory=dict)  # file name -> bytes
    root: str | None = None
    warnings: list = field(default_factory=list)  # non-fatal findings (e.g. row order)

    def __post_init__(self):
        m = self.manifest
        self.base = m["base_currency"]
        self.exps = m["currency_exponents"]
        self.offset = parse_offset(m["utc_offset"])
        self.tz = timezone(self.offset)
        self.period_start = parse_ts(m["period_start"])
        self.period_end = parse_ts(m["period_end"])
        self.rounding_mode = m["rounding"]["mode"]
        acc = self.policy["accounts"]
        self.role_of = {}
        for role in ROLES:
            self.role_of[acc[role]] = role
        for code in acc["expense"]:
            self.role_of[code] = "expense"
        self.account_of = {role: acc[role] for role in ROLES}
        self.fx = {}
        for r in self.fx_rows:
            self.fx[(r["date"], r["source_currency"])] = Decimal(r["rate"])
        self.fx_keys = {fx_key(r["date"], r["source_currency"], r["target_currency"])
                        for r in self.fx_rows}
        self.invoices_by_doc = {d["doc_id"]: d for d in self.invoices}
        self.payments_by_doc = {d["doc_id"]: d for d in self.payments}
        self.docs = {**self.invoices_by_doc, **self.payments_by_doc}
        self.invoice_by_iid = {d["invoice_id"]: d for d in self.invoices}
        self.payment_by_pid = {d["payment_id"]: d for d in self.payments}
        self._index_ledger()

    def _index_ledger(self):
        self.rows_by_id = {r["row_id"]: r for r in self.ledger}
        self.entries = {}
        for r in self.ledger:
            self.entries.setdefault(r["entry_id"], []).append(r)

    def with_ledger(self, rows) -> "Public":
        return Public(self.manifest, self.policy, sorted(rows, key=row_sort_key),
                      self.invoices, self.payments, self.fx_rows, {}, self.root)

    def role(self, row) -> str | None:
        return self.role_of.get(row["account"])

    def local_date(self, ts: str) -> date:
        return local_date(ts, self.offset)

    def in_period(self, ts: str) -> bool:
        return self.period_start <= parse_ts(ts) < self.period_end

    def id_space(self) -> set:
        """Every ID an agent can cite: rows, entries, docs, business numbers, entities, FX."""
        s = set(self.fx_keys)
        for r in self.ledger:
            for c in ("row_id", "entry_id", "entity_id", "invoice_id", "payment_id"):
                if r[c] is not None:
                    s.add(r[c])
        for d in self.invoices:
            s.update(x for x in (d["doc_id"], d["invoice_id"], d.get("entity_id")) if x)
        for d in self.payments:
            s.update(x for x in (d["doc_id"], d["payment_id"], d.get("entity_id")) if x)
        return s


def _validate_manifest(m, errors):
    if not _keys(m, MANIFEST_KEYS, MANIFEST_KEYS, "manifest.json", errors):
        return False
    ok = True
    if m["schema_version"] != SCHEMA_VERSION:
        errors.append(f"manifest.json: schema_version {m['schema_version']!r} != "
                      f"{SCHEMA_VERSION!r}")
    _id_field(m["episode_id"], "manifest.json.episode_id", errors)
    if not (isinstance(m["company"], dict) and set(m["company"]) == {"name"}
            and isinstance(m["company"]["name"], str) and m["company"]["name"]):
        errors.append("manifest.json: company must be {\"name\": <non-empty string>}")
    ce = m["currency_exponents"]
    if not isinstance(ce, dict) or not ce:
        errors.append("manifest.json: currency_exponents must be a non-empty object")
        ok = False
    else:
        for cur, e in ce.items():
            if not _CUR_RE.match(cur) or not is_int(e) or not 0 <= e <= 6:
                errors.append(f"manifest.json: bad currency exponent {cur}={e!r}")
                ok = False
            elif cur in KNOWN_EXPONENTS and KNOWN_EXPONENTS[cur] != e:
                errors.append(f"manifest.json: exponent of {cur} is {e}, contract says "
                              f"{KNOWN_EXPONENTS[cur]}")
        if m["base_currency"] not in ce:
            errors.append("manifest.json: base_currency missing from currency_exponents")
            ok = False
    if TZ_OFFSETS.get(m["timezone"]) != m["utc_offset"]:
        errors.append(f"manifest.json: timezone/utc_offset {m['timezone']!r}/"
                      f"{m['utc_offset']!r} not in the contract's fixed-offset list")
    try:
        off = parse_offset(m["utc_offset"])
    except ValueError as exc:
        errors.append(f"manifest.json: {exc}")
        return False
    times = {}
    for k in ("extract_start", "period_start", "period_end", "extract_end"):
        dt = _ts_field(m[k], f"manifest.json.{k}", errors, off)
        if dt is None:
            ok = False
            continue
        times[k] = dt
        if dt.astimezone(timezone(off)).time() != datetime.min.time():
            errors.append(f"manifest.json: {k} must be local midnight")
    if len(times) == 4 and not (times["extract_start"] <= times["period_start"]
                                < times["period_end"] <= times["extract_end"]):
        errors.append("manifest.json: need extract_start <= period_start < period_end "
                      "<= extract_end")
    c = m["counts"]
    if not (isinstance(c, dict) and set(c) == COUNT_KEYS and all(is_int(v) and v >= 0
                                                                 for v in c.values())):
        errors.append(f"manifest.json: counts must have exactly {sorted(COUNT_KEYS)} as ints")
        ok = False
    r = m["rounding"]
    if not (isinstance(r, dict) and set(r) == {"mode", "scope"}
            and r.get("mode") in ("half_up", "half_even") and r.get("scope") == "per_conversion"):
        errors.append(f"manifest.json: bad rounding {r!r}")
        ok = False
    f = m["files"]
    if not isinstance(f, dict) or set(f) != set(HASHED_FILES):
        errors.append(f"manifest.json: files must list exactly {list(HASHED_FILES)}")
        ok = False
    elif not all(isinstance(v, str) and _HEX64.match(v) for v in f.values()):
        errors.append("manifest.json: files values must be lowercase sha256 hex")
    return ok


def _validate_policy(p, m, errors):
    if not _keys(p, POLICY_KEYS, POLICY_KEYS, "policy.json", errors):
        return False
    ok = True
    for k in ("schema_version", "episode_id", "base_currency"):
        if p[k] != m[k]:
            errors.append(f"policy.json: {k} {p[k]!r} != manifest {m[k]!r}")
    want_period = {"start": m["period_start"], "end": m["period_end"], "end_exclusive": True,
                   "timezone": m["timezone"], "utc_offset": m["utc_offset"]}
    if p["period"] != want_period:
        errors.append(f"policy.json: period {p['period']!r} inconsistent with manifest "
                      f"(expected {want_period!r})")
    want_fx = {"date_rule": "event_local_date", "target_currency": m["base_currency"],
               "rate_direction": "target_per_source"}
    if p["fx"] != want_fx:
        errors.append(f"policy.json: fx {p['fx']!r} != {want_fx!r}")
    if p["rounding"] != m["rounding"]:
        errors.append("policy.json: rounding differs from manifest")
    want_rec = {"invoice": "issued_at_local_date", "payment": "timestamp_local_date"}
    if p["recognition"] != want_rec:
        errors.append(f"policy.json: recognition {p['recognition']!r} != {want_rec!r}")
    want_alloc = {"rule": "per_invoice_refs", "rounding_remainder": "last_ref",
                  "unapplied_account_role": "unapplied"}
    if p["allocation"] != want_alloc:
        errors.append(f"policy.json: allocation {p['allocation']!r} != {want_alloc!r}")
    a = p["accounts"]
    if not _keys(a, set(ROLES) | {"expense"}, set(ROLES) | {"expense"},
                 "policy.json.accounts", errors):
        return False
    codes = [a[r] for r in ROLES]
    if not all(isinstance(c, str) and c for c in codes):
        errors.append("policy.json: account codes must be non-empty strings")
        ok = False
    if not (isinstance(a["expense"], list) and a["expense"]
            and all(isinstance(c, str) and c for c in a["expense"])):
        errors.append("policy.json: accounts.expense must be a non-empty list of codes")
        ok = False
    elif ok and len(set(codes + a["expense"])) != len(codes) + len(a["expense"]):
        errors.append("policy.json: account codes must be distinct across roles")
        ok = False
    return ok


def _read_csv(text, columns, name, errors):
    reader = csv.reader(io.StringIO(text), strict=True)
    try:
        rows = list(reader)
    except csv.Error as exc:
        errors.append(f"{name}: CSV parse error: {exc}")
        return None
    if not rows or rows[0] != columns:
        errors.append(f"{name}: header must be exactly {','.join(columns)}; got "
                      f"{','.join(rows[0]) if rows else '<empty>'}")
        return None
    out = []
    for i, r in enumerate(rows[1:], start=2):
        if len(r) != len(columns):
            errors.append(f"{name}:{i}: expected {len(columns)} fields, got {len(r)}")
            continue
        out.append(dict(zip(columns, r)))
    return out


def _read_jsonl(text, name, errors, warnings=None):
    out = []
    if text and not text.endswith("\n"):
        errors.append(f"{name}: last line must end with \\n")
    for i, line in enumerate(text.split("\n"), start=1):
        if line == "":
            continue
        try:
            obj = _strict_json(line, f"{name}:{i}", errors, warnings)
        except ValueError as exc:
            errors.append(f"{name}:{i}: invalid JSON: {exc}")
            continue
        out.append((i, obj))
    return out


def _validate_invoice(inv, where, base_exps, offset, errors):
    if not _keys(inv, INVOICE_KEYS, INVOICE_KEYS, where, errors):
        return False
    ok = True
    for k in ("doc_id", "invoice_id", "entity_id"):
        _id_field(inv[k], f"{where}.{k}", errors)
    for k in ("issued_at", "due_at"):
        if _ts_field(inv[k], f"{where}.{k}", errors) is None:
            ok = False
    if inv["currency"] not in base_exps:
        errors.append(f"{where}: currency {inv['currency']!r} not in currency_exponents")
        ok = False
    if not is_int(inv["total_minor"]) or inv["total_minor"] <= 0:
        errors.append(f"{where}: total_minor must be a positive integer")
        ok = False
    items = inv["line_items"]
    if not isinstance(items, list) or not items:
        errors.append(f"{where}: line_items must be a non-empty list")
        return False
    total = 0
    for j, li in enumerate(items):
        lw = f"{where}.line_items[{j}]"
        if not _keys(li, LINE_KEYS, LINE_KEYS, lw, errors):
            ok = False
            continue
        _id_field(li["line_id"], f"{lw}.line_id", errors)
        if not isinstance(li["description"], str):
            errors.append(f"{lw}: description must be a string")
        if not all(is_int(li[k]) for k in ("quantity", "unit_price_minor", "amount_minor")):
            errors.append(f"{lw}: quantity/unit_price_minor/amount_minor must be integers")
            ok = False
            continue
        if li["quantity"] <= 0 or li["unit_price_minor"] < 0:
            errors.append(f"{lw}: quantity must be > 0 and unit price >= 0")
        if li["amount_minor"] != li["quantity"] * li["unit_price_minor"]:
            errors.append(f"{lw}: amount_minor {li['amount_minor']} != quantity × "
                          f"unit_price_minor ({li['quantity']}×{li['unit_price_minor']})")
        total += li["amount_minor"]
    if ok and total != inv["total_minor"]:
        errors.append(f"{where}: Σ line amount_minor {total} != total_minor "
                      f"{inv['total_minor']}")
    if len({li.get("line_id") for li in items if isinstance(li, dict)}) != len(items):
        errors.append(f"{where}: duplicate line_id")
    return ok


def _validate_payment(pay, where, base_exps, invoice_ids, errors):
    if not _keys(pay, PAYMENT_REQUIRED, PAYMENT_KEYS, where, errors):
        return False
    ok = True
    for k in ("doc_id", "payment_id"):
        _id_field(pay[k], f"{where}.{k}", errors)
    _id_field(pay.get("entity_id"), f"{where}.entity_id", errors, nullable=True)
    if _ts_field(pay["timestamp"], f"{where}.timestamp", errors) is None:
        ok = False
    if pay["currency"] not in base_exps:
        errors.append(f"{where}: currency {pay['currency']!r} not in currency_exponents")
        ok = False
    amt = pay["amount_minor"]
    if not is_int(amt) or amt == 0:
        errors.append(f"{where}: amount_minor must be a non-zero integer")
        return False
    refs = pay["invoice_refs"]
    if not isinstance(refs, list):
        errors.append(f"{where}: invoice_refs must be a list")
        return False
    s = 0
    for j, ref in enumerate(refs):
        rw = f"{where}.invoice_refs[{j}]"
        if not _keys(ref, REF_KEYS, REF_KEYS, rw, errors):
            ok = False
            continue
        _id_field(ref["invoice_id"], f"{rw}.invoice_id", errors)
        if ref["invoice_id"] not in invoice_ids:
            errors.append(f"{rw}: invoice_id {ref['invoice_id']!r} does not exist")
            ok = False
        if not is_int(ref["amount_minor"]) or ref["amount_minor"] <= 0:
            errors.append(f"{rw}: amount_minor must be a positive integer")
            ok = False
            continue
        s += ref["amount_minor"]
    if amt < 0 and refs:
        errors.append(f"{where}: refund (amount < 0) must have invoice_refs = []")
        ok = False
    if s > abs(amt):
        errors.append(f"{where}: Σ invoice_refs {s} > |amount_minor| {abs(amt)}")
        ok = False
    return ok


def load_public(path: str) -> Public:
    errors: list = []
    warnings: list = []
    raw, text = {}, {}
    for name in PUBLIC_FILES:
        b, t = _read_text(os.path.join(path, name), errors)
        if b is not None:
            raw[name] = b
        if t is not None:
            text[name] = t
    if len(text) != len(PUBLIC_FILES):
        raise SchemaError(errors)

    # --- manifest & policy
    try:
        manifest = _strict_json(text["manifest.json"], "manifest.json", errors, warnings)
    except ValueError as exc:
        raise SchemaError(errors + [f"manifest.json: invalid JSON: {exc}"])
    if not _validate_manifest(manifest, errors):
        raise SchemaError(errors)
    try:
        policy = _strict_json(text["policy.json"], "policy.json", errors, warnings)
    except ValueError as exc:
        raise SchemaError(errors + [f"policy.json: invalid JSON: {exc}"])
    if not _validate_policy(policy, manifest, errors):
        raise SchemaError(errors)
    for name, h in manifest["files"].items():
        if sha256_bytes(raw[name]) != h:
            errors.append(f"manifest.json: sha256 of {name} does not match file contents")

    base = manifest["base_currency"]
    exps = manifest["currency_exponents"]
    offset = parse_offset(manifest["utc_offset"])
    tz = timezone(offset)
    ex_start = parse_ts(manifest["extract_start"])
    ex_end = parse_ts(manifest["extract_end"])
    accounts = {policy["accounts"][r] for r in ROLES} | set(policy["accounts"]["expense"])

    def in_extract(ts):
        try:
            return ex_start <= parse_ts(ts) < ex_end
        except (ValueError, TypeError):
            return True  # already reported

    # --- invoices
    invoices = []
    for ln, obj in _read_jsonl(text["invoices.jsonl"], "invoices.jsonl", errors, warnings):
        if _validate_invoice(obj, f"invoices.jsonl:{ln}", exps, offset, errors):
            invoices.append(obj)
            if not in_extract(obj["issued_at"]):
                errors.append(f"invoices.jsonl:{ln}: issued_at outside extract window")
    invoice_ids = {d["invoice_id"] for d in invoices}
    if len(invoice_ids) != len(invoices):
        errors.append("invoices.jsonl: duplicate invoice_id")
    # --- payments
    payments = []
    for ln, obj in _read_jsonl(text["payments.jsonl"], "payments.jsonl", errors, warnings):
        if _validate_payment(obj, f"payments.jsonl:{ln}", exps, invoice_ids, errors):
            payments.append(obj)
            if not in_extract(obj["timestamp"]):
                errors.append(f"payments.jsonl:{ln}: timestamp outside extract window")
    payment_ids = {d["payment_id"] for d in payments}
    if len(payment_ids) != len(payments):
        errors.append("payments.jsonl: duplicate payment_id")
    # Order is not semantic (CONTRACT §2): the generator sorts, readers accept any order.
    for name, docs in (("invoices.jsonl", invoices), ("payments.jsonl", payments)):
        ids = [d["doc_id"] for d in docs]
        if ids != sorted(ids):
            warnings.append(f"{name}: documents not sorted by doc_id (accepted)")
    all_doc_ids = [d["doc_id"] for d in invoices + payments]
    if len(set(all_doc_ids)) != len(all_doc_ids):
        errors.append("documents: doc_id not unique across invoices and payments")
    business = invoice_ids | payment_ids
    if set(all_doc_ids) & business:
        errors.append("documents: doc_id collides with an invoice_id/payment_id")

    # --- fx
    fx_rows = []
    rows = _read_csv(text["fx.csv"], FX_COLUMNS, "fx.csv", errors) or []
    seen = set()
    for i, r in enumerate(rows, start=2):
        w = f"fx.csv:{i}"
        good = True
        if not _DATE_RE.match(r["date"]):
            errors.append(f"{w}: bad date {r['date']!r}")
            good = False
        else:
            try:
                date.fromisoformat(r["date"])
            except ValueError:
                errors.append(f"{w}: bad date {r['date']!r}")
                good = False
        if r["source_currency"] not in exps or r["source_currency"] == base:
            errors.append(f"{w}: source_currency {r['source_currency']!r} must be a foreign "
                          f"episode currency")
            good = False
        if r["target_currency"] != base:
            errors.append(f"{w}: target_currency must be base currency {base}")
            good = False
        if not _RATE_RE.match(r["rate"]) or Decimal(r["rate"]) <= 0:
            errors.append(f"{w}: rate {r['rate']!r} must be a positive decimal string "
                          f"with ≤6 decimals")
            good = False
        k = (r["date"], r["source_currency"])
        if k in seen:
            errors.append(f"{w}: duplicate rate for {k}")
            good = False
        seen.add(k)
        if good:
            fx_rows.append(r)
    foreign = sorted(set(exps) - {base})
    d = ex_start.astimezone(tz).date()
    last = (ex_end - timedelta(microseconds=1)).astimezone(tz).date()
    while d <= last:
        for cur in foreign:
            if (d.isoformat(), cur) not in seen:
                errors.append(f"fx.csv: missing rate for {d.isoformat()} {cur}")
        d += timedelta(days=1)

    # --- ledger
    ledger = []
    rows = _read_csv(text["ledger.csv"], LEDGER_COLUMNS, "ledger.csv", errors) or []
    for i, r in enumerate(rows, start=2):
        row = normalize_row(r, f"ledger.csv:{i}", errors, base, offset, accounts)
        if row is not None:
            ledger.append(row)
            if not in_extract(row["timestamp"]):
                errors.append(f"ledger.csv:{i}: timestamp outside extract window")
    try:
        if [row_sort_key(r) for r in ledger] != sorted(row_sort_key(r) for r in ledger):
            warnings.append("ledger.csv: rows not sorted by (timestamp, row_id) (accepted)")
        ledger.sort(key=row_sort_key)  # canonical in-memory order, independent of the file
    except ValueError:
        pass
    errors.extend(check_ledger_integrity(ledger, invoice_ids, payment_ids))

    # --- counts
    counts = manifest["counts"]
    actual = {"ledger_rows": len(rows), "entries": len({r["entry_id"] for r in ledger}),
              "invoices": len(invoices), "payments": len(payments),
              "documents": len(invoices) + len(payments), "fx_rates": len(fx_rows)}
    for k, v in actual.items():
        if counts.get(k) != v:
            errors.append(f"manifest.json: counts.{k} = {counts.get(k)} but files have {v}")

    if errors:
        raise SchemaError(errors)
    return Public(manifest, policy, ledger, invoices, payments, fx_rows, raw, path, warnings)


# --------------------------------------------------------------------------- Private

@dataclass
class Private:
    truth: dict
    provenance: dict
    id_map: dict | None = None
    root: str | None = None
    warnings: list = field(default_factory=list)


def _validate_truth(t, errors):
    if not _keys(t, TRUTH_KEYS, TRUTH_KEYS, "truth.json", errors):
        return
    if t["schema_version"] != SCHEMA_VERSION:
        errors.append("truth.json: wrong schema_version")
    if not isinstance(t["issues"], list):
        errors.append("truth.json: issues must be a list")
        return
    ids = []
    for i, iss in enumerate(t["issues"]):
        w = f"truth.json.issues[{i}]"
        if not _keys(iss, ISSUE_KEYS, ISSUE_KEYS, w, errors):
            continue
        _id_field(iss["issue_id"], f"{w}.issue_id", errors)
        ids.append(iss["issue_id"])
        if iss["type"] not in ISSUE_TYPES:
            errors.append(f"{w}: unknown type {iss['type']!r}")
        elif iss["tolerance_minor"] != TOLERANCE[iss["type"]]:
            errors.append(f"{w}: tolerance_minor {iss['tolerance_minor']!r} != contract "
                          f"{TOLERANCE[iss['type']]} for {iss['type']}")
        aff = iss["affected_ids"]
        if not (isinstance(aff, list) and len(aff) == 1 and is_id(aff[0])):
            errors.append(f"{w}: affected_ids must be exactly [doc_id]")
        if not is_int(iss["expected_amount_minor"]) or iss["expected_amount_minor"] < 0:
            errors.append(f"{w}: expected_amount_minor must be a non-negative integer")
        if iss["currency"] != t["base_currency"]:
            errors.append(f"{w}: currency must be the base currency {t['base_currency']}")
        es = iss["evidence_sets"]
        if not (isinstance(es, list) and es and all(
                isinstance(s, list) and s and all(is_id(x) for x in s) for s in es)):
            errors.append(f"{w}: evidence_sets must be a non-empty list of non-empty id lists")
        else:
            for s in es:
                if len(set(s)) > 10:
                    errors.append(f"{w}: evidence set {s} has > 10 ids (unsatisfiable)")
    if ids != sorted(ids):
        errors.append("truth.json: issues must be sorted by issue_id")
    if len(set(ids)) != len(ids):
        errors.append("truth.json: duplicate issue_id")


def _validate_provenance(p, errors):
    if not _keys(p, PROVENANCE_KEYS, PROVENANCE_KEYS, "provenance.json", errors):
        return
    if p["schema_version"] != SCHEMA_VERSION:
        errors.append("provenance.json: wrong schema_version")
    if not isinstance(p["generator_version"], str):
        errors.append("provenance.json: generator_version must be a string")
    if not isinstance(p["config"], dict):
        errors.append("provenance.json: config must be an object")
    if not (isinstance(p["config_sha256"], str) and _HEX64.match(p["config_sha256"])):
        errors.append("provenance.json: config_sha256 must be sha256 hex")
    if not is_int(p["seed"]):
        errors.append("provenance.json: seed must be an integer")
    bs = p["baseline_sha256"]
    if not (isinstance(bs, dict) and "ledger.csv" in bs and all(
            isinstance(v, str) and _HEX64.match(v) for v in bs.values())):
        errors.append("provenance.json: baseline_sha256 must map file -> sha256 and "
                      "include ledger.csv")
    if not isinstance(p["distractors"], list):
        errors.append("provenance.json: distractors must be a list")
    if not isinstance(p["changes"], list):
        errors.append("provenance.json: changes must be a list")
        return
    orders = []
    for i, ch in enumerate(p["changes"]):
        w = f"provenance.json.changes[{i}]"
        if not _keys(ch, CHANGE_KEYS, CHANGE_KEYS, w, errors):
            continue
        if not is_int(ch["order"]):
            errors.append(f"{w}: order must be an integer")
        orders.append(ch["order"])
        if ch["op"] not in OPS:
            errors.append(f"{w}: op {ch['op']!r} not in {OPS}")
            continue
        need_before = ch["op"] in ("modify_row", "delete_row")
        need_after = ch["op"] in ("modify_row", "add_row")
        if (ch["before"] is not None) != need_before or (ch["after"] is not None) != need_after:
            errors.append(f"{w}: {ch['op']} needs before={'row' if need_before else 'null'}"
                          f", after={'row' if need_after else 'null'}")
        for k in ("before", "after"):
            if ch[k] is not None:
                normalize_row(ch[k], f"{w}.{k}", errors)
        if not isinstance(ch["cause"], str) or not ch["cause"]:
            errors.append(f"{w}: cause must be a non-empty string")
    if len(set(orders)) != len(orders):
        errors.append("provenance.json: change orders must be unique")


def load_private(path: str) -> Private:
    errors: list = []
    warnings: list = []
    out = {}
    for name in ("truth.json", "provenance.json", "id_map.json"):
        fp = os.path.join(path, name)
        if name == "id_map.json" and not os.path.exists(fp):
            out[name] = None
            continue
        _, t = _read_text(fp, errors)
        if t is None:
            continue
        try:
            out[name] = _strict_json(t, name, errors, warnings)
        except ValueError as exc:
            errors.append(f"{name}: invalid JSON: {exc}")
    if errors:
        raise SchemaError(errors)
    _validate_truth(out["truth.json"], errors)
    _validate_provenance(out["provenance.json"], errors)
    im = out["id_map.json"]
    if im is not None:
        if not (isinstance(im, dict) and set(im) == {"source_episode_id", "episode_id", "ids"}
                and isinstance(im["ids"], dict)
                and all(is_id(k) and is_id(v) for k, v in im["ids"].items())):
            errors.append("id_map.json: must be {source_episode_id, episode_id, ids{old:new}}")
        elif len(set(im["ids"].values())) != len(im["ids"]):
            errors.append("id_map.json: mapping is not injective")
    if errors:
        raise SchemaError(errors)
    return Private(out["truth.json"], out["provenance.json"], im, path, warnings)
