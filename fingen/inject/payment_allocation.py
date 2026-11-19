"""A10: payment_allocation — a fully allocated receipt (Σrefs = amount) whose
Cr AR allocation is missing entirely (``missing``) or partly (``partial``:
one AR line applied short); the unallocated part sits on Cr
unapplied with ``invoice_id`` null and the payment_id kept, so the entry stays
balanced.

Variants alternate per issue (``missing``/``partial``, random start). Payments
of split settlements (several payments of one invoice) are only used when no
other candidate is left; either way only the chosen payment's rows change, so
the other payments of the same invoice remain correct.
"""

from __future__ import annotations

from . import InjectionContext, InjectionError, register
from ._common import (can_misallocate, candidates, misallocate, not_reserved_for_others, pick,
                      spares_foreign)

NAME = "payment_allocation"
VARIANTS = ("missing", "partial")


@register(NAME)
def inject_payment_allocation(ctx: InjectionContext, count: int) -> list:
    h = ctx.history
    start = ctx.rng.randrange(len(VARIANTS))
    issues = []
    for i in range(count):
        variant = VARIANTS[(start + i) % len(VARIANTS)]
        docs = [d for d in candidates(ctx, "payment") if can_misallocate(h, d)]
        spare = spares_foreign(ctx)
        pay = pick(ctx, docs,
                   lambda d: not_reserved_for_others(d, NAME) and spare(d)
                   and not {"split", "partial"} & set(d.tags),
                   lambda d: not_reserved_for_others(d, NAME) and spare(d),
                   lambda d: not_reserved_for_others(d, NAME))
        if pay is None:
            raise InjectionError("payment_allocation: no fully allocated in-period payment left")
        issue_id = ctx.new_issue_id()
        ctx.claim(pay.doc_id, issue_id)
        issues.append(misallocate(ctx, pay, variant, issue_id, NAME))
    return issues
