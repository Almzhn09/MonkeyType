"""Hidden baseline company history (A04, CONTRACT §4).

``build_history(cfg, rng)`` creates a fully consistent history *before* any
error is injected: counterparties, chart of accounts, invoices with line
items, payments (full, split, multi-invoice, advances/overpayments, refunds),
background expenses, the FX table and balanced postings.

The result is a mutable :class:`History`. Every ledger row, entry and document
is addressable by its ID. Private metadata (``LedgerRow.role``/``meta``,
``Entry.kind``/``doc_id``, document ``tags``, entity names, account names,
``History.distractors``) exists only for the generator and the injection stage
and is **never** written to public files: ``fingen.export`` serialises public
fields explicitly via the ``to_public()`` methods.

Posting rules are implemented once in :func:`invoice_lines` /
:func:`payment_lines`; injection code should reuse them (e.g. with a wrong
``rate`` or ``posting_ts``) rather than re-deriving amounts.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from decimal import Decimal

from . import money, names
from .config import BASELINE_MAX_ROWS, BASELINE_MIN_ROWS, MAX_DOCUMENTS, MIN_DOCUMENTS, config_sha256
from .ids import IdFactory, episode_id_from, fx_key
from .rng import EpisodeRng
from .timeutil import (UTC_OFFSET, date_range, format_ts, local_date, local_datetime, offset_for,
                       offset_minutes, period_bounds, reformat, to_offset, utc_date)

WINDOW_PAD_DAYS = 10

LEDGER_COLUMNS = ("row_id", "entry_id", "timestamp", "account", "debit_minor", "credit_minor",
                  "currency", "entity_id", "invoice_id", "payment_id")

# Approximate USD value of one major unit; used only to make amounts and FX
# rates plausible.
REF_USD: dict[str, Decimal] = {
    "USD": Decimal("1"), "EUR": Decimal("1.08"), "GBP": Decimal("1.27"),
    "CNY": Decimal("0.138"), "JPY": Decimal("0.0066"), "KWD": Decimal("3.25"),
    "KZT": Decimal("0.0021"),
}


class GenerationError(RuntimeError):
    """The baseline could not satisfy the size targets."""


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------

@dataclass
class Entity:
    entity_id: str
    name: str
    kind: str  # "customer" | "vendor" (vendor IDs never appear in public files)


@dataclass
class LineItem:
    line_id: str
    description: str
    quantity: int
    unit_price_minor: int
    amount_minor: int

    def to_public(self) -> dict:
        return {"line_id": self.line_id, "description": self.description,
                "quantity": self.quantity, "unit_price_minor": self.unit_price_minor,
                "amount_minor": self.amount_minor}


@dataclass
class Invoice:
    doc_id: str
    invoice_id: str
    entity_id: str
    issued_at: str          # ISO-8601, company offset or +00:00
    due_at: str
    currency: str
    total_minor: int
    line_items: list[LineItem]
    tags: list[str] = field(default_factory=list)  # private

    kind = "invoice"

    @property
    def event_ts(self) -> str:
        return self.issued_at

    def to_public(self) -> dict:
        return {"doc_id": self.doc_id, "invoice_id": self.invoice_id, "entity_id": self.entity_id,
                "issued_at": self.issued_at, "due_at": self.due_at, "currency": self.currency,
                "total_minor": self.total_minor,
                "line_items": [li.to_public() for li in self.line_items]}


@dataclass
class InvoiceRef:
    invoice_id: str
    amount_minor: int

    def to_public(self) -> dict:
        return {"invoice_id": self.invoice_id, "amount_minor": self.amount_minor}


@dataclass
class Payment:
    doc_id: str
    payment_id: str
    entity_id: str
    timestamp: str
    currency: str
    amount_minor: int       # > 0 receipt, < 0 refund
    invoice_refs: list[InvoiceRef]
    tags: list[str] = field(default_factory=list)  # private

    kind = "payment"

    @property
    def event_ts(self) -> str:
        return self.timestamp

    @property
    def is_refund(self) -> bool:
        return self.amount_minor < 0

    @property
    def refs_total(self) -> int:
        return sum(r.amount_minor for r in self.invoice_refs)

    @property
    def fully_allocated(self) -> bool:
        """Σrefs == amount (no legitimate advance; eligible for payment_allocation)."""
        return self.amount_minor > 0 and bool(self.invoice_refs) and self.refs_total == self.amount_minor

    def to_public(self) -> dict:
        return {"doc_id": self.doc_id, "payment_id": self.payment_id, "entity_id": self.entity_id,
                "timestamp": self.timestamp, "currency": self.currency,
                "amount_minor": self.amount_minor,
                "invoice_refs": [r.to_public() for r in self.invoice_refs]}


@dataclass
class LedgerRow:
    row_id: str
    entry_id: str
    timestamp: str          # always in the company offset
    account: str
    debit_minor: int
    credit_minor: int
    currency: str           # always the base currency
    entity_id: str | None
    invoice_id: str | None
    payment_id: str | None
    role: str = ""          # private: ar | revenue | bank | unapplied | expense
    meta: dict = field(default_factory=dict)  # private

    def to_public(self) -> dict:
        return {c: getattr(self, c) for c in LEDGER_COLUMNS}


@dataclass
class Entry:
    entry_id: str
    kind: str               # private: invoice | payment | refund | expense
    doc_id: str | None      # private link to the source document
    row_ids: list[str] = field(default_factory=list)
    meta: dict = field(default_factory=dict)


@dataclass
class History:
    episode_id: str
    seed: int
    config: dict
    config_sha256: str
    company_name: str
    industry: str
    base_currency: str
    foreign_currencies: list[str]
    timezone: str
    utc_offset: str
    rounding_mode: str
    period_start: _dt.datetime
    period_end: _dt.datetime
    extract_start: _dt.datetime
    extract_end: _dt.datetime
    accounts: dict            # role -> code; "expense" -> list of codes
    account_names: dict       # code -> name (private)
    ids: IdFactory
    entities: dict[str, Entity] = field(default_factory=dict)
    invoices: dict[str, Invoice] = field(default_factory=dict)    # by doc_id
    payments: dict[str, Payment] = field(default_factory=dict)    # by doc_id
    rows: dict[str, LedgerRow] = field(default_factory=dict)      # by row_id
    entries: dict[str, Entry] = field(default_factory=dict)       # by entry_id
    fx: dict[tuple[str, str], str] = field(default_factory=dict)  # (YYYY-MM-DD, SRC) -> rate
    distractors: list[dict] = field(default_factory=list)         # private
    claimed: dict[str, str] = field(default_factory=dict)         # doc_id -> issue_id (private)
    changes: list[dict] = field(default_factory=list)             # provenance change records

    # ---- currencies / time ------------------------------------------------
    @property
    def currency_exponents(self) -> dict[str, int]:
        return {c: money.exponent(c) for c in sorted({self.base_currency, *self.foreign_currencies})}

    def local_date(self, ts) -> _dt.date:
        return local_date(ts, self.utc_offset)

    def to_local_ts(self, ts) -> str:
        """Same instant formatted in the company offset (ledger format)."""
        return reformat(ts, self.utc_offset)

    def in_period(self, ts) -> bool:
        return self.period_start <= to_offset(ts, self.utc_offset) < self.period_end

    def in_extract(self, ts) -> bool:
        return self.extract_start <= to_offset(ts, self.utc_offset) < self.extract_end

    def window_dates(self) -> list[_dt.date]:
        return date_range(self.extract_start.date(), self.extract_end.date())

    # ---- FX ----------------------------------------------------------------
    def fx_rate(self, day, currency: str) -> str | None:
        key = day if isinstance(day, str) else day.isoformat()
        return self.fx.get((key, currency))

    def fx_key(self, day, currency: str) -> str:
        key = day if isinstance(day, str) else day.isoformat()
        return fx_key(key, currency, self.base_currency)

    def to_base(self, amount_minor: int, currency: str, day=None, rate: str | None = None) -> int:
        """Convert per CONTRACT §4. ``rate`` overrides the table lookup."""
        if currency == self.base_currency:
            return amount_minor
        if rate is None:
            rate = self.fx_rate(day, currency)
            if rate is None:
                raise KeyError(f"no FX rate for {currency} on {day}")
        return money.convert(amount_minor, currency, self.base_currency, rate, self.rounding_mode)

    # ---- accounts ------------------------------------------------------------
    def account(self, role: str) -> str:
        code = self.accounts[role]
        return code[0] if isinstance(code, list) else code

    def role_of_account(self, code: str) -> str | None:
        for role, value in self.accounts.items():
            if code == value or (isinstance(value, list) and code in value):
                return role
        return None

    # ---- lookups -------------------------------------------------------------
    def doc(self, doc_id: str):
        return self.invoices.get(doc_id) or self.payments.get(doc_id)

    def documents(self) -> list:
        return [*self.invoices.values(), *self.payments.values()]

    def invoice_by_number(self, invoice_id: str) -> Invoice | None:
        for inv in self.invoices.values():
            if inv.invoice_id == invoice_id:
                return inv
        return None

    def payment_by_number(self, payment_id: str) -> Payment | None:
        for pay in self.payments.values():
            if pay.payment_id == payment_id:
                return pay
        return None

    def entries_of_doc(self, doc_id: str) -> list[Entry]:
        return [e for e in self.entries.values() if e.doc_id == doc_id]

    def entry_rows(self, entry_id: str) -> list[LedgerRow]:
        return [self.rows[r] for r in self.entries[entry_id].row_ids]

    def doc_rows(self, doc_id: str, role: str | None = None) -> list[LedgerRow]:
        out = []
        for e in self.entries_of_doc(doc_id):
            out.extend(r for r in self.entry_rows(e.entry_id) if role is None or r.role == role)
        return out

    def docs_with_tag(self, tag: str) -> list:
        return sorted((d for d in self.documents() if tag in d.tags), key=lambda d: d.doc_id)

    def sorted_rows(self) -> list[LedgerRow]:
        """CONTRACT §2 order: (timestamp instant, row_id)."""
        return sorted(self.rows.values(),
                      key=lambda r: (to_offset(r.timestamp, UTC_OFFSET), r.row_id))

    # ---- mutation primitives (used by build and by fingen.inject) -------------
    def new_entry(self, kind: str, doc_id: str | None, meta: dict | None = None) -> Entry:
        entry = Entry(self.ids.new("entry"), kind, doc_id, [], dict(meta or {}))
        self.entries[entry.entry_id] = entry
        return entry

    def add_row(self, row: LedgerRow) -> LedgerRow:
        if row.row_id in self.rows:
            raise ValueError(f"row {row.row_id} exists")
        if row.entry_id not in self.entries:
            raise ValueError(f"unknown entry {row.entry_id}; create it with new_entry()")
        self.rows[row.row_id] = row
        self.entries[row.entry_id].row_ids.append(row.row_id)
        return row

    def remove_row(self, row_id: str) -> LedgerRow:
        row = self.rows.pop(row_id)
        entry = self.entries[row.entry_id]
        entry.row_ids.remove(row_id)
        if not entry.row_ids:
            del self.entries[entry.entry_id]
        return row

    def make_row(self, entry: Entry, timestamp: str, role: str, debit: int, credit: int, *,
                 entity_id=None, invoice_id=None, payment_id=None, account: str | None = None,
                 meta: dict | None = None) -> LedgerRow:
        """Create (and add) a row with a fresh row_id from the episode factory."""
        row = LedgerRow(self.ids.new("row"), entry.entry_id, self.to_local_ts(timestamp),
                        account or self.account(role), debit, credit, self.base_currency,
                        entity_id, invoice_id, payment_id, role, dict(meta or {}))
        return self.add_row(row)


# --------------------------------------------------------------------------
# Posting rules (CONTRACT §4)
# --------------------------------------------------------------------------

def invoice_lines(h: History, inv: Invoice, *, rate: str | None = None,
                  rate_date: _dt.date | None = None) -> list[tuple]:
    """``[(role, debit, credit, invoice_id, payment_id)]`` for an invoice."""
    day = rate_date or h.local_date(inv.issued_at)
    base = h.to_base(inv.total_minor, inv.currency, day, rate)
    return [("ar", base, 0, inv.invoice_id, None), ("revenue", 0, base, inv.invoice_id, None)]


def payment_lines(h: History, pay: Payment, *, rate: str | None = None,
                  rate_date: _dt.date | None = None) -> list[tuple]:
    """``[(role, debit, credit, invoice_id, payment_id)]`` for a payment/refund."""
    day = rate_date or h.local_date(pay.timestamp)
    pid = pay.payment_id

    def conv(amount: int) -> int:
        return h.to_base(amount, pay.currency, day, rate)

    total = conv(abs(pay.amount_minor))
    if pay.amount_minor < 0:
        return [("unapplied", total, 0, None, pid), ("bank", 0, total, None, pid)]
    lines = [("bank", total, 0, None, pid)]
    refs = pay.invoice_refs
    if refs and pay.refs_total == pay.amount_minor:
        acc = 0
        for i, ref in enumerate(refs):
            b = conv(ref.amount_minor) if i < len(refs) - 1 else total - acc
            acc += b
            lines.append(("ar", 0, b, ref.invoice_id, pid))
    else:
        acc = 0
        for ref in refs:
            b = conv(ref.amount_minor)
            acc += b
            lines.append(("ar", 0, b, ref.invoice_id, pid))
        lines.append(("unapplied", 0, total - acc, None, pid))
    return lines


def post_document(h: History, doc, *, posting_ts: str | None = None, rate: str | None = None,
                  rate_date: _dt.date | None = None, meta: dict | None = None) -> Entry:
    """Create one entry for ``doc`` following CONTRACT §4 and return it."""
    if isinstance(doc, Invoice):
        lines, kind = invoice_lines(h, doc, rate=rate, rate_date=rate_date), "invoice"
    else:
        lines = payment_lines(h, doc, rate=rate, rate_date=rate_date)
        kind = "refund" if doc.is_refund else "payment"
    ts = posting_ts or doc.event_ts
    entry = h.new_entry(kind, doc.doc_id, meta)
    for idx, (role, debit, credit, invoice_id, payment_id) in enumerate(lines):
        h.make_row(entry, ts, role, debit, credit, entity_id=doc.entity_id,
                   invoice_id=invoice_id, payment_id=payment_id, meta={"line": idx})
    return entry


# --------------------------------------------------------------------------
# Builder
# --------------------------------------------------------------------------

def _round_sig(value: Decimal, sig: int = 3) -> int:
    n = int(value)
    digits = len(str(abs(n)))
    if digits > sig:
        q = 10 ** (digits - sig)
        n = (n // q) * q
    return n


class _Builder:
    def __init__(self, cfg: dict, er: EpisodeRng) -> None:
        self.cfg = cfg
        self.er = er
        self.rng = er.stream("documents")
        self.offset = offset_for(cfg["timezone"])
        self.off_min = offset_minutes(self.offset)
        self.base = cfg["base_currency"]
        self.foreign = list(cfg["foreign_currencies"])

    # ---------------------------------------------------------------- setup
    def build(self) -> History:
        cfg = self.cfg
        p_start, p_end = period_bounds(cfg["period"]["start_month"], cfg["period"]["months"],
                                       self.offset)
        pad = _dt.timedelta(days=WINDOW_PAD_DAYS)
        sha = config_sha256(cfg)
        episode_id = episode_id_from(sha)
        ids = IdFactory(self.er.stream("ids"), year=p_start.year)
        ids.reserve(episode_id)
        nrng = self.er.stream("names")
        industry = nrng.choice(sorted(names.INDUSTRIES))
        h = History(
            episode_id=episode_id, seed=cfg["seed"], config=cfg, config_sha256=sha,
            company_name=names.company_name(nrng, industry), industry=industry,
            base_currency=self.base, foreign_currencies=sorted(self.foreign),
            timezone=cfg["timezone"], utc_offset=self.offset, rounding_mode=cfg["rounding_mode"],
            period_start=p_start, period_end=p_end,
            extract_start=p_start - pad, extract_end=p_end + pad,
            accounts={}, account_names={}, ids=ids)
        self.h = h
        self.d_first = h.extract_start.date() + _dt.timedelta(days=1)
        self.d_last = h.extract_end.date() - _dt.timedelta(days=2)
        self.p_first = p_start.date()
        self.p_last = p_end.date() - _dt.timedelta(days=1)
        self._chart()
        self._fx()
        self._entities(nrng)
        self._documents()
        for doc in sorted(h.documents(), key=lambda d: d.doc_id):
            post_document(h, doc)
        self._background()
        self._tag_edges()
        self._check()
        return h

    def _chart(self) -> None:
        rng = self.er.stream("accounts")
        h = self.h
        style = rng.choice(("4digit", "6digit", "dotted", "prefixed"))
        ranges = {"bank": (1000, 1099), "ar": (1200, 1299), "unapplied": (2200, 2399),
                  "revenue": (4000, 4199), "expense": (6000, 7999)}
        used: set[str] = set()

        def code(role: str) -> str:
            while True:
                n = rng.randint(*ranges[role])
                if style == "4digit":
                    c = str(n)
                elif style == "6digit":
                    c = f"{n}{rng.randint(0, 99):02d}"
                elif style == "dotted":
                    c = f"{n}.{rng.randint(1, 30):02d}"
                else:
                    c = f"{rng.choice('ABGLS')}{n}"
                if c not in used:
                    used.add(c)
                    return c

        for role in ("ar", "revenue", "bank", "unapplied"):
            c = code(role)
            h.accounts[role] = c
            h.account_names[c] = rng.choice(names.ACCOUNT_NAMES[role])
        exp_names = list(names.INDUSTRIES[h.industry]["expenses"]) + ["Bank charges", "Utilities"]
        rng.shuffle(exp_names)
        n_exp = rng.randint(3, 6)
        codes = sorted(code("expense") for _ in range(n_exp))
        for c, nm in zip(codes, exp_names):
            h.account_names[c] = nm
        h.accounts["expense"] = codes

    def _fx(self) -> None:
        rng = self.er.stream("fx")
        h = self.h
        for cur in sorted(self.foreign):
            ref = REF_USD[cur] / REF_USD[self.base]
            places = 2 if ref >= 100 else 4 if ref >= 1 else 6
            anchor = ref * (Decimal(rng.randint(9200, 10800)) / Decimal(10000))
            rate = anchor
            for day in h.window_dates():
                bp = rng.randint(-45, 45) + int((anchor - rate) / anchor * 400)
                rate = rate * (1 + Decimal(bp) / Decimal(10000))
                s = money.format_rate(rate, places)
                if Decimal(s) <= 0:
                    s = money.format_rate(anchor, places)
                h.fx[(day.isoformat(), cur)] = s
                rate = Decimal(s)

    def _entities(self, nrng) -> None:
        h = self.h
        n_inv = self.cfg["size"]["invoices"]
        n_c = max(4, min(25, n_inv // 3))
        seen: set[str] = set()
        self.customers: list[str] = []
        self.weights: list[int] = []
        rng = self.er.stream("entities")
        while len(self.customers) < n_c:
            nm = names.customer_name(nrng)
            if nm in seen or nm == h.company_name:
                continue
            seen.add(nm)
            ent = Entity(h.ids.new("entity"), nm, "customer")
            h.entities[ent.entity_id] = ent
            self.customers.append(ent.entity_id)
            self.weights.append(rng.choice((1, 1, 2, 3, 5)))
        for _ in range(rng.randint(3, 8)):
            nm = names.customer_name(nrng)
            ent = Entity(h.ids.new("entity"), nm, "vendor")
            h.entities[ent.entity_id] = ent
        self.vendors = [e.entity_id for e in h.entities.values() if e.kind == "vendor"]

    # ------------------------------------------------------------- helpers
    def _entity(self, exclude: str | None = None) -> str:
        while True:
            e = self.rng.choices(self.customers, weights=self.weights)[0]
            if e != exclude:
                return e

    def _currency(self, foreign_prob: float = 0.35) -> str:
        if self.foreign and self.rng.random() < foreign_prob:
            return self.rng.choice(self.foreign)
        return self.base

    def _instant(self, d: _dt.date) -> _dt.datetime:
        """Business-hours instant on local date ``d``."""
        return local_datetime(d, self.rng.randint(8 * 60, 20 * 60 - 1), self.offset,
                              self.rng.randint(0, 59))

    def _edge_instant(self, side: str) -> tuple[_dt.datetime, bool]:
        """Instant next to a period boundary where (if offset != 0) the UTC
        date differs from the local date. Returns (instant, local_in_period)."""
        rng, o = self.rng, self.off_min
        end_date = self.h.period_end.date()
        if o > 0:
            d = self.p_first if side == "start" else end_date
            minute = rng.randint(1, o - 1)
        elif o < 0:
            d = self.p_first - _dt.timedelta(days=1) if side == "start" else self.p_last
            minute = rng.randint(24 * 60 + o + 1, 24 * 60 - 1)
        else:
            early = rng.random() < 0.5
            if side == "start":
                d = self.p_first if early else self.p_first - _dt.timedelta(days=1)
            else:
                d = end_date if early else self.p_last
            minute = rng.randint(0, 25) if early else rng.randint(24 * 60 - 25, 24 * 60 - 1)
        dt = local_datetime(d, minute, self.offset, rng.randint(0, 59))
        return dt, self.p_first <= d <= self.p_last

    def _near_boundary_instant(self) -> _dt.datetime:
        rng = self.rng
        end_date = self.h.period_end.date()
        d = rng.choice((self.p_first - _dt.timedelta(days=1), self.p_first, self.p_last, end_date))
        if rng.random() < 0.5:
            minute = rng.choice((rng.randint(0, 90), rng.randint(22 * 60, 24 * 60 - 1)))
            return local_datetime(d, minute, self.offset, rng.randint(0, 59))
        return self._instant(d)

    def _date_between(self, lo: _dt.date, hi: _dt.date) -> _dt.date | None:
        lo, hi = max(lo, self.d_first), min(hi, self.d_last)
        if lo > hi:
            return None
        return lo + _dt.timedelta(days=self.rng.randint(0, (hi - lo).days))

    def _price_minor(self, currency: str) -> int:
        rng = self.rng
        usd_cents = rng.choice((500, 1000, 2000, 5000, 10000, 20000, 50000, 100000, 200000)) \
            * rng.randint(80, 125) // 100
        value = Decimal(usd_cents) / 100 / REF_USD[currency] * (10 ** money.exponent(currency))
        return max(_round_sig(value, 3), 10 ** money.exponent(currency))

    def _lines(self, currency: str) -> list[LineItem]:
        rng = self.rng
        catalog = names.INDUSTRIES[self.h.industry]["items"]
        k = rng.choice((1, 1, 2, 2, 3, 4))
        out = []
        for desc in rng.sample(catalog, k):
            qty = rng.choice((1, 1, 1, 2, 2, 3, 4, 5, 10, 12))
            price = self._price_minor(currency)
            out.append(LineItem(self.h.ids.new("line"), desc, qty, price, qty * price))
        return out

    def _ts(self, dt: _dt.datetime, utc_repr: bool) -> str:
        return format_ts(dt.astimezone(_dt.timezone.utc)) if utc_repr else \
            reformat(dt, self.offset)

    # ------------------------------------------------------------ documents
    def _spec(self, issued: _dt.datetime, currency: str | None = None, entity: str | None = None,
              utc_repr: bool | None = None, tags=()) -> dict:
        spec = {"issued": issued, "currency": currency or self._currency(),
                "entity": entity or self._entity(),
                "utc_repr": self.rng.random() < 0.35 if utc_repr is None else utc_repr,
                "tags": list(tags), "pays": [], "copy_from": None, "plain": False}
        self.specs.append(spec)
        return spec

    def _plain_payment(self, spec: dict, unpaid_prob: float = 0.12) -> None:
        rng = self.rng
        if rng.random() < unpaid_prob:
            spec["tags"].append("unpaid")
            return
        delay = rng.choice((1, 2, 3, 5, 7, 10, 12, 14, 18, 21, 25, 30, 35))
        d = self.h.local_date(spec["issued"]) + _dt.timedelta(days=delay)
        if d > self.d_last:
            spec["tags"].append("unpaid")
            return
        spec["pays"].append({"dt": self._instant(d), "pct": 100, "tags": []})
        spec["plain"] = True

    def _split_dates(self, start: _dt.date, parts: int) -> list[_dt.date]:
        """Increasing payment dates after ``start``; falls back to 2-day gaps."""
        dates, d = [], start
        for _ in range(parts):
            d = d + _dt.timedelta(days=self.rng.randint(2, 9))
            dates.append(d)
        if dates[-1] > self.d_last:
            dates = [start + _dt.timedelta(days=2 * (i + 1)) for i in range(parts)]
        return dates

    def _documents(self) -> None:
        cfg, rng, h = self.cfg, self.rng, self.h
        n_inv = cfg["size"]["invoices"]
        dis, err, combos = cfg["distractors"], cfg["errors"], cfg["combos"]
        self.specs: list[dict] = []
        self.standalone: list[dict] = []   # payments without an invoice spec
        self.records: list[dict] = []      # distractor records (resolved to doc_ids later)
        days = _dt.timedelta

        # (a) UTC-boundary distractors: documents in +00:00 whose UTC date
        # differs from the local date, correctly posted on the local date.
        for i in range(dis["utc_boundary"]):
            side = ("start", "end")[(i // 2 + rng.randint(0, 1)) % 2]
            inst, inside = self._edge_instant(side)
            if i % 2 == 0:
                spec = self._spec(inst, self._currency(0.6), utc_repr=True,
                                  tags=["near_boundary", "distractor"])
                self._plain_payment(spec, unpaid_prob=0.3)
                self.records.append({"kind": "utc_boundary", "spec": spec, "pay": None,
                                     "boundary": "period_" + side})
            else:
                issued = self._instant(max(self.d_first, inst.date() - days(rng.randint(4, 12))))
                spec = self._spec(issued, self._currency(0.9))
                pay = {"dt": inst, "pct": 100, "utc_repr": True,
                       "tags": ["near_boundary", "distractor"]}
                spec["pays"].append(pay)
                self.records.append({"kind": "utc_boundary", "spec": spec, "pay": pay,
                                     "boundary": "period_" + side})

        # (b) Near-boundary pool (candidates for wrong_period / fx / combos).
        reserved = 2 * dis["repeated_amounts"] + dis["split_payments"] + dis["advances"] + 2
        budget = max(0, min(n_inv // 2 - len(self.specs), n_inv - len(self.specs) - reserved))
        nb_inv = min(budget, 3 + err["wrong_period"] + combos["duplicate_plus_period"]
                     + (err["fx_conversion"] + 1) // 2)
        nb_pay = min(max(0, budget - nb_inv), 2 + 2 * combos["fx_partial_payment_boundary"]
                     + (err["fx_conversion"] + 1) // 2 + err["payment_allocation"] // 2)
        for _ in range(nb_inv):
            if self.off_min and rng.random() < 0.5:
                inst, _inside = self._edge_instant(rng.choice(("start", "end")))
            else:
                inst = self._near_boundary_instant()
            spec = self._spec(inst, self._currency(0.5), tags=["near_boundary"])
            self._plain_payment(spec, unpaid_prob=0.25)
        for _ in range(nb_pay):
            if self.off_min and rng.random() < 0.6:
                inst, _inside = self._edge_instant(rng.choice(("start", "end")))
            else:
                inst = self._near_boundary_instant()
            issued = self._instant(max(self.d_first, inst.date() - days(rng.randint(4, 15))))
            spec = self._spec(issued, self._currency(0.85))
            pct = 100 if rng.random() < 0.5 else rng.randint(35, 70)
            pay = {"dt": inst, "pct": pct, "utc_repr": rng.random() < 0.5,
                   "tags": ["near_boundary"] + (["partial"] if pct < 100 else [])}
            spec["pays"].append(pay)
            if pct < 100:
                rest = self._date_between(inst.date() + days(2), inst.date() + days(12))
                if rest is None:
                    pay["pct"] = 100
                    pay["tags"].remove("partial")
                else:
                    spec["pays"].append({"dt": self._instant(rest), "pct": 100 - pct,
                                         "tags": ["partial"]})
                    spec["tags"].append("split")

        # (c) Repeated amounts: different invoices with identical totals.
        for _ in range(dis["repeated_amounts"]):
            d1 = self._date_between(self.p_first, self.p_last - days(6))
            a = self._spec(self._instant(d1), tags=["repeated_amount", "distractor"])
            d2 = self._date_between(d1 + days(3), self.p_last)
            same = rng.random() < 0.5
            b = self._spec(self._instant(d2), a["currency"],
                           a["entity"] if same else self._entity(exclude=a["entity"]),
                           tags=["repeated_amount", "distractor"])
            b["copy_from"] = a
            self._plain_payment(a, 0.1)
            self._plain_payment(b, 0.1)
            self.records.append({"kind": "repeated_amounts", "specs": [a, b]})

        # (d) Split payments: one invoice settled by 2-3 payments, Σ = total.
        for _ in range(dis["split_payments"]):
            d = self._date_between(self.p_first - days(5), self.p_last - days(8))
            spec = self._spec(self._instant(d), tags=["split", "distractor"])
            n_parts = rng.choice((2, 2, 3))
            dates = self._split_dates(d, n_parts)
            pcts = [rng.randint(20, 100 // len(dates) + 10) for _ in dates[:-1]]
            for dt_, pct in zip(dates, pcts + [100 - sum(pcts)]):
                spec["pays"].append({"dt": self._instant(dt_), "pct": pct,
                                     "tags": ["split", "distractor"]})
            self.records.append({"kind": "split_payments", "spec": spec})

        # (e) Advances (prepayment without refs, or overpayment of an invoice)
        # and refunds of advances (Dr unapplied / Cr bank).
        for i in range(dis["advances"]):
            d = self._date_between(self.p_first, self.p_last - days(5))
            refund = i < dis["refunds"]
            if rng.random() < 0.5:
                currency, entity = self._currency(0.4), self._entity()
                amount = self._price_minor(currency) * rng.choice((2, 3, 5, 10))
                adv = {"dt": self._instant(d), "entity": entity, "currency": currency,
                       "amount": amount, "refs": [], "tags": ["advance", "prepayment", "distractor"]}
                self.standalone.append(adv)
                rec = {"kind": "advances", "subtype": "prepayment", "pays": [adv]}
                refund_amount = amount * rng.randint(30, 100) // 100
            else:
                issued = self._instant(d - days(rng.randint(2, 10)))
                spec = self._spec(issued, tags=["overpaid", "distractor"])
                adv = {"dt": self._instant(d), "pct": 100, "overpay_pct": rng.randint(5, 20),
                       "tags": ["advance", "overpayment", "distractor"]}
                spec["pays"].append(adv)
                currency, entity = spec["currency"], spec["entity"]
                rec = {"kind": "advances", "subtype": "overpayment", "spec": spec, "pays": [adv]}
                refund_amount = None  # = excess, resolved once the total is known
            self.records.append(rec)
            if refund:
                rd = self._date_between(d + days(2), d + days(12))
                ref = {"dt": self._instant(rd), "entity": entity, "currency": currency,
                       "amount": refund_amount, "refund_of": adv, "refs": [],
                       "tags": ["refund", "distractor"]}
                self.standalone.append(ref)
                self.records.append({"kind": "refunds", "pays": [ref, adv]})

        # (f) Ordinary invoices across the extract window (~70% in period).
        rest = n_inv - len(self.specs)
        if rest < 0:
            raise GenerationError("too many special invoices for size.invoices")
        for _ in range(rest):
            if rng.random() < 0.7:
                d = self._date_between(self.p_first, self.p_last)
            else:
                d = self._date_between(self.d_first, self.d_last)
            spec = self._spec(self._instant(d))
            self._plain_payment(spec)

        # (g) Multi-invoice payments: merge two plain payments of the same
        # customer and currency into one payment with two refs.
        groups: dict[tuple, list[dict]] = {}
        for spec in self.specs:
            if spec["plain"] and "distractor" not in spec["tags"] and len(spec["pays"]) == 1:
                groups.setdefault((spec["entity"], spec["currency"]), []).append(spec)
        merges = max(1, n_inv // 20)
        for key in sorted(groups):
            if merges <= 0:
                break
            g = groups[key]
            if len(g) >= 2:
                a, b = g[0], g[1]
                pa, pb = a["pays"][0], b["pays"][0]
                later = pa if pa["dt"] >= pb["dt"] else pb
                merged = {"dt": later["dt"], "entity": a["entity"], "currency": a["currency"],
                          "multi": [a, b] if rng.random() < 0.5 else [b, a], "refs": None,
                          "tags": ["multi_ref"]}
                a["pays"], b["pays"] = [], []
                a["plain"] = b["plain"] = False
                self.standalone.append(merged)
                merges -= 1

        self._ensure_min_documents()
        self._materialize()

    def _ensure_min_documents(self) -> None:
        """Small configs: pay unpaid invoices / split plain payments until the
        planned document count reaches MIN_DOCUMENTS."""
        rng, days = self.rng, _dt.timedelta

        def planned() -> int:
            return len(self.specs) + sum(len(s["pays"]) for s in self.specs) + len(self.standalone)

        while planned() < MIN_DOCUMENTS:
            changed = False
            for spec in self.specs:
                issued = self.h.local_date(spec["issued"])
                if "unpaid" in spec["tags"] and "distractor" not in spec["tags"] \
                        and issued + days(1) <= self.d_last:
                    spec["tags"].remove("unpaid")
                    d = self._date_between(issued + days(1), issued + days(20))
                    spec["pays"].append({"dt": self._instant(d), "pct": 100, "tags": []})
                    changed = True
                    break
            if not changed:
                for spec in self.specs:
                    issued = self.h.local_date(spec["issued"])
                    if spec["plain"] and len(spec["pays"]) == 1 and issued + days(4) <= self.d_last:
                        d1 = self._date_between(issued + days(1), issued + days(1))
                        d2 = self._date_between(issued + days(2), issued + days(12))
                        pct = rng.randint(30, 70)
                        spec["pays"] = [{"dt": self._instant(d1), "pct": pct, "tags": ["split"]},
                                        {"dt": self._instant(d2), "pct": 100 - pct, "tags": ["split"]}]
                        spec["plain"] = False
                        spec["tags"].append("split")
                        changed = True
                        break
            if not changed:
                raise GenerationError("cannot reach the minimum number of documents")

    def _materialize(self) -> None:
        h, rng = self.h, self.rng
        # Invoices (copies after their originals so totals match).
        ordered = [s for s in self.specs if s["copy_from"] is None] + \
                  [s for s in self.specs if s["copy_from"] is not None]
        for spec in ordered:
            if spec["copy_from"] is not None:
                lines = [LineItem(h.ids.new("line"), li.description, li.quantity,
                                  li.unit_price_minor, li.amount_minor)
                         for li in spec["copy_from"]["invoice"].line_items]
            else:
                lines = self._lines(spec["currency"])
            issued = spec["issued"]
            terms = rng.choice((7, 14, 15, 30, 30, 45, 60))
            inv = Invoice(h.ids.new("doc"), h.ids.new("invoice"), spec["entity"],
                          self._ts(issued, spec["utc_repr"]),
                          self._ts(issued + _dt.timedelta(days=terms), spec["utc_repr"]),
                          spec["currency"], sum(li.amount_minor for li in lines), lines,
                          sorted(set(spec["tags"])))
            h.invoices[inv.doc_id] = inv
            spec["invoice"] = inv

        def add_payment(dt, entity, currency, amount, refs, tags, utc_repr=None) -> Payment:
            if utc_repr is None:
                utc_repr = rng.random() < 0.35
            pay = Payment(h.ids.new("doc"), h.ids.new("payment"), entity, self._ts(dt, utc_repr),
                          currency, amount, refs, sorted(set(tags)))
            h.payments[pay.doc_id] = pay
            return pay

        for spec in self.specs:
            inv = spec["invoice"]
            paid = 0
            for i, p in enumerate(spec["pays"]):
                last = i == len(spec["pays"]) - 1
                part = inv.total_minor - paid if last and len(spec["pays"]) > 1 \
                    else inv.total_minor * p["pct"] // 100
                paid += part
                amount = part
                if p.get("overpay_pct"):
                    amount += max(part * p["overpay_pct"] // 100,
                                  10 ** money.exponent(inv.currency))
                p["payment"] = add_payment(p["dt"], inv.entity_id, inv.currency, amount,
                                           [InvoiceRef(inv.invoice_id, part)], p["tags"],
                                           p.get("utc_repr"))
        for p in sorted(self.standalone, key=lambda x: "refund_of" in x):
            if "multi" in p:
                refs = [InvoiceRef(s["invoice"].invoice_id, s["invoice"].total_minor)
                        for s in p["multi"]]
                p["payment"] = add_payment(p["dt"], p["entity"], p["currency"],
                                           sum(r.amount_minor for r in refs), refs, p["tags"])
            elif "refund_of" in p:
                adv = p["refund_of"]["payment"]
                amount = p["amount"] if p["amount"] is not None else \
                    adv.amount_minor - adv.refs_total
                p["payment"] = add_payment(p["dt"], p["entity"], p["currency"], -amount, [],
                                           p["tags"])
            else:
                p["payment"] = add_payment(p["dt"], p["entity"], p["currency"], p["amount"], [],
                                           p["tags"])

        # Distractor records -> doc ids (private, provenance only).
        for rec in self.records:
            kind = rec["kind"]
            out = {"kind": kind}
            if kind == "utc_boundary":
                doc = rec["pay"]["payment"] if rec["pay"] else rec["spec"]["invoice"]
                ts = doc.event_ts
                out.update({"doc_ids": [doc.doc_id], "doc_timestamp": ts,
                            "local_date": h.local_date(ts).isoformat(),
                            "utc_date": utc_date(ts).isoformat(),
                            "local_in_period": h.in_period(ts), "boundary": rec["boundary"],
                            "note": "posted on the local date; UTC date differs"
                            if h.local_date(ts) != utc_date(ts) else
                            "company offset is +00:00; event placed next to the boundary"})
            elif kind == "repeated_amounts":
                a, b = (s["invoice"] for s in rec["specs"])
                out.update({"doc_ids": sorted([a.doc_id, b.doc_id]), "currency": a.currency,
                            "total_minor": a.total_minor,
                            "same_entity": a.entity_id == b.entity_id})
            elif kind == "split_payments":
                inv = rec["spec"]["invoice"]
                out.update({"doc_ids": [inv.doc_id] + sorted(p["payment"].doc_id
                                                             for p in rec["spec"]["pays"]),
                            "invoice_id": inv.invoice_id})
            elif kind == "advances":
                pay = rec["pays"][0]["payment"]
                ids_ = [pay.doc_id] + ([rec["spec"]["invoice"].doc_id] if "spec" in rec else [])
                out.update({"subtype": rec["subtype"], "doc_ids": ids_,
                            "unapplied_minor": pay.amount_minor - pay.refs_total,
                            "currency": pay.currency})
            elif kind == "refunds":
                refund, adv = (p["payment"] for p in rec["pays"])
                out.update({"doc_ids": [refund.doc_id, adv.doc_id],
                            "amount_minor": refund.amount_minor, "currency": refund.currency})
            h.distractors.append(out)
        for p in self.standalone:
            if "multi" in p:
                pay = p["payment"]
                h.distractors.append({"kind": "multi_invoice_payment", "doc_ids": [pay.doc_id],
                                      "invoice_ids": [r.invoice_id for r in pay.invoice_refs]})
        h.distractors.sort(key=lambda r: (r["kind"], r["doc_ids"]))

    # ------------------------------------------------------------ background
    def _background(self) -> None:
        h = self.h
        rng = self.er.stream("background")
        expense = h.accounts["expense"]

        def add_one() -> None:
            d = self.d_first + _dt.timedelta(days=rng.randint(0, (self.d_last - self.d_first).days))
            ts = local_datetime(d, rng.randint(7 * 60, 21 * 60), self.offset, rng.randint(0, 59))
            n_lines = 1 if rng.random() < 0.75 else 2
            amounts = []
            for _ in range(n_lines):
                usd_cents = rng.choice((2000, 5000, 10000, 25000, 60000, 150000, 400000)) \
                    * rng.randint(70, 130) // 100
                value = Decimal(usd_cents) / 100 / REF_USD[self.base] \
                    * (10 ** money.exponent(self.base))
                amounts.append(max(_round_sig(value, 3), 10 ** money.exponent(self.base)))
            entry = h.new_entry("expense", None, {"vendor": rng.choice(self.vendors)})
            accts = rng.sample(expense, n_lines)
            for acct, amt in zip(accts, amounts):
                h.make_row(entry, format_ts(ts), "expense", amt, 0, account=acct)
            h.make_row(entry, format_ts(ts), "bank", 0, sum(amounts))

        for _ in range(self.cfg["size"]["background_entries"]):
            add_one()
        while len(h.rows) < BASELINE_MIN_ROWS:
            add_one()

    def _tag_edges(self) -> None:
        h = self.h
        for doc in h.documents():
            ts = doc.event_ts
            if h.local_date(ts) != utc_date(ts) and "utc_edge" not in doc.tags:
                doc.tags = sorted(doc.tags + ["utc_edge"])
            if h.in_period(ts) and "in_period" not in doc.tags:
                doc.tags = sorted(doc.tags + ["in_period"])

    def _check(self) -> None:
        h = self.h
        n_docs = len(h.invoices) + len(h.payments)
        if not MIN_DOCUMENTS <= n_docs <= MAX_DOCUMENTS:
            raise GenerationError(f"documents {n_docs} outside [{MIN_DOCUMENTS}, {MAX_DOCUMENTS}]")
        if len(h.rows) > BASELINE_MAX_ROWS:
            raise GenerationError(f"ledger rows {len(h.rows)} exceed {BASELINE_MAX_ROWS}; "
                                  "reduce size.background_entries")
        for row in h.rows.values():
            if (row.debit_minor > 0) == (row.credit_minor > 0) or min(row.debit_minor,
                                                                       row.credit_minor) < 0:
                raise GenerationError(f"row {row.row_id} must have exactly one positive side")
        for doc in h.documents():
            if not h.in_extract(doc.event_ts):
                raise GenerationError(f"document {doc.doc_id} outside the extract window")


def build_history(cfg: dict, er: EpisodeRng | None = None) -> History:
    """Build the consistent baseline history for a *resolved* config."""
    return _Builder(cfg, er or EpisodeRng(cfg["seed"])).build()
