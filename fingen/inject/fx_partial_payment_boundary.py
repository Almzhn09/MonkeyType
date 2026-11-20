"""A16: combo fx_partial_payment_boundary — ONE foreign-currency receipt next
to the period boundary whose UTC date differs from its local date carries two
issues on the same document:

  order 1  fx_conversion       the whole entry is converted at the UTC-date
                               rate (Dr bank and the Cr AR allocation, with the
                               §4 remainder rule at that rate);
  order 2  payment_allocation  part of the (already mis-converted) Cr AR line
                               is moved to Cr unapplied.

Amounts are independent: fx = |Dr bank recorded − Dr bank at the local-date
rate| (step 2 does not touch the bank line); allocation = Σ Cr unapplied as
recorded (= Dr bank − Σ Cr AR). The provenance ``order`` of the change records
follows this sequence and every ``cause`` names its step.

Candidate tiers: dedicated baseline payments (tag ``fx_combo``), then other
near-boundary/UTC-edge receipts, then any UTC-edge receipt. With company
offset +00:00 there is no UTC-edge document, and a UTC-date rate can equal
the local-date rate (6-decimal quotes of small rates): then the previous/next
day's rate is used instead. If both adjacent quotes are unchanged, the nearest
different published quote within a week is considered (variant recorded in the cause).
"""

from __future__ import annotations

import datetime as _dt

from . import InjectionContext, InjectionError, register
from ._common import (apply_rescale, can_misallocate, candidates, correct_key_amount, fx_issue,
                      has_utc_edge, misallocate, pick, rescaled, rows_of, sole_entry)
from ..timeutil import utc_date

NAME = "fx_partial_payment_boundary"


def _options(h, pay) -> list[tuple]:
    """Wrong-rate options in preference order: [(variant, rate, date, why)].
    The UTC-date rate first; the adjacent days only as a fallback (company
    offset +00:00, or a UTC-date rate equal to the local-date rate, which
    happens for small rates quoted with 6 decimals)."""
    local = h.local_date(pay.timestamp)
    correct = h.fx_rate(local, pay.currency)
    out = []
    if has_utc_edge(h, pay):
        out.append(("utc_date", utc_date(pay.timestamp), "UTC date"))
    for s in (-1, 1):
        out.append(("adjacent_day", local + _dt.timedelta(days=s),
                    "previous day" if s < 0 else "next day"))
    res = []
    for variant, d, why in out:
        rate = h.fx_rate(d, pay.currency)
        if rate is not None and rate != correct:
            res.append((variant, rate, d, why))
    if not res:
        for distance in range(2, 8):
            for delta in (-distance, distance):
                day = local + _dt.timedelta(days=delta)
                rate = h.fx_rate(day, pay.currency)
                if rate is not None and rate != correct:
                    res.append(("nearby_day", rate, day, "nearby day"))
    return res


def _plan(h, pay, utc_only: bool):
    """First option yielding a valid rescale whose AR line can still be split."""
    if pay.currency == h.base_currency or not can_misallocate(h, pay):
        return None
    for variant, rate, d, why in _options(h, pay):
        if utc_only and variant != "utc_date":
            continue
        plan = rescaled(h, pay, sole_entry(h, pay), rate)
        if plan is not None and any(r.role == "ar" and c >= 2 for r, _d, c in plan):
            return variant, rate, d, why, plan
    return None


@register(NAME)
def inject_fx_partial_payment_boundary(ctx: InjectionContext, count: int) -> list:
    h = ctx.history
    issues = []
    for _ in range(count):
        docs = [d for d in candidates(ctx, "payment") if _plan(h, d, False)]
        utc_ok = {d.doc_id for d in docs if _plan(h, d, True)}
        pay = pick(ctx, docs, lambda d: d.doc_id in utc_ok and "fx_combo" in d.tags,
                   lambda d: d.doc_id in utc_ok and "near_boundary" in d.tags,
                   lambda d: d.doc_id in utc_ok,
                   lambda d: "fx_combo" in d.tags,
                   lambda d: "near_boundary" in d.tags)
        if pay is None:
            raise InjectionError(f"{NAME}: no foreign-currency fully allocated receipt left")
        fx_id, alloc_id = ctx.new_issue_id(), ctx.new_issue_id()
        ctx.claim(pay.doc_id, fx_id)
        entry = sole_entry(h, pay)
        variant, rate, d, why, plan = _plan(h, pay, pay.doc_id in utc_ok)
        local = h.local_date(pay.timestamp)
        correct = correct_key_amount(h, pay)
        cause = (f"fx_conversion[{variant}] (combo {NAME}, "
                 f"step 1/2): payment {pay.payment_id} at {pay.timestamp} (local {local}) "
                 f"converted at the {why} {d} rate {rate} instead of "
                 f"{h.fx_rate(local, pay.currency)}")
        apply_rescale(ctx, plan, issue_id=fx_id, type_="fx_conversion", cause=cause)
        fx = fx_issue(ctx, pay, entry, fx_id, correct)
        alloc = misallocate(ctx, pay, "partial", alloc_id, "payment_allocation",
                            note=f" (combo {NAME}, step 2/2, after the fx step)")
        if not rows_of(h, entry, "unapplied"):
            raise InjectionError(f"{NAME}: allocation step produced no unapplied line")
        issues += [fx, alloc]
    return issues
