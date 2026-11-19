"""Small, reproducible integration episodes pending the team's full generator."""

import csv
from decimal import Decimal, ROUND_HALF_UP
import json
from pathlib import Path
from random import Random

from .schema import fx_id

IN_PERIOD = "2026-11-30T23:55:00+05:00"
OUT_OF_PERIOD = "2026-12-01T00:10:00+05:00"
EARLIER = "2026-11-29T09:00:00+05:00"
DUE = "2026-12-15T09:00:00+05:00"
DEV_CASES = {
    "demo-mixed": ("duplicate_invoice", "payment_allocation"),
    "demo-clean": (),
    "dev-duplicate": ("duplicate_invoice",),
    "dev-allocation": ("payment_allocation",),
    "dev-fx": ("fx_conversion",),
    "dev-period": ("reporting_period",),
    "dev-allocation-fx": ("payment_allocation", "fx_conversion"),
    "dev-duplicate-period": ("duplicate_invoice", "reporting_period"),
    "dev-clean-split": (),
    "dev-clean-repeat": (),
}
DEMO_CASES = ("demo-mixed", "demo-clean")


def dump_json(path, payload):
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def dump_jsonl(path, rows):
    with path.open("w", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


def dump_csv(path, rows):
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def make_episode(root, episode_id, *, seed, errors=(), permute=False):
    public = Path(root) / "public"
    private = Path(root) / "private"
    public.mkdir(parents=True, exist_ok=True)
    private.mkdir(parents=True, exist_ok=True)
    rng = Random(seed)
    ledger, invoices, payments, issues, changes = [], [], [], [], []
    prefix = "P" if permute else "R"
    entity = f"{prefix}-entity-1"

    def invoice(currency, amount, issued_at):
        number = len(invoices) + 1
        record = {
            "doc_id": f"{prefix}-doc-invoice-{number}", "invoice_id": f"{prefix}-invoice-{number}",
            "entity_id": entity, "issued_at": issued_at, "due_at": DUE, "currency": currency,
            "total_minor": amount, "line_items": [{"description": "Training services", "amount_minor": amount}],
        }
        invoices.append(record)
        return record

    def payment(currency, amount, issued_at, invoiced):
        number = len(payments) + 1
        record = {
            "doc_id": f"{prefix}-doc-payment-{number}", "payment_id": f"{prefix}-payment-{number}",
            "timestamp": issued_at, "currency": currency, "amount_minor": amount,
            "invoice_refs": [invoiced["invoice_id"]],
        }
        payments.append(record)
        return record

    def post(account, amount, timestamp, invoiced=None, paid=None):
        record = {
            "row_id": f"{prefix}-row-{len(ledger) + 1:03}", "timestamp": timestamp,
            "account": account, "debit_minor": max(amount, 0), "credit_minor": max(-amount, 0),
            "currency": "USD", "entity_id": entity,
            "invoice_id": invoiced["invoice_id"] if invoiced else None,
            "payment_id": paid["payment_id"] if paid else None,
        }
        ledger.append(record)
        return record["row_id"]

    def issue(kind, affected, amount, evidence, before, after):
        issue_id = f"{prefix}-issue-{len(issues) + 1}"
        issues.append({
            "issue_id": issue_id, "type": kind, "affected_ids": affected,
            "expected_amount_minor": amount, "currency": "USD", "evidence_sets": [evidence],
        })
        changes.append({"issue_id": issue_id, "before": before, "after": after})

    usd_amount = rng.randrange(3000, 4500)
    usd_invoice = invoice("USD", usd_amount, IN_PERIOD)
    same_amount = invoice("USD", usd_amount, IN_PERIOD)
    euro_invoice = invoice("EUR", rng.randrange(4000, 5000), EARLIER)
    for record in (usd_invoice, same_amount):
        amount = record["total_minor"]
        receivable = post("accounts_receivable", amount, IN_PERIOD, record)
        post("service_revenue", -amount, IN_PERIOD, record)
        has_duplicate = record == usd_invoice and "duplicate_invoice" in errors
        has_period_error = record == usd_invoice and "reporting_period" in errors
        if has_duplicate:
            timestamp = OUT_OF_PERIOD if has_period_error else IN_PERIOD
            duplicate_row = post("accounts_receivable", amount, timestamp, record)
            post("service_revenue", -amount, timestamp, record)
            issue("duplicate_invoice", [record["invoice_id"]], amount,
                  [record["doc_id"], receivable, duplicate_row], 1, 2)
        if has_period_error:
            if has_duplicate:
                shifted_row = duplicate_row
            else:
                shifted_row = receivable
                ledger[-2]["timestamp"] = OUT_OF_PERIOD
                ledger[-1]["timestamp"] = OUT_OF_PERIOD
            issue("reporting_period", [record["invoice_id"]], amount,
                  [record["doc_id"], shifted_row], IN_PERIOD, OUT_OF_PERIOD)

    # The reference invoice revenue uses issuance FX. The payment uses payment-date FX.
    rates = [
        {"date": "2026-11-29", "source_currency": "EUR", "target_currency": "USD", "rate": "1.10"},
        {"date": "2026-11-30", "source_currency": "EUR", "target_currency": "USD", "rate": "1.25"},
    ]

    def convert(amount, rate):
        return int((Decimal(amount) * Decimal(rate["rate"])).quantize(Decimal("1"), rounding=ROUND_HALF_UP))

    euro_receivable = convert(euro_invoice["total_minor"], rates[0])
    post("accounts_receivable", euro_receivable, EARLIER, euro_invoice)
    post("service_revenue", -euro_receivable, EARLIER, euro_invoice)
    has_fx_error = "fx_conversion" in errors
    cross_currency_combination = has_fx_error and "payment_allocation" in errors

    split_amount = usd_amount // 3
    usd_payment = payment("USD", split_amount, IN_PERIOD, usd_invoice)
    post("cash", split_amount, IN_PERIOD, paid=usd_payment)
    installment = split_amount // 2
    shortfall = 125 if "payment_allocation" in errors and not cross_currency_combination else 0
    first = post("accounts_receivable", -installment, IN_PERIOD, usd_invoice, usd_payment)
    second = post("accounts_receivable", -(split_amount - installment - shortfall), IN_PERIOD, usd_invoice, usd_payment)
    if shortfall:
        post("unapplied_cash", -shortfall, IN_PERIOD, paid=usd_payment)
        issue("payment_allocation", [usd_payment["payment_id"]], shortfall,
              [usd_payment["doc_id"], usd_invoice["doc_id"], first, second], split_amount, split_amount - shortfall)

    # Book an installment and clear the receivable at the invoice's original FX value.
    euro_installment = euro_invoice["total_minor"] // 2
    euro_payment = payment("EUR", euro_installment, IN_PERIOD, euro_invoice)
    correct = convert(euro_installment, rates[1])
    reflected = convert(euro_installment, rates[0]) if has_fx_error else correct
    applied = convert(euro_installment, rates[0])
    residual = 100 if cross_currency_combination else 0
    cash_row = post("cash", reflected, IN_PERIOD, paid=euro_payment)
    alloc_row = post("accounts_receivable", -(applied - residual), IN_PERIOD, euro_invoice, euro_payment)
    realized_fx = reflected - applied
    if realized_fx:
        post("fx_gain", -realized_fx, IN_PERIOD, paid=euro_payment)
    if has_fx_error:
        issue("fx_conversion", [euro_payment["payment_id"]], correct - reflected,
              [euro_payment["doc_id"], cash_row, fx_id(rates[1])], correct, reflected)
    if residual:
        suspense = post("unapplied_cash", -residual, IN_PERIOD, paid=euro_payment)
        issue("payment_allocation", [euro_payment["payment_id"]], residual,
              [euro_payment["doc_id"], euro_invoice["doc_id"], alloc_row, suspense, fx_id(rates[0])],
              applied, applied - residual)

    # Change presentation order without changing the financial relations.
    if permute:
        for records in (ledger, invoices, payments, rates):
            rng.shuffle(records)

    rounding = "ROUND_HALF_UP"
    dump_json(public / "manifest.json", {
        "schema_version": 1, "episode_id": episode_id, "base_currency": "USD",
        "period_start": "2026-11-01T00:00:00+05:00", "period_end": "2026-12-01T00:00:00+05:00",
        "timezone": "Asia/Almaty", "record_count": len(ledger), "document_count": len(invoices) + len(payments),
        "currency_exponents": {"USD": 2, "EUR": 2}, "rounding": rounding,
    })
    dump_json(public / "policy.json", {
        "fx_date_rule": "Use issue date for invoices, payment date for cash and issue date for receivable settlement. Use local dates.",
        "period_rule": "Book invoice transactions in the local period of issuance. Adjacent boundary records may be inspected.",
        "rounding": rounding,
    })
    dump_csv(public / "ledger.csv", ledger)
    dump_jsonl(public / "invoices.jsonl", invoices)
    dump_jsonl(public / "payments.jsonl", payments)
    dump_csv(public / "fx.csv", rates)
    dump_json(private / "truth.json", {"episode_id": episode_id, "issues": issues})
    dump_json(private / "provenance.json", {"seed": seed, "changes": changes})


def make_dev(root, *, demo=False):
    names = DEMO_CASES if demo else DEV_CASES
    for index, name in enumerate(names):
        make_episode(Path(root) / name, name, seed=400 + index, errors=DEV_CASES[name])
