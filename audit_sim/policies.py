"""Built-in policies. Each gets only the public request/response interface."""

from collections import defaultdict
from random import Random

from .schema import ISSUE_TYPES, TABLES, record_id

POLICIES = ("random", "reconcile", "mark_all", "oracle")


def finding(kind, affected, amount, currency, evidence, explanation):
    return {
        "type": kind,
        "affected_ids": affected,
        "claimed_amount_minor": amount,
        "currency": currency,
        "evidence_ids": evidence,
        "explanation": explanation,
    }


def page(request, table, cursor=None):
    return request({"action": "list_records", "table": table, "cursor": cursor, "limit": 50})


def rows(request, table, budget):
    result = []
    cursor = None
    while budget > 1:
        response = page(request, table, cursor)
        budget = response["steps_remaining"]
        result.extend(response.get("result", []))
        cursor = response.get("next_cursor")
        if cursor is None:
            return result, budget
    return None, budget


def random_explorer(request, initial, seed):
    random = Random(seed)
    budget = initial["steps_remaining"]
    for _ in range(min(8, budget - 1)):
        table = random.choice(TABLES)
        entry = page(request, table).get("result", [])
        if entry:
            chosen = random.choice(entry)
            request({"action": "get_record", "table": table, "id": record_id(table, chosen)})
    return []


def reconcile(request, initial, seed):
    tables = {}
    budget = initial["steps_remaining"]
    for table in ("invoices", "payments", "ledger"):
        records, budget = rows(request, table, budget)
        if records is None:
            return []
        tables[table] = records
    invoices = {row["invoice_id"]: row for row in tables["invoices"]}
    debits = defaultdict(list)
    credits = defaultdict(list)
    payment_currencies = defaultdict(set)
    for row in tables["ledger"]:
        if row["payment_id"] is not None:
            payment_currencies[row["payment_id"]].add(row["currency"])
        if row["account"] != "accounts_receivable":
            continue
        if row["payment_id"] is None and row["debit_minor"] > 0:
            debits[row["invoice_id"], row["currency"]].append(row)
        if row["payment_id"] is not None and row["invoice_id"] is not None and row["credit_minor"] > 0:
            credits[row["payment_id"], row["currency"]].append(row)

    findings = []
    for key, entries in debits.items():
        invoice_id, currency = key
        invoice = invoices.get(invoice_id)
        if invoice is None or invoice["currency"] != currency:
            continue
        amount = invoice["total_minor"]
        copies = [row["row_id"] for row in entries if row["debit_minor"] == amount]
        if 1 < len(copies) <= 9:
            findings.append(finding(
                "duplicate_invoice", [invoice_id], amount * (len(copies) - 1),
                currency, [invoice["doc_id"], *copies],
                "Multiple full receivable postings reference the same invoice.",
            ))

    for payment in tables["payments"]:
        payment_id = payment["payment_id"]
        currency = payment["currency"]
        references = payment["invoice_refs"]
        if payment_currencies[payment_id] != {currency} or not references or not all(
            key in invoices and invoices[key]["currency"] == currency for key in references
        ):
            continue
        entries = credits[payment_id, currency]
        if any(row["invoice_id"] not in references for row in entries):
            continue
        difference = payment["amount_minor"] - sum(row["credit_minor"] for row in entries)
        if 0 < difference and len(entries) + len(references) < 10:
            findings.append(finding(
                "payment_allocation", [payment_id], difference, currency,
                [payment["doc_id"], *(invoices[ref]["doc_id"] for ref in references),
                 *(row["row_id"] for row in entries)],
                "Same-currency invoice allocations total less than the payment.",
            ))
    return findings[:30]


def mark_all(request, initial, seed):
    records = page(request, "ledger").get("result", [])
    return [
        finding(kind, [row["row_id"]], max(row["debit_minor"], row["credit_minor"]),
                row["currency"], [row["row_id"]], "Broad accusation control.")
        for row in records[:8]
        for kind in ISSUE_TYPES
    ][:30]


WORKER_POLICIES = {"random": random_explorer, "reconcile": reconcile, "mark_all": mark_all}
