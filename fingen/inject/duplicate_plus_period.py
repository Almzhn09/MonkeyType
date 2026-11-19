"""A17: combo duplicate_plus_period — two DIFFERENT invoices of the same
customer (preferably with equal totals; the baseline builds such
``combo_pair`` invoices): invoice A is posted twice inside the period
(duplicate_invoice), invoice B's only posting is moved past the period
boundary (wrong_period). Change order: A's duplicate first, then B's move.
The two issues are told apart by invoice_id: A has two postings on in-period
dates, B has one posting on the wrong side of the boundary.
"""

from __future__ import annotations

import datetime as _dt
import itertools

from . import InjectionContext, InjectionError, register
from ._common import business_ts, can_duplicate, candidates, move_posting, post_duplicate
from .wrong_period import _posted_on_issue_date

NAME = "duplicate_plus_period"


@register(NAME)
def inject_duplicate_plus_period(ctx: InjectionContext, count: int) -> list:
    h, rng = ctx.history, ctx.rng
    issues = []
    for _ in range(count):
        invs = [d for d in candidates(ctx, "invoice") if _posted_on_issue_date(h, d)]
        pairs = [(a, b) for a, b in itertools.permutations(invs, 2)
                 if a.entity_id == b.entity_id and can_duplicate(h, a)]
        tiers = (
            lambda a, b: "combo_pair" in a.tags and "combo_pair" in b.tags
            and a.total_minor == b.total_minor,
            lambda a, b: a.currency == b.currency and a.total_minor == b.total_minor,
            lambda a, b: True,
        )
        chosen = None
        for pred in tiers:
            tier = sorted((p for p in pairs if pred(*p)), key=lambda p: (p[0].doc_id, p[1].doc_id))
            if tier:
                chosen = rng.choice(tier)
                break
        if chosen is None:
            raise InjectionError(f"{NAME}: no two in-period invoices of one customer left")
        a, b = chosen
        dup_id, wp_id = ctx.new_issue_id(), ctx.new_issue_id()
        ctx.claim(a.doc_id, dup_id)
        ctx.claim(b.doc_id, wp_id)
        dup = post_duplicate(ctx, a, dup_id, "duplicate_invoice",
                             note=f" (combo {NAME}, step 1/2; sibling {b.doc_id})")
        d = h.period_end.date() + _dt.timedelta(days=rng.randint(0, 5))
        cause = (f"wrong_period[late_posting] (combo {NAME}, step 2/2; sibling {a.doc_id}): "
                 f"in-period invoice issued {h.local_date(b.issued_at)} posted after "
                 f"period_end on {d}")
        wp = move_posting(ctx, b, business_ts(ctx, d), wp_id, "wrong_period", cause)
        issues += [dup, wp]
    return issues
