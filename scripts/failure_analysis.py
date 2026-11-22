"""Measured failure analysis: which planted errors each policy finds, and why its other findings fail.

    python3 scripts/failure_analysis.py episodes --split dev

Runs the scripted policies through the same runner and grader as the release, then explains each
finding with the playground's per-finding review (it mirrors ``score_submission``). Interacting
errors are recognised from the generator's provenance; legitimate look-alikes (split payments,
refunds, advances, repeated amounts, UTC-boundary events) from its distractor list. Only the
trusted side reads those private files; the output contains counts, not answers.
"""

import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from audit_sim.environment import AuditEnv  # noqa: E402
from audit_sim.runner import run_policy  # noqa: E402
from playground.server import review  # noqa: E402

TYPES = ("duplicate_invoice", "payment_allocation", "fx_conversion", "wrong_period")
POLICIES = ("reconcile", "simple", "random", "mark_all")
REASONS = (
    ("unknown_evidence", "cites IDs that do not exist"),
    ("distractor", "flags a legitimate look-alike (split payment, refund, advance, repeat, UTC boundary)"),
    ("no_error", "no error of this type on these documents"),
    ("wrong_amount", "right error and document, wrong amount"),
    ("incomplete_evidence", "right error, document and amount, evidence incomplete"),
    ("duplicate", "repeats an earlier finding"),
    ("already_credited", "error already credited to another finding"),
)


def reason(detail, finding, distractor_docs):
    if detail.startswith("Evidence IDs not found"):
        return "unknown_evidence"
    if detail.startswith("No error of this type"):
        return "distractor" if set(finding["affected_ids"]) & distractor_docs else "no_error"
    if "amount is wrong" in detail:
        return "wrong_amount"
    if "evidence is incomplete" in detail:
        return "incomplete_evidence"
    return "already_credited"


def analyse(root, split, policies=POLICIES):
    public_root, private_root = Path(root) / "public" / split, Path(root) / "private" / split
    found = defaultdict(Counter)   # policy -> category -> found
    totals = Counter()             # category -> planted
    fps = defaultdict(Counter)     # policy -> reason -> count
    runs = defaultdict(Counter)    # policy -> tp/fp/fn/episodes/invalid/truncated
    for public in sorted(path for path in public_root.iterdir() if path.is_dir()):
        private = private_root / public.name
        provenance = json.loads((private / "provenance.json").read_text(encoding="utf-8"))
        interacting = {change["issue_id"] for change in provenance.get("changes", [])
                       if "combo " in change.get("cause", "")}
        distractor_docs = {doc for item in provenance.get("distractors", []) for doc in item.get("doc_ids", [])}
        env = AuditEnv()
        env.reset(None, {"episode": str(public), "private": str(private)})
        for issue in env.episode.issues:
            totals[issue["type"]] += 1
            totals["interacting" if issue["issue_id"] in interacting else "single"] += 1
        for policy in policies:
            report, _ = run_policy(str(public), policy, private_directory=str(private))
            counts = runs[policy]
            counts["episodes"] += 1
            counts["invalid"] += report["invalid_submission"]
            counts["truncated"] += report["truncated"]
            for name in ("tp", "fp", "fn"):
                counts[name] += report[name]
            payload = {"episode_id": env.episode_id, "findings": report["submitted_findings"]}
            explained = review(env, payload)
            for issue in explained["issues"]:
                if issue["found_by"] is not None:
                    found[policy][issue["type"]] += 1
                    found[policy]["interacting" if issue["issue_id"] in interacting else "single"] += 1
            for finding in explained["findings"]:
                verdict = finding["verdict"]
                if verdict["status"] == "duplicate":
                    fps[policy]["duplicate"] += 1
                elif verdict["status"] == "wrong":
                    fps[policy][reason(verdict["detail"], finding, distractor_docs)] += 1
            if sum(fps[policy].values()) != counts["fp"] and not report["invalid_submission"]:
                raise AssertionError(f"{policy} on {public.name}: explained FPs differ from the grader")
    return {"totals": totals, "found": found, "false_positives": fps, "runs": runs}


def markdown(result, split, policies=POLICIES):
    totals, found, fps, runs = (result[key] for key in ("totals", "found", "false_positives", "runs"))
    lines = [f"Recall by error type ({split}, closed mode; found / planted):", "",
             "| Errors | Planted | " + " | ".join(f"`{p}`" for p in policies) + " |",
             "|---|---:|" + "---:|" * len(policies)]
    for category in (*TYPES, "single", "interacting"):
        if totals[category]:
            cells = [f"{found[p][category]}/{totals[category]}" for p in policies]
            lines.append(f"| {category} | {totals[category]} | " + " | ".join(cells) + " |")
    lines += ["", "Why findings were not credited (count of false positives):", "",
              "| Reason | " + " | ".join(f"`{p}`" for p in policies) + " |",
              "|---|" + "---:|" * len(policies)]
    for name, label in REASONS:
        if any(fps[p][name] for p in policies):
            lines.append(f"| {label} | " + " | ".join(str(fps[p][name]) for p in policies) + " |")
    lines.append("| **total FP** | " + " | ".join(f"**{runs[p]['fp']}**" for p in policies) + " |")
    lines.append("| invalid submissions / truncated runs | "
                 + " | ".join(f"{runs[p]['invalid']} / {runs[p]['truncated']}" for p in policies) + " |")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("episodes", nargs="?", default=str(ROOT / "episodes"))
    parser.add_argument("--split", default="dev")
    parser.add_argument("--policies", nargs="+", default=list(POLICIES), choices=POLICIES)
    args = parser.parse_args(argv)
    print(markdown(analyse(args.episodes, args.split, args.policies), args.split, args.policies))


if __name__ == "__main__":
    main()
