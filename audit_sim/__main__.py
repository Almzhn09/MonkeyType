"""Command line for generating integration fixtures and running offline evaluation."""

import argparse
import json
from pathlib import Path
from shutil import copytree
from statistics import mean
import sys
import tempfile
import unittest

from fincheck.load import SchemaError, fx_key
from fincheck.verify import discover

from .data import load_json
from .fixtures import dump_json, dump_jsonl, make_dev
from .policies import POLICIES
from .release import ROOT, prepare_release
from .runner import WorkerProtocolError, run_policy


def episodes(directory, *, private_directory=None, split=None):
    if private_directory is not None:
        if split is not None:
            raise ValueError("Select either --private or --split")
        return [(directory, private_directory)]
    if (directory / "public" / "manifest.json").is_file():
        if split is not None:
            raise ValueError("--split requires a dataset root")
        return [(directory, None)]
    if (directory / "public").is_dir() or (directory / "private").is_dir():
        pairs, problems = discover(str(directory))
        if problems:
            raise ValueError("\n".join(problems))
        found = [(Path(pub), Path(prv)) for pub, prv in pairs if split is None or Path(pub).parent.name == split]
    else:
        if split is not None:
            raise ValueError("--split requires a dataset root")
        found = [(path.parent.parent, None) for path in sorted(directory.glob("*/public/manifest.json"))]
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
             timeout=60.0, tolerances=None, output=None, split=None, private_directory=None):
    reports = []
    trajectories = []
    for episode, private in episodes(directory, split=split, private_directory=private_directory):
        for policy in choices:
            report, trajectory = run_policy(
                episode, policy, private_directory=private, mode=mode, policy_seed=policy_seed,
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
    control = ROOT / "fincheck" / "fixtures" / "a08"
    clean = "EP-QRS8HS6811"
    with tempfile.TemporaryDirectory() as temp:
        directory = Path(temp)
        for scope in ("public", "private"):
            copytree(control / scope, directory / scope / "demo" / "EP-CHK-A08")
            copytree(ROOT / "episodes" / scope / "dev" / clean, directory / scope / "demo" / clean)
        reports, _, trajectories = evaluate(
            directory, split="demo", choices=("reconcile", "mark_all"), output=output,
        )
    # Display only the same public documents and records used as agent evidence.
    public_records = {}
    for row in trajectories:
        if row["policy"] != "reconcile" or row["action"]["action"] != "list_records":
            continue
        for record in row["observation"]["result"]:
            evidence_id = record.get("doc_id") or record.get("row_id")
            if evidence_id is None:
                evidence_id = fx_key(record["date"], record["source_currency"], record["target_currency"])
            public_records[row["episode_id"], evidence_id] = record
    for report in reports:
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
    run.add_argument("--split", help="Select a split under public/ and private/")
    run.add_argument("--private", type=Path, help="Private directory when the positional path is public")
    run.add_argument("--policy", choices=(*POLICIES, "all"), default="all")
    run.add_argument("--mode", choices=("closed", "train"), default="closed")
    run.add_argument("--policy-seed", type=int, default=0)
    run.add_argument("--timeout", type=float, default=60.0)
    run.add_argument("--tolerances", type=Path)
    run.add_argument("--output", type=Path)
    demo_parser = commands.add_parser("demo", help="Show two paired terminal demonstrations")
    demo_parser.add_argument("--output", type=Path)
    commands.add_parser("verify", help="Run tests and the dev integration check")
    release = commands.add_parser("release", help="Verify and optionally export all submission data")
    release.add_argument("--output", type=Path)
    args = parser.parse_args()

    try:
        if args.command == "fixtures":
            make_dev(args.directory)
        elif args.command == "run":
            _, summary, _ = evaluate(
                args.directory, choices=POLICIES if args.policy == "all" else (args.policy,),
                mode=args.mode, policy_seed=args.policy_seed, timeout=args.timeout,
                tolerances=load_json(args.tolerances) if args.tolerances else None,
                output=args.output, split=args.split, private_directory=args.private,
            )
            print(json.dumps(summary, indent=2))
        elif args.command == "demo":
            demo(args.output)
        elif args.command == "verify":
            return verify()
        elif args.command == "release":
            print(json.dumps(prepare_release(evaluate, args.output), indent=2))
    except (ValueError, OSError, KeyError, SchemaError, WorkerProtocolError) as error:
        print(f"audit_sim: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
