"""Trusted-side loader for an episode with separate public and private directories."""

import csv
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from fincheck.load import fx_key as v1_fx_key, load_private, load_public
from fingen.generate import generate_episode

from .schema import ISSUE_TYPES, TABLES, is_currency, is_id, record_id, valid_ids

MANIFEST_FIELDS = (
    "schema_version", "episode_id", "base_currency", "period_start", "period_end",
    "timezone", "record_count", "document_count", "rounding", "currency_exponents",
)
POLICY_FIELDS = ("fx_date_rule", "period_rule", "rounding")
DOCUMENT_FIELDS = {
    "invoices": ("doc_id", "invoice_id", "entity_id", "issued_at", "due_at", "currency", "total_minor", "line_items"),
    "payments": ("doc_id", "payment_id", "timestamp", "currency", "amount_minor", "invoice_refs"),
    "fx": ("date", "source_currency", "target_currency", "rate"),
}


def load_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def load_jsonl(path):
    with path.open(encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def load_csv(path):
    with path.open(encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream))


def select_fields(rows, fields):
    return [{field: row[field] for field in fields} for row in rows]


def typed_ledger(row):
    return {
        "row_id": row["row_id"],
        "timestamp": row["timestamp"],
        "account": row["account"],
        "debit_minor": int(row["debit_minor"]),
        "credit_minor": int(row["credit_minor"]),
        "currency": row["currency"],
        "entity_id": row["entity_id"],
        "invoice_id": row["invoice_id"] or None,
        "payment_id": row["payment_id"] or None,
    }


def aware_timestamp(value):
    return datetime.fromisoformat(value).utcoffset() is not None


def valid_record(table, record):
    if table == "fx":
        try:
            rate = Decimal(record["rate"])
        except (InvalidOperation, TypeError):
            return False
        return (
            isinstance(record["rate"], str)
            and rate.is_finite() and rate > 0
            and date.fromisoformat(record["date"]).isoformat() == record["date"]
            and is_currency(record["source_currency"]) and is_currency(record["target_currency"])
        )
    if not is_currency(record["currency"]):
        return False
    if table == "ledger":
        return aware_timestamp(record["timestamp"]) and all(
            record[key] is None or is_id(record[key]) for key in ("invoice_id", "payment_id")
        )
    if table == "invoices":
        return (
            type(record["total_minor"]) is int and isinstance(record["line_items"], list)
            and is_id(record["entity_id"])
            and aware_timestamp(record["issued_at"]) and aware_timestamp(record["due_at"])
        )
    references = record["invoice_refs"]
    return (
        type(record["amount_minor"]) is int and aware_timestamp(record["timestamp"])
        and (references is None or isinstance(references, list) and all(map(is_id, references)))
    )


@dataclass(frozen=True)
class Episode:
    manifest: dict
    policy: dict
    tables: dict
    records: dict
    documents: dict
    evidence_ids: frozenset
    issues: tuple

    @classmethod
    def load(cls, directory, private_directory=None):
        root = Path(directory)
        if private_directory is not None:
            return cls.load_v1(root, Path(private_directory))
        public = root / "public"
        incoming = load_json(public / "manifest.json")
        if incoming["schema_version"] == "1.0":
            return cls.load_v1(public, root / "private")
        manifest = {field: incoming[field] for field in MANIFEST_FIELDS}
        version = manifest["schema_version"]
        if type(version) not in (str, int) or version not in (1, "1"):
            raise ValueError(f"Unsupported schema version: {version!r}")
        if not is_id(manifest["episode_id"]) or not is_currency(manifest["base_currency"]):
            raise ValueError("Invalid episode ID or base currency")
        if not all(type(manifest[key]) is int and manifest[key] >= 0 for key in ("record_count", "document_count")):
            raise ValueError("Invalid manifest counts")
        exponents = manifest["currency_exponents"]
        if (
            not isinstance(exponents, dict) or manifest["base_currency"] not in exponents
            or not all(is_currency(code) and type(exp) is int and 0 <= exp <= 9 for code, exp in exponents.items())
        ):
            raise ValueError("Invalid currency exponents")

        incoming_policy = load_json(public / "policy.json")
        policy = {field: incoming_policy[field] for field in POLICY_FIELDS}
        if not all(isinstance(rule, str) and rule for rule in policy.values()):
            raise ValueError("Policy rules must be nonempty strings")
        if (
            manifest["rounding"] != policy["rounding"]
            or not all(aware_timestamp(manifest[name]) for name in ("period_start", "period_end"))
            or datetime.fromisoformat(manifest["period_start"]) >= datetime.fromisoformat(manifest["period_end"])
            or not isinstance(manifest["timezone"], str) or not manifest["timezone"]
        ):
            raise ValueError("Invalid period or rounding policy")
        tables = {
            "ledger": [typed_ledger(row) for row in load_csv(public / "ledger.csv")],
            "invoices": select_fields(load_jsonl(public / "invoices.jsonl"), DOCUMENT_FIELDS["invoices"]),
            "payments": select_fields(load_jsonl(public / "payments.jsonl"), DOCUMENT_FIELDS["payments"]),
            "fx": select_fields(load_csv(public / "fx.csv"), DOCUMENT_FIELDS["fx"]),
        }
        records = {}
        documents = {}
        evidence_ids = set()
        for name in TABLES:
            records[name] = {}
            for row in tables[name]:
                if not valid_record(name, row):
                    raise ValueError(f"Invalid {name} record")
                currencies = (row["source_currency"], row["target_currency"]) if name == "fx" else (row["currency"],)
                if any(currency not in exponents for currency in currencies):
                    raise ValueError(f"Undeclared currency in {name}")
                key = record_id(name, row)
                if not is_id(key) or key in records[name]:
                    raise ValueError(f"Invalid or duplicate key in {name}")
                records[name][key] = row
                if name in ("invoices", "payments"):
                    doc_id = row["doc_id"]
                    if not is_id(doc_id) or doc_id in documents:
                        raise ValueError("Invalid or duplicate document ID")
                    documents[doc_id] = row
                    evidence_key = doc_id
                else:
                    evidence_key = key
                if evidence_key in evidence_ids:
                    raise ValueError("Evidence IDs must be globally unique")
                evidence_ids.add(evidence_key)
        if manifest["record_count"] != len(tables["ledger"]) or manifest["document_count"] != len(documents):
            raise ValueError("Manifest count mismatch")

        truth = load_json(root / "private" / "truth.json")
        issues = truth["issues"]
        if truth.get("episode_id", manifest["episode_id"]) != manifest["episode_id"]:
            raise ValueError("Truth belongs to another episode")
        if not isinstance(issues, list) or len(issues) > 30:
            raise ValueError("Invalid truth issue count")
        issue_ids = set()
        for issue in issues:
            if (
                not is_id(issue["issue_id"]) or issue["issue_id"] in issue_ids
                or issue["type"] not in ISSUE_TYPES or not valid_ids(issue["affected_ids"])
                or not is_currency(issue["currency"]) or type(issue["expected_amount_minor"]) is not int
            ):
                raise ValueError("Invalid truth issue")
            sets = issue["evidence_sets"]
            if not isinstance(sets, list) or not sets or not all(
                valid_ids(items, maximum=10) and evidence_ids.issuperset(items) for items in sets
            ):
                raise ValueError("Truth contains unavailable or invalid evidence")
            issue_ids.add(issue["issue_id"])
        return cls(manifest, policy, tables, records, documents, frozenset(evidence_ids), tuple(issues))

    @classmethod
    def load_v1(cls, public_directory, private_directory):
        public = load_public(str(public_directory))
        private = load_private(str(private_directory))
        truth = private.truth
        if any(truth[key] != public.manifest[key] for key in ("episode_id", "base_currency")):
            raise ValueError("Truth belongs to another episode")
        evidence_ids = frozenset(public.id_space())
        issues = tuple(truth["issues"])
        if len(issues) > 30:
            raise ValueError("Too many issues to submit in one episode")
        if any(
            len(set(issue["affected_ids"])) != 1 or issue["affected_ids"][0] not in public.docs
            or any(not evidence_ids.issuperset(ids) for ids in issue["evidence_sets"])
            for issue in issues
        ):
            raise ValueError("Truth contains unavailable documents or evidence")
        tables = {
            "ledger": public.ledger, "invoices": public.invoices,
            "payments": public.payments, "fx": public.fx_rows,
        }
        records = {
            "ledger": public.rows_by_id, "invoices": public.invoice_by_iid,
            "payments": public.payment_by_pid, "fx": {
                v1_fx_key(row["date"], row["source_currency"], row["target_currency"]): row
                for row in public.fx_rows
            },
        }
        return cls(public.manifest, public.policy, tables, records, public.docs, evidence_ids, issues)


def load_episode(seed, config):
    """Load a saved episode or generate one using the trusted reset inputs."""
    if "episode" in config:
        return Episode.load(config["episode"], config.get("private"))
    generation_config = config if seed is None else config | {"seed": seed}
    with TemporaryDirectory(prefix="audit-reset-") as directory:
        _, public, private = generate_episode(generation_config, directory)
        return Episode.load(public, private)
