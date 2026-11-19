"""Independent recomputation from PUBLIC data only (CONTRACT v1.0 §4–§5).

* ``expected_invoice`` / ``expected_payment`` — reference postings per §4.
* ``reconcile(pub)`` — reference reconciler: scans every document and returns all §5
  discrepancies as §7 findings (first evidence-set rule).
* ``audit(pub, findings)`` — discrepancies that are NOT explained by any §5 type (a posting
  that disagrees with its document while no finding covers that document, malformed entry
  structure, ...). An episode is consistent only if this list is empty.
* ``amount_from_evidence`` — recompute an issue's amount from ONLY the records named in
  one evidence set (+ manifest/policy/fx), to prove the set is sufficient.
"""
from __future__ import annotations

from collections import Counter
from decimal import ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal, localcontext

from .load import FX_KEY_RE, Public, canon_ids, fx_key, parse_ts

ROUNDING = {"half_up": ROUND_HALF_UP, "half_even": ROUND_HALF_EVEN}


class RecomputeError(Exception):
    pass


# --------------------------------------------------------------------------- money

def convert(amount_minor: int, rate: Decimal, exp_src: int, exp_base: int, mode: str) -> int:
    """base = round(amount × rate × 10^(exp_base − exp_src)), per conversion, to integer."""
    if amount_minor < 0:
        return -convert(-amount_minor, rate, exp_src, exp_base, mode)
    with localcontext() as ctx:
        ctx.prec = 80
        v = Decimal(amount_minor) * Decimal(rate) * (Decimal(10) ** (exp_base - exp_src))
        return int(v.quantize(Decimal(1), rounding=ROUNDING[mode]))


def to_base(pub: Public, amount: int, currency: str, ts: str):
    """Convert ``amount`` at the rate of the event's local date. Returns (base, fx_key|None)."""
    if currency == pub.base:
        return amount, None
    d = pub.local_date(ts)
    rate = pub.fx.get((d.isoformat(), currency))
    if rate is None:
        raise RecomputeError(f"no FX rate for {currency} on {d}")
    return (convert(amount, rate, pub.exps[currency], pub.exps[pub.base], pub.rounding_mode),
            fx_key(d, currency, pub.base))


def expected_invoice(pub: Public, inv: dict) -> dict:
    amt, key = to_base(pub, inv["total_minor"], inv["currency"], inv["issued_at"])
    return {"ar_dr": amt, "revenue_cr": amt, "date": pub.local_date(inv["issued_at"]),
            "fx_key": key}


def expected_payment(pub: Public, pay: dict) -> dict:
    cur, ts, amt = pay["currency"], pay["timestamp"], pay["amount_minor"]
    total, key = to_base(pub, abs(amt), cur, ts)
    out = {"date": pub.local_date(ts), "fx_key": key}
    if amt < 0:
        out.update(kind="refund", unapplied_dr=total, bank_cr=total)
        return out
    refs = pay["invoice_refs"]
    ar = [(r["invoice_id"], to_base(pub, r["amount_minor"], cur, ts)[0]) for r in refs]
    if refs and sum(r["amount_minor"] for r in refs) == amt:
        # rounding remainder goes to the last ref (in invoice_refs order)
        ar[-1] = (ar[-1][0], total - sum(a for _, a in ar[:-1]))
        unapplied = 0
    else:
        unapplied = total - sum(a for _, a in ar)
    out.update(kind="receipt", bank_dr=total, ar=ar, unapplied_cr=unapplied)
    return out


# --------------------------------------------------------------------------- ledger views

def entry_key(pub: Public, eid: str):
    return (parse_ts(pub.entries[eid][0]["timestamp"]), eid)


def classify_entries(pub: Public):
    """Group entries by document: payment entries (any row with payment_id), invoice
    entries (invoice_id but no payment_id), other entries."""
    inv_e, pay_e, other, problems = {}, {}, [], []
    for eid, rows in pub.entries.items():
        pids = {r["payment_id"] for r in rows if r["payment_id"]}
        iids = {r["invoice_id"] for r in rows if r["invoice_id"]}
        if pids:
            if len(pids) > 1:
                problems.append(f"entry {eid} mixes payments {sorted(pids)}")
            for pid in pids:
                pay_e.setdefault(pid, []).append(eid)
        elif iids:
            if len(iids) > 1:
                problems.append(f"entry {eid} mixes invoices {sorted(iids)}")
            for iid in iids:
                inv_e.setdefault(iid, []).append(eid)
        else:
            other.append(eid)
    for d in (inv_e, pay_e):
        for k in d:
            d[k].sort(key=lambda e: entry_key(pub, e))
    return inv_e, pay_e, other, problems


def _rows(pub, eid, role, side=None):
    out = [r for r in pub.entries[eid] if pub.role(r) == role]
    if side == "dr":
        out = [r for r in out if r["debit_minor"] > 0]
    elif side == "cr":
        out = [r for r in out if r["credit_minor"] > 0]
    return out


def _dr(rows):
    return sum(r["debit_minor"] for r in rows)


def _cr(rows):
    return sum(r["credit_minor"] for r in rows)


def _finding(pub, ftype, doc_id, amount, evidence, explanation):
    return {"type": ftype, "affected_ids": [doc_id], "claimed_amount_minor": int(amount),
            "currency": pub.base, "evidence_ids": [doc_id] + [e for e in evidence
                                                              if e != doc_id],
            "explanation": explanation}


def _invoice_entry_order(pub, inv, entries, exp):
    def key(eid):
        date_ok = pub.local_date(pub.entries[eid][0]["timestamp"]) == exp["date"]
        amt_ok = _dr(_rows(pub, eid, "ar", "dr")) == exp["ar_dr"]
        return (not date_ok, not amt_ok) + entry_key(pub, eid)
    return sorted(entries, key=key)


def _ids(rows):
    return [r["row_id"] for r in rows]


# --------------------------------------------------------------------------- reconciler

def reconcile(pub: Public) -> list:
    """Reference reconciler: all §5 discrepancies found in the public files."""
    inv_e, pay_e, _, _ = classify_entries(pub)
    findings = []
    for inv in sorted(pub.invoices, key=lambda d: d["doc_id"]):
        doc, iid = inv["doc_id"], inv["invoice_id"]
        entries = inv_e.get(iid, [])
        if not entries:
            continue  # reported by audit()
        try:
            exp = expected_invoice(pub, inv)
        except RecomputeError:
            continue
        ordered = _invoice_entry_order(pub, inv, entries, exp)
        original, extras = ordered[0], ordered[1:]
        if extras:
            amount = sum(_dr(_rows(pub, e, "ar", "dr")) for e in extras)
            ev = []
            for e in ordered:
                ev += _ids(_rows(pub, e, "ar", "dr")) or _ids(_rows(pub, e, "revenue", "cr"))
            findings.append(_finding(
                pub, "duplicate_invoice", doc, amount, ev,
                f"Invoice {iid} is posted {len(ordered)} times (entries "
                f"{', '.join(ordered)}); expected one. Overstatement = Dr AR of the extra "
                f"posting(s) = {amount}."))
        issued_in = pub.in_period(inv["issued_at"])
        for e in ordered:
            ts = pub.entries[e][0]["timestamp"]
            if pub.in_period(ts) != issued_in:
                ar = _rows(pub, e, "ar", "dr")
                amount = _dr(ar) if ar else _cr(_rows(pub, e, "revenue", "cr"))
                ev = _ids(ar) or _ids(_rows(pub, e, "revenue", "cr"))
                findings.append(_finding(
                    pub, "wrong_period", doc, amount, ev,
                    f"Invoice {iid} issued {inv['issued_at']} (local date {exp['date']}, "
                    f"{'in' if issued_in else 'outside'} the period) but entry {e} is posted "
                    f"{ts} ({'in' if not issued_in else 'outside'} the period)."))
        if inv["currency"] != pub.base:
            ar = _rows(pub, original, "ar", "dr")
            rec = _dr(ar)
            if ar and rec != exp["ar_dr"]:
                diff = abs(rec - exp["ar_dr"])
                findings.append(_finding(
                    pub, "fx_conversion", doc, diff, _ids(ar) + [exp["fx_key"]],
                    f"Invoice {iid}: {inv['total_minor']} {inv['currency']} at the "
                    f"{exp['date']} rate ({exp['fx_key']}) is {exp['ar_dr']} {pub.base}; "
                    f"ledger records Dr AR {rec}. Difference {diff}."))
    for pay in sorted(pub.payments, key=lambda d: d["doc_id"]):
        doc, pid = pay["doc_id"], pay["payment_id"]
        entries = pay_e.get(pid, [])
        if not entries:
            continue
        try:
            exp = expected_payment(pub, pay)
        except RecomputeError:
            continue
        e = entries[0]
        if exp["kind"] == "receipt":
            bank = _rows(pub, e, "bank", "dr")
            rec_bank = _dr(bank)
            want_bank = exp["bank_dr"]
        else:
            bank = _rows(pub, e, "bank", "cr")
            rec_bank = _cr(bank)
            want_bank = exp["bank_cr"]
        if pay["currency"] != pub.base and bank and rec_bank != want_bank:
            diff = abs(rec_bank - want_bank)
            findings.append(_finding(
                pub, "fx_conversion", doc, diff, _ids(bank) + [exp["fx_key"]],
                f"Payment {pid}: {abs(pay['amount_minor'])} {pay['currency']} at the "
                f"{exp['date']} rate ({exp['fx_key']}) is {want_bank} {pub.base}; ledger "
                f"bank line records {rec_bank}. Difference {diff}."))
        if exp["kind"] == "receipt":
            refs_total = sum(r["amount_minor"] for r in pay["invoice_refs"])
            if pay["invoice_refs"] and refs_total == pay["amount_minor"]:
                un = _rows(pub, e, "unapplied", "cr")
                amount = _cr(un)
                if amount > 0:
                    ar_cr = _cr(_rows(pub, e, "ar", "cr"))
                    findings.append(_finding(
                        pub, "payment_allocation", doc, amount, _ids(un),
                        f"Payment {pid} is fully allocated by invoice_refs (Σrefs = amount "
                        f"= {refs_total} {pay['currency']}) but the ledger credits AR only "
                        f"{ar_cr} of Dr bank {rec_bank}; {amount} went to unapplied."))
    return findings


# --------------------------------------------------------------------------- audit

def _strict_invoice(pub, inv, entries):
    p, iid = [], inv["invoice_id"]
    if len(entries) != 1:
        return [f"invoice {inv['doc_id']}: expected 1 posting, found {len(entries)}"]
    eid = entries[0]
    exp = expected_invoice(pub, inv)
    rows = pub.entries[eid]
    ar, rev = _rows(pub, eid, "ar", "dr"), _rows(pub, eid, "revenue", "cr")
    if len(ar) != 1 or len(rev) != 1 or len(rows) != 2:
        p.append(f"invoice {inv['doc_id']}: entry {eid} must be exactly Dr AR + Cr revenue")
    if _dr(ar) != exp["ar_dr"] or _cr(rev) != exp["revenue_cr"]:
        p.append(f"invoice {inv['doc_id']}: entry {eid} amounts Dr AR {_dr(ar)} / Cr revenue "
                 f"{_cr(rev)} != expected {exp['ar_dr']}")
    if pub.local_date(rows[0]["timestamp"]) != exp["date"]:
        p.append(f"invoice {inv['doc_id']}: entry {eid} local date "
                 f"{pub.local_date(rows[0]['timestamp'])} != issued_at local date {exp['date']}")
    for r in rows:
        if r["invoice_id"] != iid or r["payment_id"] is not None or \
                r["entity_id"] != inv["entity_id"]:
            p.append(f"invoice {inv['doc_id']}: row {r['row_id']} must carry invoice_id={iid}, "
                     f"entity_id={inv['entity_id']}, payment_id=null")
    return p


def _strict_payment(pub, pay, entries):
    p, pid, doc = [], pay["payment_id"], pay["doc_id"]
    if len(entries) != 1:
        return [f"payment {doc}: expected 1 posting, found {len(entries)}"]
    eid = entries[0]
    exp = expected_payment(pub, pay)
    rows = pub.entries[eid]
    if pub.local_date(rows[0]["timestamp"]) != exp["date"]:
        p.append(f"payment {doc}: entry {eid} local date "
                 f"{pub.local_date(rows[0]['timestamp'])} != payment local date {exp['date']}")
    for r in rows:
        if r["payment_id"] != pid:
            p.append(f"payment {doc}: row {r['row_id']} must carry payment_id={pid}")
    if exp["kind"] == "refund":
        un, bank = _rows(pub, eid, "unapplied", "dr"), _rows(pub, eid, "bank", "cr")
        if len(un) != 1 or len(bank) != 1 or len(rows) != 2:
            p.append(f"payment {doc}: refund entry {eid} must be Dr unapplied + Cr bank")
        if _dr(un) != exp["unapplied_dr"] or _cr(bank) != exp["bank_cr"]:
            p.append(f"payment {doc}: refund amounts {_dr(un)}/{_cr(bank)} != expected "
                     f"{exp['bank_cr']}")
        if any(r["invoice_id"] is not None for r in rows):
            p.append(f"payment {doc}: refund rows must have invoice_id=null")
        return p
    bank = _rows(pub, eid, "bank", "dr")
    ar = _rows(pub, eid, "ar", "cr")
    un = _rows(pub, eid, "unapplied", "cr")
    if len(bank) != 1 or _dr(bank) != exp["bank_dr"]:
        p.append(f"payment {doc}: Dr bank {_dr(bank)} ({len(bank)} rows) != expected "
                 f"{exp['bank_dr']}")
    got = Counter((r["invoice_id"], r["credit_minor"]) for r in ar)
    if got != Counter(exp["ar"]):
        p.append(f"payment {doc}: Cr AR lines {sorted(got.elements())} != expected "
                 f"{sorted(exp['ar'])}")
    want_un = [exp["unapplied_cr"]] if exp["unapplied_cr"] > 0 else []
    if [r["credit_minor"] for r in un] != want_un:
        p.append(f"payment {doc}: Cr unapplied {[r['credit_minor'] for r in un]} != expected "
                 f"{want_un}")
    if len(rows) != len(bank) + len(ar) + len(un):
        p.append(f"payment {doc}: entry {eid} has rows outside Dr bank/Cr AR/Cr unapplied")
    for r in bank + un:
        if r["invoice_id"] is not None:
            p.append(f"payment {doc}: row {r['row_id']} ({pub.role(r)}) must have "
                     f"invoice_id=null")
    return p


def _structural(pub, inv_e, pay_e, other):
    """Role/side structure that every §5 error type preserves."""
    p = []
    for eid in other:
        for r in pub.entries[eid]:
            role = pub.role(r)
            if not ((role == "expense" and r["debit_minor"] > 0) or
                    (role == "bank" and r["credit_minor"] > 0)):
                p.append(f"entry {eid}: non-document entry must be Dr expense / Cr bank "
                         f"(row {r['row_id']} is {role})")
    for iid, eids in inv_e.items():
        for eid in eids:
            for r in pub.entries[eid]:
                role = pub.role(r)
                if not ((role == "ar" and r["debit_minor"] > 0) or
                        (role == "revenue" and r["credit_minor"] > 0)):
                    p.append(f"entry {eid} (invoice {iid}): row {r['row_id']} must be Dr AR "
                             f"or Cr revenue")
    for pid, eids in pay_e.items():
        pay = pub.payment_by_pid[pid]
        refs = {x["invoice_id"] for x in pay["invoice_refs"]}
        for eid in eids:
            for r in pub.entries[eid]:
                role, dr = pub.role(r), r["debit_minor"] > 0
                if pay["amount_minor"] > 0:
                    ok = (role == "bank" and dr) or (role == "unapplied" and not dr) or \
                         (role == "ar" and not dr and r["invoice_id"] in refs)
                else:
                    ok = (role == "unapplied" and dr) or (role == "bank" and not dr)
                if not ok:
                    p.append(f"entry {eid} (payment {pid}): row {r['row_id']} ({role}, "
                             f"{'Dr' if dr else 'Cr'}, invoice {r['invoice_id']}) not allowed")
    return p


def audit(pub: Public, findings=None, strict_all: bool = False) -> list:
    """Problems not explained by the given findings. Documents covered by a finding are
    exempt from the exact amount/date/count comparison (structure is still checked).
    With ``strict_all`` (baseline ledger) nothing is exempt."""
    inv_e, pay_e, other, problems = classify_entries(pub)
    problems = list(problems) + _structural(pub, inv_e, pay_e, other)
    covered = set()
    if not strict_all:
        for f in findings if findings is not None else reconcile(pub):
            covered.update(canon_ids(f["affected_ids"]))
    for inv in pub.invoices:
        if inv["doc_id"] in covered:
            continue
        try:
            problems += _strict_invoice(pub, inv, inv_e.get(inv["invoice_id"], []))
        except RecomputeError as exc:
            problems.append(f"invoice {inv['doc_id']}: {exc}")
    for pay in pub.payments:
        if pay["doc_id"] in covered:
            continue
        try:
            problems += _strict_payment(pub, pay, pay_e.get(pay["payment_id"], []))
        except RecomputeError as exc:
            problems.append(f"payment {pay['doc_id']}: {exc}")
    return problems


# --------------------------------------------------------------------------- evidence

def amount_from_evidence(pub: Public, itype: str, doc_id: str, evidence_set) -> int:
    """Recompute the discrepancy amount using only: the document and ledger rows named in
    ``evidence_set``, FX keys named in it (for fx_conversion), plus manifest/policy (and the
    fx table where the §4 expected value of a document is needed). Raises RecomputeError
    when the set does not establish the discrepancy."""
    ids = set(canon_ids(evidence_set))
    if doc_id not in ids:
        raise RecomputeError(f"evidence set lacks the document {doc_id}")
    doc = pub.docs.get(doc_id)
    if doc is None:
        raise RecomputeError(f"{doc_id} is not a document")
    rows = [pub.rows_by_id[i] for i in sorted(ids) if i in pub.rows_by_id]
    fxk = [i for i in ids if FX_KEY_RE.match(i)]
    is_inv = doc_id in pub.invoices_by_doc

    def of_doc(r):
        if is_inv:
            return r["invoice_id"] == doc["invoice_id"] and r["payment_id"] is None
        return r["payment_id"] == doc["payment_id"]

    rows = [r for r in rows if of_doc(r)]
    by_role = {}
    for r in rows:
        by_role.setdefault(pub.role(r), []).append(r)

    def amt(r):
        return r["debit_minor"] or r["credit_minor"]

    if itype == "duplicate_invoice":
        if not is_inv:
            raise RecomputeError("duplicate_invoice needs an invoice document")
        exp = expected_invoice(pub, doc)["ar_dr"]
        for role in ("ar", "revenue"):
            rs = [r for r in by_role.get(role, [])
                  if (r["debit_minor"] if role == "ar" else r["credit_minor"]) > 0]
            if len({r["entry_id"] for r in rs}) >= 2:
                rs.sort(key=lambda r: (amt(r) != exp, parse_ts(r["timestamp"]), r["row_id"]))
                return sum(amt(r) for r in rs[1:])
        raise RecomputeError("set does not show two AR (or two revenue) postings of the "
                             "invoice in different entries")
    if itype == "payment_allocation":
        if is_inv or doc["amount_minor"] <= 0:
            raise RecomputeError("payment_allocation needs a receipt document")
        if sum(x["amount_minor"] for x in doc["invoice_refs"]) != doc["amount_minor"]:
            raise RecomputeError("payment has a legitimate advance (Σrefs < amount)")
        un = [r for r in by_role.get("unapplied", []) if r["credit_minor"] > 0]
        if un:
            return sum(r["credit_minor"] for r in un)
        bank = [r for r in by_role.get("bank", []) if r["debit_minor"] > 0]
        if bank:
            ar = [r for r in by_role.get("ar", []) if r["credit_minor"] > 0]
            v = _dr(bank) - _cr(ar)
            if v > 0:
                return v
        raise RecomputeError("set has neither unapplied rows nor a bank row exceeding AR")
    if itype == "fx_conversion":
        cur = doc["currency"]
        if cur == pub.base:
            raise RecomputeError("document is in base currency")
        ts = doc["issued_at"] if is_inv else doc["timestamp"]
        want_key = fx_key(pub.local_date(ts), cur, pub.base)
        if want_key not in fxk:
            raise RecomputeError(f"set lacks the correct-date FX key {want_key}")
        m = FX_KEY_RE.match(want_key)
        rate = pub.fx.get((m.group(1), cur))
        if rate is None:
            raise RecomputeError(f"{want_key} not in fx.csv")
        src = doc["total_minor"] if is_inv else abs(doc["amount_minor"])
        expected = convert(src, rate, pub.exps[cur], pub.exps[pub.base], pub.rounding_mode)
        if is_inv:
            cand = [r for r in by_role.get("ar", []) if r["debit_minor"]] or \
                   [r for r in by_role.get("revenue", []) if r["credit_minor"]]
        else:
            side = "debit_minor" if doc["amount_minor"] > 0 else "credit_minor"
            cand = [r for r in by_role.get("bank", []) if r[side]]
        if not cand:
            raise RecomputeError("set lacks the posting row (AR/revenue or bank)")
        return abs(amt(cand[0]) - expected)
    if itype == "wrong_period":
        if not is_inv:
            raise RecomputeError("wrong_period needs an invoice document")
        issued_in = pub.in_period(doc["issued_at"])
        for role in ("ar", "revenue"):
            for r in by_role.get(role, []):
                if pub.in_period(r["timestamp"]) != issued_in:
                    return amt(r)
        raise RecomputeError("no row in the set is posted on the other side of the period "
                             "boundary from issued_at")
    raise RecomputeError(f"unknown type {itype}")
