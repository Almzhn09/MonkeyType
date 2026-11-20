"""Per-episode verification (alan A07/A08/A15; jury criterion "Данные и эталон").

(a) schema of public + private files            (fincheck.load)
(b) baseline accounting equalities: undo provenance changes in reverse order, then every
    entry balances, refs exist, every posting equals §4 for its document, the reconciler
    finds nothing, baseline_sha256 agrees
(c) each truth issue: amount recomputed from public files == expected (± tolerance), all
    ids exist publicly, every evidence set is sufficient on its own
(d) reconciler findings == truth via the reference matcher (F1 = 1) and no discrepancy
    outside the four §5 types
(e) clean episodes: no issues, no changes, nothing found
(f) leakage: no truth/provenance-only strings, no absolute paths, no extra public files
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field

from .load import (HASHED_FILES, PUBLIC_FILES, SchemaError, canon_ids,
                   check_ledger_integrity, load_private, load_public, normalize_row,
                   render_ledger, sha256_bytes)
from .matcher_stub import explain_unmatched, match
from .recompute import RecomputeError, amount_from_evidence, audit, reconcile

# Separators delimit marker words; opaque alphanumeric IDs can include their letters by chance.
LEAK_WORDS = re.compile(
    r"(?<![A-Za-z0-9])(?:duplicat(?:e[ds]?|ions?|ing|ors?)|"
    r"inject(?:ed|ing|ions?|ors?|s)?|errors?|truths?|provenance|distractors?|mistakes?|wrong|"
    r"oracles?|tamper(?:ed|ing|s)?|expected_amount(?:_minor)?|evidence_sets?|issue_ids?)"
    r"(?![A-Za-z0-9])", re.IGNORECASE,
)
ABS_PATH = re.compile(r"(^/|^~/|[A-Za-z]:\\|file:|/home/|/tmp/|/Users/|/root/|/var/|/mnt/)")


@dataclass
class Report:
    episode_id: str
    public_dir: str
    private_dir: str
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    issues: int = 0
    findings: int = 0
    f1: float | None = None
    findings_list: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def format(self, verbose=True) -> str:
        f1 = "n/a" if self.f1 is None else f"{self.f1:.3f}"
        head = (f"{'PASS' if self.ok else 'FAIL'} {self.episode_id}  issues={self.issues} "
                f"findings={self.findings} F1={f1}  errors={len(self.errors)} "
                f"warnings={len(self.warnings)}")
        lines = [head]
        if verbose or not self.ok:
            lines += [f"    - {e}" for e in self.errors[:40]]
            if len(self.errors) > 40:
                lines.append(f"    - ... {len(self.errors) - 40} more")
            lines += [f"    ! {w}" for w in self.warnings[:10]]
        return "\n".join(lines)


# --------------------------------------------------------------------------- (b)

def rebuild_baseline(pub, prov, errors, warnings=None) -> list | None:
    """Undo provenance changes (reverse `order`) on the published ledger."""
    rows = {r["row_id"]: r for r in pub.ledger}
    accounts = set(pub.role_of)
    ok = True
    for ch in sorted(prov["changes"], key=lambda c: c["order"], reverse=True):
        w = f"provenance change order={ch['order']} ({ch['issue_id']}, {ch['op']})"
        errs = []
        before = normalize_row(ch["before"], f"{w}.before", errs, pub.base, pub.offset,
                               accounts) if ch["before"] is not None else None
        after = normalize_row(ch["after"], f"{w}.after", errs, pub.base, pub.offset,
                              accounts) if ch["after"] is not None else None
        if errs:
            errors.extend(errs)
            ok = False
            continue
        rid = (after or before)["row_id"]
        if ch["target"] != rid and warnings is not None:
            warnings.append(f"{w}: target {ch['target']!r} != row_id {rid}")
        if after is not None:
            if rows.get(after["row_id"]) != after:
                errors.append(f"{w}: published ledger row {after['row_id']} does not equal "
                              f"`after`")
                ok = False
                continue
            del rows[after["row_id"]]
        if before is not None:
            if before["row_id"] in rows:
                errors.append(f"{w}: `before` row {before['row_id']} already exists")
                ok = False
                continue
            rows[before["row_id"]] = before
    return list(rows.values()) if ok else None


def _check_baseline(pub, prv, rep):
    errs = []
    base_rows = rebuild_baseline(pub, prv.provenance, errs, rep.warnings)
    rep.errors += [f"(b) {e}" for e in errs]
    if base_rows is None:
        return
    integ = check_ledger_integrity(base_rows, set(pub.invoice_by_iid),
                                   set(pub.payment_by_pid), "baseline ledger")
    rep.errors += [f"(b) {e}" for e in integ]
    if integ:
        return
    bpub = pub.with_ledger(base_rows)
    rep.errors += [f"(b) baseline: {p}" for p in audit(bpub, strict_all=True)]
    for f in reconcile(bpub):
        rep.errors += [f"(b) baseline already has a discrepancy: {f['type']} "
                       f"{f['affected_ids']} {f['claimed_amount_minor']}"]
    bs = prv.provenance["baseline_sha256"]
    # baseline_sha256[ledger.csv] is over the canonical form (rows sorted by
    # (timestamp, row_id), CONTRACT §6), so it is defined for permuted ledgers too; the
    # formatting check therefore compares lines regardless of their order.
    for name, h in sorted(bs.items()):
        if name == "ledger.csv":
            if sorted(render_ledger(pub.ledger).split(b"\n")) == \
                    sorted(pub.raw["ledger.csv"].split(b"\n")):
                if sha256_bytes(render_ledger(base_rows)) != h:
                    rep.errors.append("(b) baseline_sha256[ledger.csv] does not match the "
                                      "ledger rebuilt from provenance changes")
            else:
                rep.warnings.append("ledger.csv formatting not reproducible by fincheck; "
                                    "baseline_sha256[ledger.csv] not checked")
        elif name in HASHED_FILES:
            if pub.manifest["files"][name] != h:
                rep.errors.append(f"(b) baseline_sha256[{name}] differs from published file "
                                  f"but changes only touch ledger rows")
        elif name != "manifest.json":
            rep.warnings.append(f"baseline_sha256 lists unknown file {name}")


# --------------------------------------------------------------------------- (c)

def _doc_rows(pub, doc, is_inv):
    if is_inv:
        return [r for r in pub.ledger if r["invoice_id"] == doc["invoice_id"]
                and r["payment_id"] is None]
    return [r for r in pub.ledger if r["payment_id"] == doc["payment_id"]]


def _check_issues(pub, prv, findings, id_space, rep):
    issues = prv.truth["issues"]
    changes = prv.provenance["changes"]
    by_issue = {}
    for ch in changes:
        by_issue.setdefault(ch["issue_id"], []).append(ch)
    known = {i["issue_id"]: i for i in issues}
    for iid, chs in by_issue.items():
        if iid not in known:
            rep.errors.append(f"(c) provenance change for unknown issue {iid}")
            continue
        for ch in chs:
            if ch["type"] != known[iid]["type"]:
                rep.errors.append(f"(c) change order={ch['order']} type {ch['type']} != "
                                  f"truth {known[iid]['type']}")
    seen_keys = {}
    for iss in issues:
        w = f"(c) {iss['issue_id']} {iss['type']}"
        doc_id = iss["affected_ids"][0]
        doc = pub.docs.get(doc_id)
        if doc is None:
            rep.errors.append(f"{w}: affected id {doc_id} is not a public document")
            continue
        key = (iss["type"], doc_id)
        if key in seen_keys:
            rep.warnings.append(f"{iss['issue_id']} and {seen_keys[key]} share type and "
                                f"document; matching relies on amount/evidence")
        seen_keys[key] = iss["issue_id"]
        is_inv = doc_id in pub.invoices_by_doc
        if iss["type"] in ("duplicate_invoice", "wrong_period") and not is_inv:
            rep.errors.append(f"{w}: {iss['type']} must affect an invoice")
        if iss["type"] == "payment_allocation" and is_inv:
            rep.errors.append(f"{w}: payment_allocation must affect a payment")
        if iss["type"] == "payment_allocation" and not is_inv and \
                sum(r["amount_minor"] for r in doc["invoice_refs"]) != doc["amount_minor"]:
            rep.errors.append(f"{w}: injected into a payment with Σrefs != amount")
        if iss["type"] == "fx_conversion" and doc["currency"] == pub.base:
            rep.errors.append(f"{w}: fx_conversion on a base-currency document")
        if iss["expected_amount_minor"] <= 0:
            rep.errors.append(f"{w}: expected_amount_minor must be > 0 (no real discrepancy)")
        # scope: document event or its posting lies in the period
        ev_ts = doc["issued_at"] if is_inv else doc["timestamp"]
        if not (pub.in_period(ev_ts) or any(pub.in_period(r["timestamp"])
                                            for r in _doc_rows(pub, doc, is_inv))):
            rep.errors.append(f"{w}: neither the document event nor its posting is in the "
                              f"period")
        for s in iss["evidence_sets"]:
            missing = sorted(set(canon_ids(s)) - id_space)
            if missing:
                rep.errors.append(f"{w}: evidence set {s} cites non-public ids {missing}")
        if iss["issue_id"] not in by_issue:
            rep.errors.append(f"{w}: no provenance change recorded for this issue")
        for ch in by_issue.get(iss["issue_id"], []):
            for k in ("before", "after"):
                r = ch[k]
                if r is None:
                    continue
                rel = (r.get("invoice_id") == doc.get("invoice_id") and
                       r.get("payment_id") in (None, "")) if is_inv else \
                    r.get("payment_id") == doc.get("payment_id")
                if not rel:
                    rep.errors.append(f"{w}: change order={ch['order']} touches row "
                                      f"{r.get('row_id')} not belonging to {doc_id}")
        # independent recompute from the full public data
        amounts = [f["claimed_amount_minor"] for f in findings
                   if f["type"] == iss["type"] and f["affected_ids"] == [doc_id]]
        if not any(abs(a - iss["expected_amount_minor"]) <= iss["tolerance_minor"]
                   for a in amounts):
            rep.errors.append(f"{w}: recomputed amount(s) {amounts or 'none'} != expected "
                              f"{iss['expected_amount_minor']}")
        # sufficiency of every evidence set
        for s in iss["evidence_sets"]:
            try:
                a = amount_from_evidence(pub, iss["type"], doc_id, s)
            except RecomputeError as exc:
                rep.errors.append(f"{w}: evidence set {s} insufficient: {exc}")
                continue
            if abs(a - iss["expected_amount_minor"]) > iss["tolerance_minor"]:
                rep.errors.append(f"{w}: evidence set {s} yields {a}, expected "
                                  f"{iss['expected_amount_minor']}")


# --------------------------------------------------------------------------- (f)

def _walk_strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _walk_strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk_strings(v)


def _check_leakage(pub, prv, rep):
    extra = sorted(set(os.listdir(pub.root)) - set(PUBLIC_FILES))
    if extra:
        rep.errors.append(f"(f) public dir contains extra entries {extra}")
    values = list(_walk_strings(pub.manifest)) + list(_walk_strings(pub.policy))
    for d in pub.invoices + pub.payments:
        values += list(_walk_strings(d))
    for r in pub.ledger:
        values += [str(v) for v in r.values() if v is not None]
    for r in pub.fx_rows:
        values += list(r.values())
    bad = sorted({v for v in values if LEAK_WORDS.search(v)})
    if bad:
        rep.errors.append(f"(f) public values contain truth-like words: {bad[:5]}")
    paths = sorted({v for v in values if ABS_PATH.search(v)})
    if paths:
        rep.errors.append(f"(f) public values look like filesystem paths: {paths[:5]}")
    blob = b"\n".join(pub.raw.values())
    secrets = {i["issue_id"] for i in prv.truth["issues"]}
    secrets |= {c["cause"] for c in prv.provenance["changes"] if len(c["cause"]) >= 8}
    for s in sorted(secrets):
        if s.encode("utf-8") in blob:
            rep.errors.append(f"(f) private string {s!r} appears in public files")
    extra_priv = sorted(set(os.listdir(prv.root)) - {"truth.json", "provenance.json",
                                                     "id_map.json"})
    if extra_priv:
        rep.warnings.append(f"private dir has extra entries {extra_priv}")


# --------------------------------------------------------------------------- episode

def verify_episode(public_dir: str, private_dir: str, check_dirnames: bool = False) -> Report:
    """Run checks (a)–(f). ``check_dirnames``: require both directory names to equal the
    episode_id (CONTRACT §1 layout; enabled by verify_all)."""
    rep = Report(os.path.basename(os.path.normpath(public_dir)), public_dir, private_dir)
    pub = prv = None
    try:
        pub = load_public(public_dir)
    except SchemaError as exc:
        rep.errors += [f"(a) {e}" for e in exc.errors]
    try:
        prv = load_private(private_dir)
    except SchemaError as exc:
        rep.errors += [f"(a) private: {e}" for e in exc.errors]
    if pub is None or prv is None:
        return rep
    rep.warnings += pub.warnings + prv.warnings
    eid = pub.manifest["episode_id"]
    rep.episode_id = eid
    rep.issues = len(prv.truth["issues"])
    for name, obj in (("truth.json", prv.truth), ("provenance.json", prv.provenance)):
        if obj["episode_id"] != eid:
            rep.errors.append(f"(a) {name} episode_id {obj['episode_id']} != {eid}")
    if prv.truth["base_currency"] != pub.base:
        rep.errors.append("(a) truth base_currency differs from manifest")
    for d in (public_dir, private_dir) if check_dirnames else ():
        if os.path.basename(os.path.normpath(d)) != eid:
            rep.errors.append(f"(a) directory name {os.path.basename(os.path.normpath(d))!r}"
                              f" != episode_id {eid}")
    cfg = prv.provenance["config"]
    cands = [json.dumps(cfg, sort_keys=True, separators=sep, ensure_ascii=ea).encode()
             for sep in ((",", ":"), (", ", ": ")) for ea in (True, False)]
    cands += [c + b"\n" for c in cands]
    if prv.provenance["config_sha256"] not in {sha256_bytes(c) for c in cands}:
        rep.warnings.append("config_sha256 not reproducible from provenance.config with "
                            "sort_keys JSON")
    id_space = pub.id_space()

    _check_baseline(pub, prv, rep)

    findings = reconcile(pub)
    rep.findings = len(findings)
    rep.findings_list = findings
    _check_issues(pub, prv, findings, id_space, rep)

    issues = prv.truth["issues"]
    res = match(findings, issues, id_space)
    rep.f1 = res.f1
    if res.f1 != 1.0:
        rep.errors += [f"(d) {m}" for m in explain_unmatched(res, findings, issues, id_space)]
    rep.errors += [f"(d) unlabeled discrepancy: {p}" for p in audit(pub, findings)]

    if not issues:
        if prv.provenance["changes"]:
            rep.errors.append("(e) clean episode has provenance changes")
        if findings:
            rep.errors.append(f"(e) clean episode: reconciler found {len(findings)} issue(s)")

    _check_leakage(pub, prv, rep)
    rep._pub = pub  # for pair checks
    rep._prv = prv
    return rep


def check_pairs(reports) -> None:
    """A18: for paired episodes (id_map.json), the source episode's reconciler answer,
    renamed through id_map, must match the paired truth with F1 = 1."""
    by_id = {r.episode_id: r for r in reports if getattr(r, "_pub", None) is not None}
    for r in by_id.values():
        im = r._prv.id_map
        if im is None:
            continue
        src = by_id.get(im["source_episode_id"])
        if src is None:
            r.warnings.append(f"pair source {im['source_episode_id']} not among verified "
                              f"episodes")
            continue
        m = im["ids"]
        renamed = []
        for f in src.findings_list:
            g = dict(f)
            g["affected_ids"] = [m.get(x, x) for x in f["affected_ids"]]
            g["evidence_ids"] = [m.get(x, x) for x in f["evidence_ids"]]
            renamed.append(g)
        res = match(renamed, r._prv.truth["issues"], r._pub.id_space())
        if res.f1 != 1.0:
            r.errors.append(f"(pair) source {src.episode_id} answer renamed via id_map "
                            f"gives F1={res.f1:.3f}")
        if (len(r._pub.ledger), len(r._pub.invoices), len(r._pub.payments)) != \
                (len(src._pub.ledger), len(src._pub.invoices), len(src._pub.payments)):
            r.errors.append(f"(pair) record counts differ from {src.episode_id}")


def discover(root: str):
    """Yield (public_dir, private_dir) for episodes/{public,private}/<split>/<episode>."""
    pub_root = os.path.join(root, "public")
    prv_root = os.path.join(root, "private")
    pairs, problems = [], []
    splits = sorted(set(os.listdir(pub_root)) | set(os.listdir(prv_root))) \
        if os.path.isdir(pub_root) and os.path.isdir(prv_root) else []
    if not splits:
        problems.append(f"{root}: expected public/ and private/ split directories")
    for split in splits:
        a = os.path.join(pub_root, split)
        b = os.path.join(prv_root, split)
        eps_a = set(os.listdir(a)) if os.path.isdir(a) else set()
        eps_b = set(os.listdir(b)) if os.path.isdir(b) else set()
        for ep in sorted(eps_a ^ eps_b):
            problems.append(f"{split}/{ep}: present only in "
                            f"{'public' if ep in eps_a else 'private'}")
        for ep in sorted(eps_a & eps_b):
            pairs.append((os.path.join(a, ep), os.path.join(b, ep)))
    return pairs, problems


def verify_all(root: str):
    pairs, problems = discover(root)
    reports = [verify_episode(a, b, check_dirnames=True) for a, b in pairs]
    check_pairs(reports)
    return reports, problems
