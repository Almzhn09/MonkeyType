"""Built-in policies. Each gets only the public request/response interface."""

from collections import defaultdict
from random import Random

from fincheck.load import Public
from fincheck.recompute import reconcile as check_public

from .schema import ISSUE_TYPES, TABLES, record_id

POLICY_ROLES = {
    "random": "random_baseline",
    "reconcile": "public_solvability_check",
    "mark_all": "mass_accusation_control",
    "oracle": "evaluator_answer_key",
    "simple": "simple_baseline",
}
POLICIES = tuple(POLICY_ROLES)


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
            key = record_id(table, chosen)
            if table == "fx" and initial["manifest"]["schema_version"] == "1.0":
                key = "FX:" + key
            request({"action": "get_record", "table": table, "id": key})
    return []


def read_tables(request, initial, names):
    tables = {}
    budget = initial["steps_remaining"]
    for table in names:
        records, budget = rows(request, table, budget)
        if records is None:
            return None
        tables[table] = records
    return tables


def reconcile(request, initial, seed):
    """Reference solvability check on public records, with the legacy heuristic for v0."""
    v1 = initial["manifest"]["schema_version"] == "1.0"
    names = ("invoices", "payments", "ledger", "fx") if v1 else ("invoices", "payments", "ledger")
    tables = read_tables(request, initial, names)
    if tables is None:
        return []
    if v1:
        public = Public(
            initial["manifest"], initial["policy"], tables["ledger"], tables["invoices"],
            tables["payments"], tables["fx"],
        )
        return check_public(public)[:30]
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


def simple_reconcile(request, initial, seed):
    """Compare postings with documents in the base currency using elementary checks."""
    if initial["manifest"]["schema_version"] != "1.0":
        return reconcile(request, initial, seed)
    tables = read_tables(request, initial, ("invoices", "payments", "ledger"))
    if tables is None:
        return []
    base = initial["manifest"]["base_currency"]
    accounts = initial["policy"]["accounts"]
    receivables = defaultdict(list)
    unapplied = defaultdict(list)
    for row in tables["ledger"]:
        if row["currency"] != base:
            continue
        if row["payment_id"] is None and row["account"] == accounts["ar"] and row["debit_minor"] > 0:
            receivables[row["invoice_id"]].append(row)
        if row["payment_id"] is not None and row["account"] == accounts["unapplied"] and row["credit_minor"] > 0:
            unapplied[row["payment_id"]].append(row)

    results = []
    for invoice in sorted(tables["invoices"], key=lambda document: document["doc_id"]):
        if invoice["currency"] != base:
            continue
        amount = invoice["total_minor"]
        postings = receivables[invoice["invoice_id"]]
        if (len(postings) == 2 and postings[0]["entry_id"] != postings[1]["entry_id"]
                and all(row["debit_minor"] == amount for row in postings)):
            doc_id = invoice["doc_id"]
            results.append(finding("duplicate_invoice", [doc_id], amount, base,
                                   [doc_id, *sorted(row["row_id"] for row in postings)],
                                   "Two full receivable postings reference the same invoice."))

    for payment in sorted(tables["payments"], key=lambda document: document["doc_id"]):
        if payment["currency"] != base or payment["amount_minor"] <= 0:
            continue
        intended = sum(ref["amount_minor"] for ref in payment["invoice_refs"])
        postings = unapplied[payment["payment_id"]]
        if intended == payment["amount_minor"] and 0 < len(postings) <= 9:
            doc_id = payment["doc_id"]
            results.append(finding("payment_allocation", [doc_id], sum(row["credit_minor"] for row in postings), base,
                                   [doc_id, *sorted(row["row_id"] for row in postings)],
                                   "A payment fully assigned to invoices has an unapplied balance."))
    return results[:30]


def mark_all(request, initial, seed):
    records = page(request, "ledger").get("result", [])
    if initial["manifest"]["schema_version"] == "1.0":
        return mark_visible_documents(request, initial, records)
    return [
        finding(kind, [row["row_id"]], max(row["debit_minor"], row["credit_minor"]),
                row["currency"], [row["row_id"]], "Broad accusation control.")
        for row in records[:8]
        for kind in ISSUE_TYPES
    ][:30]


def mark_visible_documents(request, initial, records):
    invoices = page(request, "invoices").get("result", [])
    payments = page(request, "payments").get("result", [])
    account_map = initial["policy"]["accounts"]
    amounts = defaultdict(list)
    for row in records:
        if row["payment_id"] is not None and row["account"] in (account_map["bank"], account_map["unapplied"]):
            amounts[row["payment_id"]].append(row)
        elif row["invoice_id"] is not None and row["payment_id"] is None and row["account"] == account_map["ar"]:
            amounts[row["invoice_id"]].append(row)
    currency = initial["manifest"]["base_currency"]
    groups = (
        (invoices, "invoice_id", "total_minor", ("duplicate_invoice", "wrong_period", "fx_conversion")),
        (payments, "payment_id", "amount_minor", ("payment_allocation", "fx_conversion")),
    )
    # Strike broadly on real document IDs, using whatever postings were actually observed.
    accusations = []
    for documents, identifier, amount_field, kinds in groups:
        for document in documents[:6]:
            doc_id = document["doc_id"]
            linked = amounts[document[identifier]]
            claimed = abs(document[amount_field]) if document["currency"] == currency else 0
            if linked and identifier == "payment_id":
                unapplied = [entry for entry in linked if entry["account"] == account_map["unapplied"]]
                claimed = sum(row["credit_minor"] for row in unapplied) or linked[0]["debit_minor"]
            elif linked:
                claimed = linked[0]["debit_minor"]
            evidence = [doc_id, *(row["row_id"] for row in linked[:9])]
            for kind in kinds:
                accusations.append(finding(
                    kind, [doc_id], claimed, currency, evidence,
                    "Flagged without resolving the discrepancy for this type.",
                ))
    return accusations[:30]


WORKER_POLICIES = {"random": random_explorer, "reconcile": reconcile, "mark_all": mark_all, "simple": simple_reconcile}
