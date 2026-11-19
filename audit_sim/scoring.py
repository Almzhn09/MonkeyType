"""Deterministic one-to-one evidence matching and the common Genesis reward."""

from dataclasses import dataclass

from .schema import ISSUE_TYPES, is_currency, valid_ids

DEFAULT_TOLERANCES = dict.fromkeys(ISSUE_TYPES, 0)


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
    return tuple(sorted(set(ids)))


def key(item):
    return item["type"], canonical_ids(item["affected_ids"]), item["currency"]


def candidate_graph(findings, issues, evidence_ids, tolerances):
    """Rows are in submission order; columns in issue ID order."""
    issue_keys = [key(issue) for issue in issues]
    requirements = [[frozenset(ids) for ids in issue["evidence_sets"]] for issue in issues]
    graph = []
    for finding in findings:
        evidence = frozenset(finding["evidence_ids"])
        finding_key = key(finding)
        if not evidence <= evidence_ids or finding["type"] not in tolerances:
            graph.append([])
            continue
        graph.append([
            index
            for index, issue in enumerate(issues)
            if finding_key == issue_keys[index]
            and abs(finding["claimed_amount_minor"] - issue["expected_amount_minor"]) <= tolerances[issue["type"]]
            and any(needed <= evidence for needed in requirements[index])
        ])
    return graph


def maximum_matches(graph):
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
    return len(owners)


def score_submission(episode, payload, inspection_steps, tolerances=None):
    if not validate_submission(payload, episode.manifest["episode_id"]):
        return Score(0, 0, len(episode.issues), 0.0, 0.0, 0.0, -1.0, invalid_submission=True)

    tolerances = DEFAULT_TOLERANCES if tolerances is None else tolerances
    findings = payload["findings"]
    issues = sorted(episode.issues, key=lambda issue: issue["issue_id"])
    unique = []
    seen = set()
    for finding in findings:
        signature = (
            key(finding),
            finding["claimed_amount_minor"],
            canonical_ids(finding["evidence_ids"]),
        )
        if signature not in seen:
            unique.append(finding)
            seen.add(signature)
    graph = candidate_graph(unique, issues, episode.evidence_ids, tolerances)
    tp = maximum_matches(graph)
    fp = len(findings) - tp
    fn = len(issues) - tp
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * tp / (2 * tp + fp + fn) if tp + fp + fn else 1.0
    reward = max(-1.0, min(1.0, 2 * f1 - 1 - 0.002 * inspection_steps))
    return Score(tp, fp, fn, precision, recall, f1, reward)


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
