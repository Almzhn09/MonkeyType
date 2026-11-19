"""A11: fx_conversion — a foreign-currency invoice or receipt whose WHOLE
entry is converted at a wrong rate:

* ``utc_date``     — the rate of the event's UTC date (documents whose UTC date
                     differs from the company-local date);
* ``adjacent_day`` — the rate of the previous or next local day;
* ``bad_rate``     — a typo of the correct rate (digits transposed / one digit
                     mistyped) or another currency's rate of the same day.

Variants rotate per issue (random start); when a variant has no usable
candidate the next one is tried. Payments with refs are re-allocated with the
same §4 rules at the wrong rate (``payment_lines(rate=...)``), so the entry
never also shows an allocation error. A candidate/rate whose key amount
(Dr AR / Dr bank) would not change is skipped (expected must be > 0).
"""

from __future__ import annotations

import datetime as _dt

from . import InjectionContext, InjectionError, register
from ._common import (apply_rescale, correct_key_amount, fx_issue, has_utc_edge,
                      not_reserved_for_others, pick, rescaled, sole_entry, typo_rates)
from ..history import Invoice, Payment
from ..timeutil import utc_date

NAME = "fx_conversion"
VARIANTS = ("utc_date", "adjacent_day", "bad_rate")


def wrong_rates(h, doc, variant: str, rng=None) -> list[tuple[str, str]]:
    """[(rate, description)] for ``variant`` (may be empty). With ``rng`` the
    options are shuffled; without it (feasibility probing) no draws are made."""
    local = h.local_date(doc.event_ts)
    cur = doc.currency
    correct = h.fx_rate(local, cur)
    out = []
    if variant == "utc_date":
        if has_utc_edge(h, doc):
            u = utc_date(doc.event_ts)
            r = h.fx_rate(u, cur)
            if r is not None:
                out.append((r, f"rate of the UTC date {u} ({r}) used instead of local date "
                               f"{local} ({correct})"))
    elif variant == "adjacent_day":
        steps = [-1, 1]
        if rng is not None:
            rng.shuffle(steps)
        for s in steps:
            d = local + _dt.timedelta(days=s)
            r = h.fx_rate(d, cur)
            if r is not None:
                out.append((r, f"rate of {'previous' if s < 0 else 'next'} day {d} ({r}) used "
                               f"instead of {local} ({correct})"))
    elif variant == "bad_rate":
        opts = typo_rates(h, correct, cur, local)
        if rng is not None:
            rng.shuffle(opts)
        out.extend((r, f"wrong rate for {local}: {label}") for r, label in opts)
    return [(r, why) for r, why in out if r != correct]


def eligible(h, doc) -> bool:
    if doc.currency == h.base_currency or sole_entry(h, doc) is None:
        return False
    if isinstance(doc, Payment) and doc.amount_minor <= 0:
        return False
    rows = h.entry_rows(sole_entry(h, doc).entry_id)
    return h.local_date(rows[0].timestamp) == h.local_date(doc.event_ts)


def plan_for(h, doc, variant, rng=None):
    entry = sole_entry(h, doc)
    for rate, why in wrong_rates(h, doc, variant, rng):
        plan = rescaled(h, doc, entry, rate)
        if plan is not None:
            return entry, rate, why, plan
    return None


@register(NAME)
def inject_fx_conversion(ctx: InjectionContext, count: int) -> list:
    h = ctx.history
    start = ctx.rng.randrange(len(VARIANTS))
    issues = []
    for i in range(count):
        chosen = None
        for k in range(len(VARIANTS)):
            variant = VARIANTS[(start + i + k) % len(VARIANTS)]
            docs = [d for d in ctx.candidates() if eligible(h, d)]
            feasible = {}
            for d in docs:
                if plan_for(h, d, variant) is not None:
                    feasible[d.doc_id] = d
            doc = pick(ctx, list(feasible.values()),
                       lambda d: not_reserved_for_others(d, NAME))
            if doc is not None:
                chosen = (variant, doc)
                break
        if chosen is None:
            raise InjectionError("fx_conversion: no foreign-currency document left")
        variant, doc = chosen
        entry, rate, why, plan = plan_for(h, doc, variant, ctx.rng)
        issue_id = ctx.new_issue_id()
        ctx.claim(doc.doc_id, issue_id)
        correct = correct_key_amount(h, doc)
        kind = "invoice" if isinstance(doc, Invoice) else "payment"
        cause = f"{NAME}[{variant}]: {kind} {doc.doc_id} converted at {rate}: {why}"
        apply_rescale(ctx, plan, issue_id=issue_id, type_=NAME, cause=cause)
        issues.append(fx_issue(ctx, doc, entry, issue_id, correct))
    return issues
