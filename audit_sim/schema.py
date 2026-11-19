"""Stable identifiers and JSON types shared by the data loader and evaluator."""

ISSUE_TYPES = (
    "duplicate_invoice",
    "payment_allocation",
    "fx_conversion",
    "reporting_period",
)
V1_ISSUE_TYPES = (*ISSUE_TYPES[:3], "wrong_period")
TABLES = ("ledger", "invoices", "payments", "fx")


def fx_id(record):
    return "{date}:{source_currency}:{target_currency}".format_map(record)


def record_id(table, record):
    if table == "fx":
        return fx_id(record)
    return record[{"ledger": "row_id", "invoices": "invoice_id", "payments": "payment_id"}[table]]


def is_id(value):
    return isinstance(value, str) and 0 < len(value) <= 256


def is_currency(value):
    return isinstance(value, str) and len(value) == 3 and value.isascii() and value.isalpha() and value.isupper()


def valid_ids(values, minimum=1, maximum=100):
    return isinstance(values, list) and minimum <= len(values) <= maximum and all(map(is_id, values))
