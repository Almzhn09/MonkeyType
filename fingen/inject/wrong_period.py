"""A12: wrong_period — an invoice's posting lands on the other side of the
period boundary than its ``issued_at`` local date (amounts unchanged, so the
FX rate stays the event-date rate and no fx error appears):

* ``late_posting``  — in-period invoice posted after ``period_end`` (or, for an
                      invoice from the first days, before ``period_start``);
* ``early_posting`` — out-of-period invoice (issued in the extract window
                      before/after the period) posted inside the period;
* ``utc_trap``      — ``issued_at`` written in +00:00 whose UTC date is on the
                      other side of the boundary than the local date; the
                      entry is dated by the UTC wall clock (UTC date) in the
                      company offset.

Variants rotate per issue (random start); a variant without candidates falls
back to the next one. Every new timestamp stays inside the extract window.
"""

from __future__ import annotations

import datetime as _dt

from . import InjectionContext, InjectionError, register
from ._common import (business_ts, move_posting, not_reserved_for_others, period_first_last,
                      pick, sole_entry)
from ..timeutil import as_ts, format_ts, tzinfo, utc_date

NAME = "wrong_period"
VARIANTS = ("late_posting", "early_posting", "utc_trap")


def _posted_on_issue_date(h, inv) -> bool:
    entry = sole_entry(h, inv)
    if entry is None:
        return False
    rows = h.entry_rows(entry.entry_id)
    return h.local_date(rows[0].timestamp) == h.local_date(inv.issued_at)


def utc_trap_ts(h, inv) -> str | None:
    """UTC wall-clock of ``issued_at`` stamped with the company offset, if
    that crosses the boundary (None otherwise)."""
    if h.utc_offset == "+00:00" or not inv.issued_at.endswith("+00:00"):
        return None
    if h.local_date(inv.issued_at) == utc_date(inv.issued_at):
        return None
    wall = as_ts(inv.issued_at).replace(tzinfo=tzinfo(h.utc_offset))
    ts = format_ts(wall)
    if h.in_period(ts) == h.in_period(inv.issued_at) or not h.in_extract(ts):
        return None
    return ts


def _days_to_boundary(h, inv) -> int:
    first, last = period_first_last(h)
    d = h.local_date(inv.issued_at)
    return min(abs((d - first).days), abs((d - last).days))


def choose(ctx: InjectionContext, variant: str):
    """(invoice, new_ts, description) or None."""
    h, rng = ctx.history, ctx.rng
    first, last = period_first_last(h)
    end = h.period_end.date()
    own = lambda d: not_reserved_for_others(d, NAME)  # noqa: E731
    if variant == "utc_trap":
        docs = [d for d in ctx.candidates("invoice", in_period=False)
                if _posted_on_issue_date(h, d) and utc_trap_ts(h, d)]
        inv = pick(ctx, docs, lambda d: own(d) and "utc_trap" in d.tags, own)
        if inv is None:
            return None
        ts = utc_trap_ts(h, inv)
        return inv, ts, (f"issued_at {inv.issued_at} (local date {h.local_date(inv.issued_at)}) "
                         f"posted by its UTC date {utc_date(inv.issued_at)}")
    if variant == "late_posting":
        docs = [d for d in ctx.candidates("invoice") if _posted_on_issue_date(h, d)]
        inv = pick(ctx, docs, lambda d: own(d) and "utc_trap" not in d.tags
                   and h.local_date(d.issued_at) >= last - _dt.timedelta(days=6),
                   lambda d: own(d) and "utc_trap" not in d.tags, own)
        if inv is None:
            return None
        issued = h.local_date(inv.issued_at)
        if (issued - first).days <= 2 and rng.random() < 0.5:
            d = first - _dt.timedelta(days=rng.randint(1, 4))
            why = "posted before period_start"
        else:
            d = end + _dt.timedelta(days=rng.randint(0, 5))
            why = "posted after period_end"
        return inv, business_ts(ctx, d), f"in-period invoice issued {issued} {why} on {d}"
    if variant == "early_posting":
        docs = [d for d in ctx.candidates("invoice", in_period=False)
                if not h.in_period(d.issued_at) and _posted_on_issue_date(h, d)]
        inv = pick(ctx, docs, lambda d: own(d) and "utc_trap" not in d.tags
                   and _days_to_boundary(h, d) <= 7,
                   lambda d: own(d) and "utc_trap" not in d.tags, own)
        if inv is None:
            return None
        issued = h.local_date(inv.issued_at)
        if issued >= end:
            d = last - _dt.timedelta(days=rng.randint(0, 3))
        else:
            d = first + _dt.timedelta(days=rng.randint(0, 3))
        return inv, business_ts(ctx, d), (f"out-of-period invoice issued {issued} posted "
                                           f"inside the period on {d}")
    raise InjectionError(f"unknown wrong_period variant {variant!r}")


@register(NAME)
def inject_wrong_period(ctx: InjectionContext, count: int) -> list:
    start = ctx.rng.randrange(len(VARIANTS))
    issues = []
    for i in range(count):
        got = None
        for k in range(len(VARIANTS)):
            variant = VARIANTS[(start + i + k) % len(VARIANTS)]
            got = choose(ctx, variant)
            if got is not None:
                break
        if got is None:
            raise InjectionError("wrong_period: no invoice left to move across the boundary")
        inv, ts, why = got
        issue_id = ctx.new_issue_id()
        ctx.claim(inv.doc_id, issue_id)
        cause = f"{NAME}[{variant}]: {why}"
        issues.append(move_posting(ctx, inv, ts, issue_id, NAME, cause))
    return issues
