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
offset +00:00 there is no UTC-edge document: the previous day's rate is used
instead (documented in the cause).
"""

from __future__ import annotations

import datetime as _dt

from . import InjectionContext, InjectionError, register
from ._common import (apply_rescale, can_misallocate, correct_key_amount, fx_issue,
                      has_utc_edge, misallocate, pick, rescaled, rows_of, sole_entry)
from ..timeutil import utc_date

NAME = "fx_partial_payment_boundary"


def _wrong_rate(h, pay):
    local = h.local_date(pay.timestamp)
    if has_utc_edge(h, pay):
        d, why = utc_date(pay.timestamp), "UTC date"
    else:
        d, why = local - _dt.timedelta(days=1), "previous day (offset +00:00 has no UTC edge)"
    rate = h.fx_rate(d, pay.currency)
    return (rate, d, why) if rate is not None else None


def _feasible(h, pay) -> bool:
    if pay.currency == h.base_currency or not can_misallocate(h, pay):
        return False
    if h.utc_offset != "+00:00" and not has_utc_edge(h, pay):
        return False
    w = _wrong_rate(h, pay)
    if w is None:
        return False
    plan = rescaled(h, pay, sole_entry(h, pay), w[0])
    # step 2 needs an AR line of at least 2 minor units to split
    return plan is not None and any(r.role == "ar" and c >= 2 for r, _d, c in plan)


@register(NAME)
def inject_fx_partial_payment_boundary(ctx: InjectionContext, count: int) -> list:
    h = ctx.history
    issues = []
    for _ in range(count):
        docs = [d for d in ctx.candidates("payment") if _feasible(h, d)]
        pay = pick(ctx, docs, lambda d: "fx_combo" in d.tags,
                   lambda d: "near_boundary" in d.tags and has_utc_edge(h, d),
                   lambda d: has_utc_edge(h, d))
        if pay is None:
            raise InjectionError(f"{NAME}: no foreign-currency fully allocated receipt left")
        fx_id, alloc_id = ctx.new_issue_id(), ctx.new_issue_id()
        ctx.claim(pay.doc_id, fx_id)
        entry = sole_entry(h, pay)
        rate, d, why = _wrong_rate(h, pay)
        local = h.local_date(pay.timestamp)
        correct = correct_key_amount(h, pay)
        plan = rescaled(h, pay, entry, rate)
        cause = (f"fx_conversion[{'utc_date' if 'UTC' in why else 'adjacent_day'}] (combo {NAME}, "
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
