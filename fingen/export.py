"""Public / private export (A05-A07, CONTRACT §1-§3, §6).

Only fields named by the contract are serialised; private metadata on the
history objects (roles, tags, names, distractors, ...) never reaches public
files. All renderers return ``bytes`` so that hashing and writing use exactly
the same content.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os

from . import GENERATOR_VERSION, SCHEMA_VERSION
from .history import LEDGER_COLUMNS, History
from .timeutil import format_ts

PUBLIC_DATA_FILES = ("policy.json", "ledger.csv", "invoices.jsonl", "payments.jsonl", "fx.csv")
PUBLIC_FILES = ("manifest.json",) + PUBLIC_DATA_FILES
PRIVATE_FILES = ("truth.json", "provenance.json")
FX_COLUMNS = ("date", "source_currency", "target_currency", "rate")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def dumps_json(obj) -> bytes:
    return (json.dumps(obj, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False)
            + "\n").encode("utf-8")


def dumps_jsonl(objs) -> bytes:
    return "".join(json.dumps(o, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                              allow_nan=False) + "\n" for o in objs).encode("utf-8")


def dumps_csv(header, rows) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n", quoting=csv.QUOTE_MINIMAL)
    w.writerow(header)
    for row in rows:
        w.writerow(["" if v is None else str(v) for v in row])
    return buf.getvalue().encode("utf-8")


def _rounding(h: History) -> dict:
    return {"mode": h.rounding_mode, "scope": "per_conversion"}


def render_policy(h: History) -> bytes:
    return dumps_json({
        "schema_version": SCHEMA_VERSION,
        "episode_id": h.episode_id,
        "base_currency": h.base_currency,
        "period": {"start": format_ts(h.period_start), "end": format_ts(h.period_end),
                   "end_exclusive": True, "timezone": h.timezone, "utc_offset": h.utc_offset},
        "fx": {"date_rule": "event_local_date", "target_currency": h.base_currency,
               "rate_direction": "target_per_source"},
        "rounding": _rounding(h),
        "recognition": {"invoice": "issued_at_local_date", "payment": "timestamp_local_date"},
        "allocation": {"rule": "per_invoice_refs", "rounding_remainder": "last_ref",
                       "unapplied_account_role": "unapplied"},
        "accounts": {"ar": h.accounts["ar"], "revenue": h.accounts["revenue"],
                     "bank": h.accounts["bank"], "unapplied": h.accounts["unapplied"],
                     "expense": sorted(h.accounts["expense"])},
    })


def render_ledger(h: History) -> bytes:
    rows = h.sorted_rows()
    return dumps_csv(LEDGER_COLUMNS, ([r.to_public()[c] for c in LEDGER_COLUMNS] for r in rows))


def render_invoices(h: History) -> bytes:
    return dumps_jsonl(h.invoices[k].to_public() for k in sorted(h.invoices))


def render_payments(h: History) -> bytes:
    return dumps_jsonl(h.payments[k].to_public() for k in sorted(h.payments))


def render_fx(h: History) -> bytes:
    keys = sorted(h.fx)
    return dumps_csv(FX_COLUMNS, ((d, src, h.base_currency, h.fx[(d, src)]) for d, src in keys))


def counts(h: History) -> dict:
    return {"ledger_rows": len(h.rows),
            "entries": len({r.entry_id for r in h.rows.values()}),
            "invoices": len(h.invoices), "payments": len(h.payments),
            "documents": len(h.invoices) + len(h.payments), "fx_rates": len(h.fx)}


def render_manifest(h: History, data_files: dict[str, bytes]) -> bytes:
    return dumps_json({
        "schema_version": SCHEMA_VERSION,
        "episode_id": h.episode_id,
        "company": {"name": h.company_name},
        "base_currency": h.base_currency,
        "currency_exponents": h.currency_exponents,
        "timezone": h.timezone,
        "utc_offset": h.utc_offset,
        "period_start": format_ts(h.period_start),
        "period_end": format_ts(h.period_end),
        "extract_start": format_ts(h.extract_start),
        "extract_end": format_ts(h.extract_end),
        "counts": counts(h),
        "rounding": _rounding(h),
        "files": {name: sha256_hex(data_files[name]) for name in PUBLIC_DATA_FILES},
    })


def render_public(h: History) -> dict[str, bytes]:
    """All six public files, keyed by file name."""
    data = {"policy.json": render_policy(h), "ledger.csv": render_ledger(h),
            "invoices.jsonl": render_invoices(h), "payments.jsonl": render_payments(h),
            "fx.csv": render_fx(h)}
    return {"manifest.json": render_manifest(h, data), **data}


def render_truth(h: History, issues) -> bytes:
    return dumps_json({
        "schema_version": SCHEMA_VERSION,
        "episode_id": h.episode_id,
        "base_currency": h.base_currency,
        "issues": [i.to_dict() for i in sorted(issues, key=lambda i: i.issue_id)],
    })


def render_provenance(h: History, baseline_sha256: dict[str, str]) -> bytes:
    return dumps_json({
        "schema_version": SCHEMA_VERSION,
        "episode_id": h.episode_id,
        "generator_version": GENERATOR_VERSION,
        "config": h.config,
        "config_sha256": h.config_sha256,
        "seed": h.seed,
        "baseline_sha256": dict(sorted(baseline_sha256.items())),
        "changes": sorted(h.changes, key=lambda c: c["order"]),
        "distractors": h.distractors,
    })


def write_files(directory: str, files: dict[str, bytes]) -> None:
    os.makedirs(directory, exist_ok=True)
    for name in sorted(files):
        with open(os.path.join(directory, name), "wb") as fh:
            fh.write(files[name])


def episode_dirs(out_dir: str, split: str, episode_id: str) -> tuple[str, str]:
    return (os.path.join(out_dir, "public", split, episode_id),
            os.path.join(out_dir, "private", split, episode_id))
