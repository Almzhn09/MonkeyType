"""A09: duplicate_invoice — one in-period invoice is posted a second time.

The duplicate is a new entry (fresh entry/row IDs from the episode IdFactory)
with Dr AR / Cr revenue equal to the original posting, dated on a later local
date that is still inside the period, so it never also becomes a
wrong_period error. The original (posting on the ``issued_at`` local date with
the expected amount) is untouched. Invoices that have a same-customer,
same-amount twin (a different, correct invoice; built by the baseline when
``distractors.repeated_amounts > 0``) are preferred as targets.
"""

from __future__ import annotations

from . import InjectionContext, InjectionError, register
from ._common import (can_duplicate, candidates, has_twin, not_reserved_for_others, pick,
                      post_duplicate, spares_foreign)

NAME = "duplicate_invoice"


@register(NAME)
def inject_duplicate_invoice(ctx: InjectionContext, count: int) -> list:
    h = ctx.history
    issues = []
    for _ in range(count):
        docs = [d for d in candidates(ctx, "invoice") if can_duplicate(h, d)]
        spare = spares_foreign(ctx)
        inv = pick(ctx, docs,
                   lambda d: not_reserved_for_others(d, NAME) and has_twin(h, d) and spare(d),
                   lambda d: not_reserved_for_others(d, NAME) and spare(d),
                   lambda d: not_reserved_for_others(d, NAME))
        if inv is None:
            raise InjectionError("duplicate_invoice: no in-period invoice left to duplicate")
        issue_id = ctx.new_issue_id()
        ctx.claim(inv.doc_id, issue_id)
        issues.append(post_duplicate(ctx, inv, issue_id, NAME))
    return issues
