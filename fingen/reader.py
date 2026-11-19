"""Minimal reader for a generated episode (used by tests and tooling).

Parses public files exactly as an external loader would: CSV empty fields
become ``None``, money columns become ``int``, FX rates stay strings.
"""

from __future__ import annotations

import csv
import json
import os

INT_LEDGER_FIELDS = ("debit_minor", "credit_minor")


def _read_csv(path: str) -> tuple[list[str], list[dict]]:
    with open(path, "r", encoding="utf-8", newline="") as fh:
        reader = csv.reader(fh)
        header = next(reader)
        rows = [dict(zip(header, [None if v == "" else v for v in rec])) for rec in reader]
    return header, rows


def _read_jsonl(path: str) -> list[dict]:
    with open(path, "r", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh.read().split("\n") if line]


def _read_json(path: str):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def load_public(directory: str) -> dict:
    ledger_header, ledger = _read_csv(os.path.join(directory, "ledger.csv"))
    for row in ledger:
        for f in INT_LEDGER_FIELDS:
            row[f] = int(row[f])
    fx_header, fx = _read_csv(os.path.join(directory, "fx.csv"))
    return {
        "manifest": _read_json(os.path.join(directory, "manifest.json")),
        "policy": _read_json(os.path.join(directory, "policy.json")),
        "ledger_header": ledger_header,
        "ledger": ledger,
        "invoices": _read_jsonl(os.path.join(directory, "invoices.jsonl")),
        "payments": _read_jsonl(os.path.join(directory, "payments.jsonl")),
        "fx_header": fx_header,
        "fx": fx,
    }


def load_private(directory: str) -> dict:
    return {"truth": _read_json(os.path.join(directory, "truth.json")),
            "provenance": _read_json(os.path.join(directory, "provenance.json"))}
