"""Reference matcher (CONTRACT §5/§7, alimzhan B10–B12). The real matcher is owned by the
environment side; this stub exists so the verifier can prove truth is reachable.

TP(finding, issue) iff: same type; canonical affected_ids equal; same currency;
|claimed − expected| ≤ tolerance; canonical evidence ⊇ some evidence_set; every evidence
id exists in the public ID space (incl. FX keys); ≤ 10 distinct evidence ids.
Matching is one-to-one, maximum cardinality (augmenting paths), deterministic: issues are
processed in issue_id order; each takes the earliest free candidate finding (submission
order) and earlier assignments are re-routed only when that increases the number of TPs. Unmatched / repeated findings are FP, unmatched issues FN.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .load import canon_ids, is_int

MAX_EVIDENCE = 10


def match_reason(finding, issue, id_space) -> str | None:
    """None if ``finding`` is a TP for ``issue``; otherwise a short reason."""
    if not isinstance(finding, dict):
        return "finding is not an object"
    try:
        if finding.get("type") != issue["type"]:
            return "type differs"
        aff = finding.get("affected_ids")
        if not isinstance(aff, list) or canon_ids(aff) != canon_ids(issue["affected_ids"]):
            return "affected_ids differ"
        if finding.get("currency") != issue["currency"]:
            return "currency differs"
        claimed = finding.get("claimed_amount_minor")
        if not is_int(claimed):
            return "claimed_amount_minor is not an integer"
        if abs(claimed - issue["expected_amount_minor"]) > issue["tolerance_minor"]:
            return (f"amount {claimed} vs expected {issue['expected_amount_minor']} "
                    f"(tolerance {issue['tolerance_minor']})")
        ev = finding.get("evidence_ids")
        if not isinstance(ev, list) or not all(isinstance(x, str) for x in ev):
            return "evidence_ids is not a list of strings"
        ev = set(canon_ids(ev))
        if len(ev) > MAX_EVIDENCE:
            return f"{len(ev)} evidence ids > {MAX_EVIDENCE}"
        missing = sorted(ev - id_space)
        if missing:
            return f"nonexistent evidence ids {missing}"
        if not any(set(canon_ids(s)) <= ev for s in issue["evidence_sets"]):
            return "no evidence set fully cited"
    except (TypeError, AttributeError) as exc:
        return f"malformed finding ({exc.__class__.__name__})"
    return None


@dataclass
class MatchResult:
    pairs: list = field(default_factory=list)  # (finding_index, issue_id)
    tp: int = 0
    fp: int = 0
    fn: int = 0
    precision: float = 0.0
    recall: float = 0.0
    f1: float = 0.0
    unmatched_findings: list = field(default_factory=list)  # indices
    unmatched_issues: list = field(default_factory=list)  # issue_ids


def scores(tp: int, fp: int, fn: int):
    """B12: zero denominator ⇒ P or R = 0; F1 = 1 when there is nothing to find and
    nothing was claimed."""
    p = tp / (tp + fp) if tp + fp else 0.0
    r = tp / (tp + fn) if tp + fn else 0.0
    d = 2 * tp + fp + fn
    return p, r, (2 * tp / d if d else 1.0)


def match(findings, issues, id_space) -> MatchResult:
    findings = list(findings or [])
    issues = sorted(issues, key=lambda i: i["issue_id"])
    adj = [[fi for fi, f in enumerate(findings) if match_reason(f, iss, id_space) is None]
           for iss in issues]
    owner = {}  # finding index -> issue index

    def augment(ii, seen):
        # prefer the earliest free finding; only then re-route earlier assignments
        for fi in adj[ii]:
            if fi not in owner and fi not in seen:
                seen.add(fi)
                owner[fi] = ii
                return True
        for fi in adj[ii]:
            if fi in seen:
                continue
            seen.add(fi)
            if augment(owner[fi], seen):
                owner[fi] = ii
                return True
        return False

    for ii in range(len(issues)):
        augment(ii, set())
    pairs = sorted((fi, issues[ii]["issue_id"]) for fi, ii in owner.items())
    tp = len(pairs)
    res = MatchResult(pairs=pairs, tp=tp, fp=len(findings) - tp, fn=len(issues) - tp)
    res.precision, res.recall, res.f1 = scores(res.tp, res.fp, res.fn)
    res.unmatched_findings = [i for i in range(len(findings)) if i not in owner]
    matched = set(owner.values())
    res.unmatched_issues = [iss["issue_id"] for ii, iss in enumerate(issues)
                            if ii not in matched]
    return res


def explain_unmatched(res: MatchResult, findings, issues, id_space) -> list:
    """Human-readable reasons for every unmatched finding / issue (best candidate)."""
    out = []
    by_id = {i["issue_id"]: i for i in issues}
    for fi in res.unmatched_findings:
        f = findings[fi]
        reasons = [f"{i['issue_id']}: {match_reason(f, i, id_space) or 'already matched'}"
                   for i in issues if isinstance(f, dict) and i["type"] == f.get("type")]
        out.append(f"FP finding #{fi} {f.get('type') if isinstance(f, dict) else f!r} "
                   f"{f.get('affected_ids') if isinstance(f, dict) else ''}: "
                   f"{'; '.join(reasons) or 'no issue of this type'}")
    for iid in res.unmatched_issues:
        iss = by_id[iid]
        reasons = [f"#{fi}: {match_reason(f, iss, id_space)}" for fi, f in enumerate(findings)
                   if isinstance(f, dict) and f.get("type") == iss["type"]]
        out.append(f"FN issue {iid} {iss['type']} {iss['affected_ids']} expected "
                   f"{iss['expected_amount_minor']}: {'; '.join(reasons) or 'no finding'}")
    return out
