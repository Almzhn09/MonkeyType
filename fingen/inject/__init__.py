"""Error-injection hook API (for A09-A12 and the combos A16-A17).

No error type is implemented here yet. To add one, create a module in this
package (e.g. ``fingen/inject/duplicate_invoice.py``) and register a function::

    from fingen.inject import InjectionContext, Issue, register

    @register("duplicate_invoice")
    def inject_duplicate(ctx: InjectionContext, count: int) -> list[Issue]:
        issues = []
        for inv in ctx.candidates("invoice")[:count]:          # deterministic order
            ...
            issue_id = ctx.new_issue_id()
            entry = ctx.new_entry("invoice", inv.doc_id)
            ar = ctx.add_row(entry, ts, "ar", amount, 0, entity_id=..., invoice_id=...,
                             issue_id=issue_id, cause="invoice posted twice")
            ...
            ctx.claim(inv.doc_id, issue_id)
            issues.append(ctx.make_issue(issue_id, "duplicate_invoice", inv.doc_id,
                                         amount, [[inv.doc_id, ar1, ar2], [...]]))
        return issues

Every module in this package is imported (sorted by name) before injection,
so registration happens automatically.

Rules the injectors must follow (CONTRACT §2, §5, §6):

* Mutate the ledger **only** through ``ctx.add_row`` / ``ctx.modify_row`` /
  ``ctx.delete_row``; each call appends a provenance change record
  ``{issue_id, type, order, op, target, before, after, cause}`` with full
  public rows, so reversing ``changes`` restores the baseline ledger.
  Documents (invoices/payments) and the FX table are never modified.
* New IDs come from ``ctx.history.ids`` (same distribution as baseline IDs).
* Only touch documents whose event or entry lies in the period
  (``history.in_period``). Pick candidates with ``ctx.candidates`` (which skips
  documents tagged ``distractor`` and docs already claimed), use
  ``ctx.rng`` for randomness, and ``ctx.claim`` the doc you use.
* Every entry must stay balanced with one timestamp (checked after ``apply``).
* Reuse ``fingen.history.invoice_lines`` / ``payment_lines`` / ``post_document``
  (with ``rate=`` / ``rate_date=`` / ``posting_ts=``) to compute amounts.
* Useful private document tags: ``in_period``, ``near_boundary``,
  ``utc_edge`` (UTC date != local date), ``partial`` (partial payment of an
  invoice), ``split``, ``multi_ref``, ``advance``, ``refund``, ``unpaid``,
  ``repeated_amount``, ``distractor``. ``Payment.fully_allocated`` tells whether
  a payment is eligible for ``payment_allocation`` (Σrefs = amount).
* A combo injector (``fx_partial_payment_boundary``, ``duplicate_plus_period``)
  returns two ``Issue`` objects whose ``type`` values are base error types.
* If a distractor document is consumed by an injection, remove/adjust its
  record in ``history.distractors``.
"""

from __future__ import annotations

import importlib
import pkgutil
import random
from dataclasses import dataclass
from typing import Callable

from ..config import COMBO_TYPES, ERROR_TYPES
from ..history import LEDGER_COLUMNS, Entry, History, LedgerRow
from ..rng import EpisodeRng

# Combos run first so they get the scarce boundary candidates.
INJECTION_ORDER: tuple[str, ...] = COMBO_TYPES + ERROR_TYPES

TOLERANCE_MINOR: dict[str, int] = {
    "duplicate_invoice": 0, "payment_allocation": 0, "fx_conversion": 1, "wrong_period": 0,
}

OPS = ("add_row", "modify_row", "delete_row")


class InjectionError(RuntimeError):
    """An injector produced an invalid result or could not find candidates."""


def canonical_ids(ids) -> list[str]:
    """CONTRACT §5: strip, upper, de-duplicate, sort."""
    return sorted({str(i).strip().upper() for i in ids})


@dataclass
class Issue:
    """One truth.json issue (CONTRACT §6)."""

    issue_id: str
    type: str                      # one of ERROR_TYPES
    affected_ids: list[str]        # exactly [doc_id]
    expected_amount_minor: int     # base currency, minor units, >= 0
    currency: str                  # base currency
    tolerance_minor: int
    evidence_sets: list[list[str]]

    def to_dict(self) -> dict:
        return {"issue_id": self.issue_id, "type": self.type,
                "affected_ids": canonical_ids(self.affected_ids),
                "expected_amount_minor": self.expected_amount_minor,
                "currency": self.currency, "tolerance_minor": self.tolerance_minor,
                "evidence_sets": [list(s) for s in self.evidence_sets]}


Injector = Callable[["InjectionContext", int], "list[Issue]"]
REGISTRY: dict[str, Injector] = {}


def register(name: str) -> Callable[[Injector], Injector]:
    """Decorator: register an injector for an error type or combo name."""
    if name not in INJECTION_ORDER:
        raise ValueError(f"unknown injection name {name!r}")

    def deco(fn: Injector) -> Injector:
        if name in REGISTRY and REGISTRY[name] is not fn:
            raise ValueError(f"injector {name!r} already registered")
        REGISTRY[name] = fn
        return fn

    return deco


class InjectionContext:
    """Handle given to an injector: the mutable history, the resolved config,
    a dedicated ``random.Random`` sub-stream (``inject:<name>``) and the
    change-recording mutation primitives."""

    def __init__(self, history: History, config: dict, rng: random.Random, name: str) -> None:
        self.history = history
        self.config = config
        self.rng = rng
        self.name = name

    # ---- ids / issues ------------------------------------------------------
    def new_issue_id(self) -> str:
        return self.history.ids.new("issue")

    def make_issue(self, issue_id: str, type_: str, doc_id: str, expected_amount_minor: int,
                   evidence_sets: list[list[str]]) -> Issue:
        if type_ not in ERROR_TYPES:
            raise InjectionError(f"unknown issue type {type_!r}")
        if isinstance(expected_amount_minor, bool) or not isinstance(expected_amount_minor, int) \
                or expected_amount_minor < 0:
            raise InjectionError("expected_amount_minor must be a non-negative int")
        return Issue(issue_id, type_, [doc_id], expected_amount_minor,
                     self.history.base_currency, TOLERANCE_MINOR[type_],
                     [list(s) for s in evidence_sets])

    # ---- candidate selection ---------------------------------------------------
    def candidates(self, kind: str | None = None, *, in_period: bool = True,
                   require_tags=(), exclude_tags=("distractor",)) -> list:
        """Unclaimed documents sorted by doc_id. ``kind``: "invoice"/"payment"/None."""
        h = self.history
        docs = h.documents() if kind is None else \
            list((h.invoices if kind == "invoice" else h.payments).values())
        out = []
        for d in docs:
            if d.doc_id in h.claimed:
                continue
            if in_period and not h.in_period(d.event_ts):
                continue
            if any(t not in d.tags for t in require_tags) or any(t in d.tags for t in exclude_tags):
                continue
            out.append(d)
        return sorted(out, key=lambda d: d.doc_id)

    def claim(self, doc_id: str, issue_id: str) -> None:
        if doc_id in self.history.claimed:
            raise InjectionError(f"document {doc_id} already used by another issue")
        self.history.claimed[doc_id] = issue_id

    # ---- recorded mutations ----------------------------------------------------
    def _record(self, issue_id: str, op: str, target: str, before, after, cause: str,
                type_: str | None) -> dict:
        change = {"issue_id": issue_id, "type": type_ or self.name,
                  "order": len(self.history.changes), "op": op, "target": target,
                  "before": before, "after": after, "cause": cause}
        self.history.changes.append(change)
        return change

    def new_entry(self, kind: str, doc_id: str | None, meta: dict | None = None) -> Entry:
        """New (empty) entry with a fresh entry_id; rows are added with add_row."""
        return self.history.new_entry(kind, doc_id, meta)

    def add_row(self, entry: Entry, timestamp: str, role: str, debit: int, credit: int, *,
                issue_id: str, cause: str, type_: str | None = None, entity_id=None,
                invoice_id=None, payment_id=None, account: str | None = None) -> LedgerRow:
        """Add a row (fresh row_id) to ``entry``; records op ``add_row``."""
        row = self.history.make_row(entry, timestamp, role, debit, credit, entity_id=entity_id,
                                    invoice_id=invoice_id, payment_id=payment_id,
                                    account=account, meta={"injected": issue_id})
        self._record(issue_id, "add_row", row.row_id, None, row.to_public(), cause, type_)
        return row

    def modify_row(self, row_id: str, fields: dict, *, issue_id: str, cause: str,
                   type_: str | None = None, role: str | None = None) -> LedgerRow:
        """Change public fields of a row (not ``row_id``); records ``modify_row``.
        Changing ``entry_id`` moves the row; pass ``role`` if the account role changes."""
        h = self.history
        row = h.rows[row_id]
        bad = [k for k in fields if k not in LEDGER_COLUMNS or k == "row_id"]
        if bad:
            raise InjectionError(f"cannot modify fields {bad}")
        before = row.to_public()
        if "entry_id" in fields and fields["entry_id"] != row.entry_id:
            if fields["entry_id"] not in h.entries:
                raise InjectionError("target entry does not exist; use new_entry()")
            old = h.entries[row.entry_id]
            old.row_ids.remove(row_id)
            if not old.row_ids:
                del h.entries[old.entry_id]
            h.entries[fields["entry_id"]].row_ids.append(row_id)
        for k, v in fields.items():
            setattr(row, k, v)
        if role is not None:
            row.role = role
        row.meta["injected"] = issue_id
        self._record(issue_id, "modify_row", row_id, before, row.to_public(), cause, type_)
        return row

    def delete_row(self, row_id: str, *, issue_id: str, cause: str,
                   type_: str | None = None) -> LedgerRow:
        row = self.history.remove_row(row_id)
        self._record(issue_id, "delete_row", row_id, row.to_public(), None, cause, type_)
        return row


def load_injectors() -> None:
    """Import every module of this package (sorted) so @register runs."""
    for mod in sorted(m.name for m in pkgutil.iter_modules(__path__)):
        importlib.import_module(f"{__name__}.{mod}")


def check_ledger(history: History) -> None:
    """Every entry balanced, one timestamp per entry, one positive side per row."""
    for entry in history.entries.values():
        rows = history.entry_rows(entry.entry_id)
        if not rows:
            continue
        if sum(r.debit_minor for r in rows) != sum(r.credit_minor for r in rows):
            raise InjectionError(f"entry {entry.entry_id} unbalanced")
        if len({r.timestamp for r in rows}) != 1:
            raise InjectionError(f"entry {entry.entry_id} has several timestamps")
        for r in rows:
            if (r.debit_minor > 0) == (r.credit_minor > 0) or min(r.debit_minor, r.credit_minor) < 0:
                raise InjectionError(f"row {r.row_id} must have exactly one positive side")


def apply(history: History, config: dict, rng: EpisodeRng) -> list[Issue]:
    """Run the configured injectors on ``history`` (in place).

    For each name in ``INJECTION_ORDER`` with a non-zero count in
    ``config["combos"]`` / ``config["errors"]``, call the registered injector
    with ``InjectionContext(history, config, rng.stream(f"inject:{name}"), name)``
    and the count. Returns all issues sorted by ``issue_id``; change records
    accumulate in ``history.changes``. Raises ``NotImplementedError`` when a
    configured type has no registered injector.
    """
    load_injectors()
    issues: list[Issue] = []
    for name in INJECTION_ORDER:
        group = config["combos"] if name in COMBO_TYPES else config["errors"]
        count = group.get(name, 0)
        if not count:
            continue
        fn = REGISTRY.get(name)
        if fn is None:
            raise NotImplementedError(f"no injector registered for {name!r}")
        ctx = InjectionContext(history, config, rng.stream(f"inject:{name}"), name)
        produced = list(fn(ctx, count))
        expected = count * (2 if name in COMBO_TYPES else 1)
        if len(produced) != expected:
            raise InjectionError(f"{name}: expected {expected} issues, got {len(produced)}")
        issues.extend(produced)
    if len({i.issue_id for i in issues}) != len(issues):
        raise InjectionError("duplicate issue ids")
    check_ledger(history)
    return sorted(issues, key=lambda i: i.issue_id)
