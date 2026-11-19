"""Shared helpers for the injectors (A09-A12, A16-A17).

Each helper mutates the ledger only through ``InjectionContext`` primitives,
so every change is recorded in provenance. Amounts are recomputed with the
baseline posting rules (``invoice_lines`` / ``payment_lines``).
"""

from __future__ import annotations

import datetime as _dt
from decimal import Decimal

from ..history import History, Invoice, LedgerRow, Payment, invoice_lines, payment_lines
from ..money import MAX_RATE_DECIMALS
from ..timeutil import format_ts, local_datetime, utc_date
from . import InjectionContext, InjectionError

# Private tags marking candidates that the builder created for one specific
# injection; other injectors only take them when nothing else is left.
RESERVED_TAGS: dict[str, str] = {
    "utc_trap": "wrong_period",                # invoice written in +00:00 across a boundary
    "fx_combo": "fx_partial_payment_boundary",  # foreign payment, local date != UTC date
    "combo_pair": "duplicate_plus_period",      # two equal invoices of one customer
}


# --------------------------------------------------------------------------- selection

def not_reserved_for_others(doc, owner: str) -> bool:
    return all(t not in doc.tags or RESERVED_TAGS[t] == owner for t in RESERVED_TAGS)


def pick(ctx: InjectionContext, docs: list, *tiers):
    """Random choice (``ctx.rng``) among the first non-empty tier; a tier is a
    predicate over ``docs``. ``docs`` itself is the last tier. None if empty."""
    docs = sorted(docs, key=lambda d: d.doc_id)
    for pred in tiers + (lambda d: True,):
        tier = [d for d in docs if pred(d)]
        if tier:
            return ctx.rng.choice(tier)
    return None


def spares_foreign(ctx: InjectionContext):
    """Predicate for non-FX injectors: when fx_conversion issues are still to
    come, leave the (scarce) foreign-currency documents to them."""
    if not ctx.config["errors"].get("fx_conversion"):
        return lambda d: True
    return lambda d: d.currency == ctx.history.base_currency


def sole_entry(h: History, doc):
    """The document's single posting entry, or None (already touched / odd)."""
    entries = h.entries_of_doc(doc.doc_id)
    return entries[0] if len(entries) == 1 else None


def rows_of(h: History, entry, role: str | None = None, side: str | None = None) -> list[LedgerRow]:
    rows = h.entry_rows(entry.entry_id)
    if role is not None:
        rows = [r for r in rows if r.role == role]
    if side == "dr":
        rows = [r for r in rows if r.debit_minor > 0]
    elif side == "cr":
        rows = [r for r in rows if r.credit_minor > 0]
    return rows


def one(rows: list[LedgerRow]) -> LedgerRow:
    if len(rows) != 1:
        raise InjectionError(f"expected exactly one row, got {len(rows)}")
    return rows[0]


# --------------------------------------------------------------------------- time

def local_d(h: History, ts) -> _dt.date:
    return h.local_date(ts)


def period_first_last(h: History) -> tuple[_dt.date, _dt.date]:
    return h.period_start.date(), h.period_end.date() - _dt.timedelta(days=1)


def business_ts(ctx: InjectionContext, d: _dt.date) -> str:
    """Business-hours instant on local date ``d`` in the company offset."""
    rng = ctx.rng
    return format_ts(local_datetime(d, rng.randint(8 * 60, 20 * 60 - 1), ctx.history.utc_offset,
                                    rng.randint(0, 59)))


def has_utc_edge(h: History, doc) -> bool:
    return h.local_date(doc.event_ts) != utc_date(doc.event_ts)


# --------------------------------------------------------------------------- FX

def lines_for(h: History, doc, rate: str | None = None) -> list[tuple]:
    if isinstance(doc, Invoice):
        return invoice_lines(h, doc, rate=rate)
    return payment_lines(h, doc, rate=rate)


def key_role(doc) -> str:
    """Row whose amount carries the fx_conversion discrepancy (CONTRACT §5)."""
    return "ar" if isinstance(doc, Invoice) else "bank"


def rescaled(h: History, doc, entry, rate: str) -> list[tuple[LedgerRow, int, int]] | None:
    """New (row, debit, credit) for every row of ``entry`` when the whole
    document is converted at ``rate``; None if the shape would break (a zero
    line) or nothing would change on the key row."""
    rows = h.entry_rows(entry.entry_id)
    lines = lines_for(h, doc, rate)
    if len(rows) != len(lines):
        return None
    out = []
    for row, (role, debit, credit, invoice_id, payment_id) in zip(rows, lines):
        if row.role != role or row.invoice_id != invoice_id or row.payment_id != payment_id:
            return None
        if (debit > 0) == (credit > 0):
            return None
        out.append((row, debit, credit))
    kr = key_role(doc)
    if all(r.debit_minor + r.credit_minor == d + c for r, d, c in out if r.role == kr):
        return None
    return out


def apply_rescale(ctx: InjectionContext, plan, *, issue_id: str, type_: str, cause: str) -> None:
    for row, debit, credit in plan:
        if (row.debit_minor, row.credit_minor) != (debit, credit):
            ctx.modify_row(row.row_id, {"debit_minor": debit, "credit_minor": credit},
                           issue_id=issue_id, type_=type_, cause=cause)


def typo_rates(h: History, correct: str, currency: str, day: _dt.date) -> list[tuple[str, str]]:
    """Plausible wrong rates for ``correct``: two adjacent digits transposed,
    one digit mistyped, or another foreign currency's rate of the same day.
    Returns [(rate, label)], valid (positive, <= 6 decimals), all != correct."""
    out: list[tuple[str, str]] = []
    digits = [i for i, ch in enumerate(correct) if ch.isdigit()]
    for a, b in zip(digits, digits[1:]):
        if b == a + 1 and correct[a] != correct[b]:
            s = correct[:a] + correct[b] + correct[a] + correct[b + 1:]
            out.append((s, f"digits transposed ({correct} -> {s})"))
    for i in digits:
        for delta in (1, -1):
            nd = int(correct[i]) + delta
            if 0 <= nd <= 9:
                s = correct[:i] + str(nd) + correct[i + 1:]
                out.append((s, f"digit mistyped ({correct} -> {s})"))
    for other in h.foreign_currencies:
        if other != currency:
            r = h.fx_rate(day, other)
            if r is not None:
                out.append((r, f"rate of {other} for the same day used ({r})"))
    valid, seen = [], {correct}
    for s, label in out:
        try:
            d = Decimal(s)
        except ArithmeticError:
            continue
        if s in seen or d <= 0 or -d.as_tuple().exponent > MAX_RATE_DECIMALS:
            continue
        # keep typos plausible: within a factor of 3 of the correct rate
        if not Decimal(correct) / 3 <= d <= Decimal(correct) * 3:
            continue
        seen.add(s)
        valid.append((s, label))
    return valid


# --------------------------------------------------------------------------- shared mutations

def can_duplicate(h: History, inv: Invoice) -> bool:
    """In-period invoice with its single baseline posting and a later local
    date still inside the period (the duplicate must not cross the boundary)."""
    _first, last = period_first_last(h)
    entry = sole_entry(h, inv)
    if entry is None or not h.in_period(inv.issued_at):
        return False
    rows = h.entry_rows(entry.entry_id)
    return h.local_date(inv.issued_at) < last and \
        h.local_date(rows[0].timestamp) == h.local_date(inv.issued_at)


def has_twin(h: History, inv: Invoice) -> bool:
    return any(o.doc_id != inv.doc_id and o.entity_id == inv.entity_id and
               o.currency == inv.currency and o.total_minor == inv.total_minor
               for o in h.invoices.values())


def post_duplicate(ctx: InjectionContext, inv: Invoice, issue_id: str, type_: str,
                   note: str = "") -> object:
    """Post ``inv`` a second time (new entry, same amounts) on a later local
    date inside the period. Returns the Issue."""
    h = ctx.history
    entry = sole_entry(h, inv)
    ar0, rev0 = one(rows_of(h, entry, "ar", "dr")), one(rows_of(h, entry, "revenue", "cr"))
    issued = h.local_date(inv.issued_at)
    _first, last = period_first_last(h)
    d = issued + _dt.timedelta(days=ctx.rng.randint(1, min(12, (last - issued).days)))
    ts = business_ts(ctx, d)
    if not h.in_period(ts) or h.local_date(ts) <= issued:
        raise InjectionError("duplicate posting would leave the period")
    cause = (f"{type_}[repost]{note}: invoice {inv.invoice_id} (issued {issued}) posted a second "
             f"time on {d} as a new entry with the same amounts")
    dup = ctx.new_entry("invoice", inv.doc_id, {"injected": issue_id})
    ar1 = ctx.add_row(dup, ts, "ar", ar0.debit_minor, 0, issue_id=issue_id, type_=type_,
                      cause=cause, entity_id=inv.entity_id, invoice_id=inv.invoice_id)
    rev1 = ctx.add_row(dup, ts, "revenue", 0, rev0.credit_minor, issue_id=issue_id, type_=type_,
                       cause=cause, entity_id=inv.entity_id, invoice_id=inv.invoice_id)
    return ctx.make_issue(issue_id, "duplicate_invoice", inv.doc_id, ar1.debit_minor,
                          [[inv.doc_id, ar0.row_id, ar1.row_id],
                           [inv.doc_id, rev0.row_id, rev1.row_id]])


def move_posting(ctx: InjectionContext, inv: Invoice, new_ts: str, issue_id: str, type_: str,
                 cause: str):
    """Re-date the invoice's entry (all rows) to ``new_ts``; amounts unchanged.
    The new timestamp must be on the other side of the period boundary than
    ``issued_at`` and inside the extract window."""
    h = ctx.history
    entry = sole_entry(h, inv)
    if h.in_period(new_ts) == h.in_period(inv.issued_at) or not h.in_extract(new_ts):
        raise InjectionError("wrong_period posting must cross the boundary inside the window")
    ts = h.to_local_ts(new_ts)
    for row in list(h.entry_rows(entry.entry_id)):
        ctx.modify_row(row.row_id, {"timestamp": ts}, issue_id=issue_id, type_=type_, cause=cause)
    ar = one(rows_of(h, entry, "ar", "dr"))
    rev = one(rows_of(h, entry, "revenue", "cr"))
    return ctx.make_issue(issue_id, "wrong_period", inv.doc_id, ar.debit_minor,
                          [[inv.doc_id, ar.row_id], [inv.doc_id, rev.row_id]])


def can_misallocate(h: History, pay) -> bool:
    if not isinstance(pay, Payment) or not pay.fully_allocated or not h.in_period(pay.timestamp):
        return False
    entry = sole_entry(h, pay)
    return entry is not None and bool(rows_of(h, entry, "ar", "cr")) and \
        not rows_of(h, entry, "unapplied")


def misallocate(ctx: InjectionContext, pay: Payment, variant: str, issue_id: str, type_: str,
                note: str = ""):
    """Move all (``missing``) or part (``partial``) of the payment's Cr AR
    allocation to Cr unapplied (invoice_id null, payment_id kept). The entry
    stays balanced. Returns the Issue (amount = Σ Cr unapplied)."""
    h = ctx.history
    rng = ctx.rng
    entry = sole_entry(h, pay)
    ar_rows = rows_of(h, entry, "ar", "cr")
    un_acct = h.account("unapplied")
    to_unapplied = {"account": un_acct, "invoice_id": None}
    if variant == "missing":
        total = sum(r.credit_minor for r in ar_rows)
        cause = (f"{type_}[missing]{note}: allocation of payment {pay.payment_id} to "
                 f"{', '.join(r.invoice_id for r in ar_rows)} not applied; whole amount "
                 f"credited to unapplied")
        first, rest = ar_rows[0], ar_rows[1:]
        for r in rest:
            ctx.delete_row(r.row_id, issue_id=issue_id, type_=type_, cause=cause)
        ctx.modify_row(first.row_id, {**to_unapplied, "credit_minor": total}, role="unapplied",
                       issue_id=issue_id, type_=type_, cause=cause)
    elif variant == "partial":
        if len(ar_rows) >= 2 and rng.random() < 0.5:
            r = rng.choice(ar_rows)
            cause = (f"{type_}[partial/ref_dropped]{note}: payment {pay.payment_id} ref to "
                     f"{r.invoice_id} not applied; its part credited to unapplied")
            ctx.modify_row(r.row_id, dict(to_unapplied), role="unapplied",
                           issue_id=issue_id, type_=type_, cause=cause)
        else:
            usable = [r for r in ar_rows if r.credit_minor >= 2]
            if not usable:
                raise InjectionError(f"payment {pay.doc_id}: AR lines too small to split")
            r = rng.choice(usable)
            x = r.credit_minor * rng.randint(15, 85) // 100
            x = max(1, min(r.credit_minor - 1, x))
            cause = (f"{type_}[partial/short_applied]{note}: payment {pay.payment_id} applied "
                     f"only {r.credit_minor - x} of {r.credit_minor} to {r.invoice_id}; "
                     f"rest {x} credited to unapplied")
            ctx.modify_row(r.row_id, {"credit_minor": r.credit_minor - x}, issue_id=issue_id,
                           type_=type_, cause=cause)
            ctx.add_row(entry, r.timestamp, "unapplied", 0, x, issue_id=issue_id, type_=type_,
                        cause=cause, entity_id=pay.entity_id, payment_id=pay.payment_id)
    else:
        raise InjectionError(f"unknown payment_allocation variant {variant!r}")
    un = rows_of(h, entry, "unapplied", "cr")
    bank = one(rows_of(h, entry, "bank", "dr"))
    ar_left = rows_of(h, entry, "ar", "cr")
    amount = sum(r.credit_minor for r in un)
    if amount != bank.debit_minor - sum(r.credit_minor for r in ar_left) or amount <= 0:
        raise InjectionError("payment_allocation produced an inconsistent entry")
    sets = [[pay.doc_id] + [r.row_id for r in un]]
    alt = [pay.doc_id, bank.row_id] + [r.row_id for r in ar_left]
    if len(alt) <= 10:
        sets.append(alt)
    return ctx.make_issue(issue_id, "payment_allocation", pay.doc_id, amount, sets)


def fx_issue(ctx: InjectionContext, doc, entry, issue_id: str, correct_amount: int):
    """Issue for a rescaled entry: |recorded − correct| on Dr AR / Dr bank."""
    h = ctx.history
    key = h.fx_key(h.local_date(doc.event_ts), doc.currency)
    if isinstance(doc, Invoice):
        ar = one(rows_of(h, entry, "ar", "dr"))
        rev = one(rows_of(h, entry, "revenue", "cr"))
        amount = abs(ar.debit_minor - correct_amount)
        sets = [[doc.doc_id, ar.row_id, key], [doc.doc_id, rev.row_id, key]]
    else:
        bank = one(rows_of(h, entry, "bank", "dr"))
        amount = abs(bank.debit_minor - correct_amount)
        sets = [[doc.doc_id, bank.row_id, key]]
    if amount <= 0:
        raise InjectionError("fx_conversion without a discrepancy")
    return ctx.make_issue(issue_id, "fx_conversion", doc.doc_id, amount, sets)


def correct_key_amount(h: History, doc) -> int:
    role = key_role(doc)
    return next(d + c for r, d, c, _i, _p in lines_for(h, doc) if r == role)
