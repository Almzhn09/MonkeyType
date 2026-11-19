"""Command line for generating integration fixtures and running offline evaluation."""

import argparse
import json
from pathlib import Path
from statistics import mean
import sys
import tempfile
import unittest

from .data import load_json
from .fixtures import dump_json, dump_jsonl, make_dev
from .policies import POLICIES
from .runner import WorkerProtocolError, run_policy


def episodes(directory):
    if (directory / "public" / "manifest.json").is_file():
        return [directory]
    found = sorted(path.parent.parent for path in directory.glob("*/public/manifest.json"))
    if not found:
        raise ValueError(f"No episode manifests at {directory}")
    return found


def summarize(reports, choices):
    metrics = {}
    for policy in choices:
        group = [item for item in reports if item["policy"] == policy]
        metrics[policy] = {
            "episodes": len(group),
            "mean_precision": mean(item["precision"] for item in group),
            "mean_recall": mean(item["recall"] for item in group),
            "mean_f1": mean(item["f1"] for item in group),
            "mean_return": mean(item["reward"] for item in group),
            "mean_steps": mean(item["steps"] for item in group),
            "mean_seconds": mean(item["duration_seconds"] for item in group),
            "truncated": sum(item["truncated"] for item in group),
            "invalid_submissions": sum(item["invalid_submission"] for item in group),
        }
    return metrics


def evaluate(directory, *, choices=POLICIES, mode="closed", policy_seed=0,
             timeout=60.0, tolerances=None, output=None):
    reports = []
    trajectories = []
    for episode in episodes(directory):
        for policy in choices:
            report, trajectory = run_policy(
                episode, policy, mode=mode, policy_seed=policy_seed,
                timeout=timeout, tolerances=tolerances,
            )
            reports.append(report)
            trajectories.extend({"policy": policy, **row} for row in trajectory)
    summary = summarize(reports, choices)
    if output is not None:
        output.mkdir(parents=True, exist_ok=True)
        dump_json(output / "summary.json", summary)
        dump_json(output / "reports.json", reports)
        dump_jsonl(output / "trajectories.jsonl", trajectories)
    return reports, summary, trajectories


def demo(output):
    with tempfile.TemporaryDirectory() as temp:
        directory = Path(temp)
        make_dev(directory, demo=True)
        reports, _, trajectories = evaluate(directory, choices=("reconcile", "mark_all"), output=output)
    # Display only the same public documents and records used as agent evidence.
    public_records = {}
    for row in trajectories:
        if row["policy"] != "reconcile" or row["action"]["action"] != "list_records":
            continue
        for record in row["observation"]["result"]:
            evidence_id = record.get("doc_id") or record["row_id"]
            public_records[row["episode_id"], evidence_id] = record
    for report in sorted(reports, key=lambda item: item["episode_id"] != "demo-mixed"):
        data = {key: report[key] for key in (
            "episode_id", "policy", "steps", "tp", "fp", "fn", "f1", "reward",
        )}
        if report["policy"] == "reconcile":
            findings = report["submitted_findings"]
            data["submitted_findings"] = findings
            data["evidence_records"] = {
                entry: public_records[report["episode_id"], entry]
                for item in findings for entry in item["evidence_ids"]
            }
        print(json.dumps(data, ensure_ascii=False, indent=2))


def verify():
    root = Path(__file__).resolve().parent.parent
    suite = unittest.defaultTestLoader.discover(str(root / "tests"))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful() or not result.testsRun:
        return 1

    with tempfile.TemporaryDirectory() as temp:
        directory = Path(temp)
        make_dev(directory)
        reports, summary, _ = evaluate(directory)
    oracle = summary["oracle"]
    mixed = next(item for item in reports if item["episode_id"] == "demo-mixed" and item["policy"] == "reconcile")
    clean = {item["policy"]: item for item in reports if item["episode_id"] == "demo-clean"}
    if (
        oracle["episodes"] != 10 or oracle["mean_f1"] != 1 or mixed["tp"] != 2
        or clean["reconcile"]["reward"] <= clean["mark_all"]["reward"]
        or any(item["truncated"] or item["invalid_submission"] for item in reports)
    ):
        print("Integration check did not meet the expected results", file=sys.stderr)
        return 1
    print(json.dumps(summary, indent=2))
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    fixtures = commands.add_parser("fixtures", help="Generate the 10 integration/dev episodes")
    fixtures.add_argument("directory", type=Path)
    run = commands.add_parser("run", help="Evaluate an episode or a directory of episodes")
    run.add_argument("directory", type=Path)
    run.add_argument("--policy", choices=(*POLICIES, "all"), default="all")
    run.add_argument("--mode", choices=("closed", "train"), default="closed")
    run.add_argument("--policy-seed", type=int, default=0)
    run.add_argument("--timeout", type=float, default=60.0)
    run.add_argument("--tolerances", type=Path)
    run.add_argument("--output", type=Path)
    demo_parser = commands.add_parser("demo", help="Show two paired terminal demonstrations")
    demo_parser.add_argument("--output", type=Path)
    commands.add_parser("verify", help="Run tests and the dev integration check")
    args = parser.parse_args()

    try:
        if args.command == "fixtures":
            make_dev(args.directory)
        elif args.command == "run":
            _, summary, _ = evaluate(
                args.directory, choices=POLICIES if args.policy == "all" else (args.policy,),
                mode=args.mode, policy_seed=args.policy_seed, timeout=args.timeout,
                tolerances=load_json(args.tolerances) if args.tolerances else None,
                output=args.output,
            )
            print(json.dumps(summary, indent=2))
        elif args.command == "demo":
            demo(args.output)
        elif args.command == "verify":
            return verify()
    except (ValueError, OSError, KeyError, WorkerProtocolError) as error:
        print(f"audit_sim: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
