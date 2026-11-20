"""Deterministic one-to-one evidence matching and the common Genesis reward."""

from dataclasses import asdict, dataclass

from fincheck.load import canon_ids

from .schema import ISSUE_TYPES, V1_ISSUE_TYPES, is_currency, valid_ids

AMOUNT_TYPES = frozenset((*ISSUE_TYPES, *V1_ISSUE_TYPES))


@dataclass(frozen=True)
class Score:
    tp: int
    fp: int
    fn: int
    precision: float
    recall: float
    f1: float
    reward: float
    invalid_submission: bool = False


def validate_submission(payload, episode_id):
    if not isinstance(payload, dict) or payload.get("episode_id") != episode_id:
        return False
    findings = payload.get("findings")
    if not isinstance(findings, list) or len(findings) > 30:
        return False
    return all(
        isinstance(item, dict)
        and isinstance(item.get("type"), str)
        and valid_ids(item.get("affected_ids"))
        and type(item.get("claimed_amount_minor")) is int
        and is_currency(item.get("currency"))
        and valid_ids(item.get("evidence_ids"), minimum=0, maximum=10)
        and isinstance(item.get("explanation"), str)
        and len(item["explanation"]) <= 1000
        for item in findings
    )


def canonical_ids(ids):
    return tuple(canon_ids(ids))


def key(item):
    return item["type"], canonical_ids(item["affected_ids"]), item["currency"]


def candidate_graph(findings, issues, evidence_ids, tolerances):
    """Rows are in submission order; columns in issue ID order."""
    if not findings:
        return [], []
    issue_keys = [key(issue) for issue in issues]
    available = frozenset(value.strip().upper() for value in evidence_ids)
    requirements = [[frozenset(canonical_ids(ids)) for ids in issue["evidence_sets"]] for issue in issues]
    graph = []
    rejection = []
    for finding in findings:
        evidence = frozenset(canonical_ids(finding["evidence_ids"]))
        if not evidence <= available:
            graph.append([])
            rejection.append("unknown_evidence")
            continue
        kind, affected, currency = key(finding)
        targeted = [i for i, issue in enumerate(issue_keys) if issue[:2] == (kind, affected)]
        same_currency = [i for i in targeted if issue_keys[i][2] == currency]
        amount_matched = [i for i in same_currency if amount_matches(finding, issues[i], tolerances)]
        candidates = [i for i in amount_matched if any(needed <= evidence for needed in requirements[i])]
        graph.append(candidates)
        reason = "already_matched"
        for population, failure in (
            (targeted, "no_matching_issue"), (same_currency, "wrong_currency"),
            (amount_matched, "amount_outside_tolerance"), (candidates, "incomplete_evidence"),
        ):
            if not population:
                reason = failure
                break
        rejection.append(reason)
    return graph, rejection


def amount_matches(finding, issue, tolerances):
    error = abs(finding["claimed_amount_minor"] - issue["expected_amount_minor"])
    return error <= tolerances.get(issue["type"], issue.get("tolerance_minor", 0))


def match_assignments(graph):
    """Use augmenting paths so a flexible match cannot block a more specific one."""
    owners = {}

    def assign(finding, visited):
        for issue in graph[finding]:
            if issue in visited:
                continue
            visited.add(issue)
            if issue not in owners or assign(owners[issue], visited):
                owners[issue] = finding
                return True
        return False

    for finding in range(len(graph)):
        assign(finding, set())
    return owners


def maximum_matches(graph):
    return len(match_assignments(graph))


def assess_submission(episode, payload, inspection_steps, tolerances=None):
    """Compute the common score and its private evidence-matching decisions."""
    issues = sorted(episode.issues, key=lambda issue: issue["issue_id"])
    if not validate_submission(payload, episode.manifest["episode_id"]):
        return Score(0, 0, len(issues), 0.0, 0.0, 0.0, -1.0, invalid_submission=True), [], issues

    tolerances = {} if tolerances is None else tolerances
    findings = payload["findings"]
    unique = []
    original_indexes = []
    seen = {}
    decisions = []
    for index, finding in enumerate(findings):
        signature = (
            key(finding),
            finding["claimed_amount_minor"],
            canonical_ids(finding["evidence_ids"]),
        )
        if signature in seen:
            decisions.append({"index": index, "status": "duplicate", "duplicate_of": seen[signature]})
        else:
            unique.append(finding)
            original_indexes.append(index)
            seen[signature] = index
            decisions.append({"index": index})
    graph, rejections = candidate_graph(unique, issues, episode.evidence_ids, tolerances)
    owners = match_assignments(graph)
    pairs = {finding: issue for issue, finding in owners.items()}
    for position, original in enumerate(original_indexes):
        if position in pairs:
            decisions[original].update(status="matched", issue_id=issues[pairs[position]]["issue_id"])
        else:
            decisions[original]["status"] = rejections[position]
    tp = len(owners)
    fp = len(findings) - tp
    fn = len(issues) - tp
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * tp / (2 * tp + fp + fn) if tp + fp + fn else 1.0
    reward = max(-1.0, min(1.0, 2 * f1 - 1 - 0.002 * inspection_steps))
    missing = [issue for index, issue in enumerate(issues) if index not in owners]
    return Score(tp, fp, fn, precision, recall, f1, reward), decisions, missing


def score_submission(episode, payload, inspection_steps, tolerances=None):
    score, _, _ = assess_submission(episode, payload, inspection_steps, tolerances)
    return score


def explain_submission(episode, payload, inspection_steps, tolerances=None):
    score, decisions, missing = assess_submission(episode, payload, inspection_steps, tolerances)
    return {
        "score": asdict(score),
        "findings": decisions,
        "missed": [{"issue_id": issue["issue_id"], "type": issue["type"]} for issue in missing],
    }


def oracle_submission(episode):
    return {
        "episode_id": episode.manifest["episode_id"],
        "findings": [
            {
                "type": issue["type"],
                "affected_ids": list(issue["affected_ids"]),
                "claimed_amount_minor": issue["expected_amount_minor"],
                "currency": issue["currency"],
                "evidence_ids": list(issue["evidence_sets"][0]),
                "explanation": "Evaluator control.",
            }
            for issue in episode.issues
        ],
    }
