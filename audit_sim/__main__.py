"""Command line for generating integration fixtures and running offline evaluation."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
from statistics import mean
import sys
import tempfile
import unittest

from fincheck.load import SchemaError, fx_key
from fincheck.verify import discover
from fingen.history import GenerationError
from fingen.inject import InjectionError

from .benchmark import benchmark
from .data import Episode, load_json
from .environment import AuditEnv, STEP_LIMIT
from .fixtures import dump_json, dump_jsonl, make_dev
from .package import build_package, verify_package
from .policies import POLICIES
from .release import evaluate_demo, prepare_release
from .replay import replay
from .report import report_directory, write_report
from .runner import WorkerProtocolError, run_loaded_policy
from .scoring import explain_submission
from .session import run_session


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


def episode_locations(directory, *, private_directory=None, split=None):
    available = {}
    for public, private in episodes(directory, private_directory=private_directory, split=split):
        manifest = public / "manifest.json" if private is not None else public / "public" / "manifest.json"
        episode_id = load_json(manifest)["episode_id"]
        if episode_id in available:
            raise ValueError(f"Duplicate episode ID in dataset: {episode_id}")
        available[episode_id] = public, private
    return available


def evaluate(directory, *, choices=POLICIES, mode="closed", policy_seed=0,
             timeout=60.0, tolerances=None, output=None, split=None, private_directory=None, jobs=1):
    if jobs < 1:
        raise ValueError("jobs must be positive")
    locations = episodes(directory, split=split, private_directory=private_directory)

    def evaluate_location(location):
        loaded = Episode.load(*location)
        return [run_loaded_policy(
            loaded, policy, mode=mode, policy_seed=policy_seed,
            timeout=timeout, tolerances=tolerances,
        ) for policy in choices]

    reports = []
    trajectories = []
    with ThreadPoolExecutor(max_workers=min(jobs, len(locations))) as executor:
        for batch in executor.map(evaluate_location, locations):
            for report, trajectory in batch:
                reports.append(report)
                trajectories.extend({"policy": report["policy"], **row} for row in trajectory)
    summary = summarize(reports, choices)
    if output is not None:
        output.mkdir(parents=True, exist_ok=True)
        dump_json(output / "summary.json", summary)
        dump_json(output / "reports.json", reports)
        dump_jsonl(output / "trajectories.jsonl", trajectories)
    return reports, summary, trajectories


def demo(output):
    reports, _, trajectories = evaluate_demo(evaluate, output=output)
    if output is not None:
        write_report(output / "report.html", reports, trajectories)
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
    run.add_argument("--jobs", type=int, default=1, help="Evaluate independent episodes concurrently")
    run.add_argument("--tolerances", type=Path)
    run.add_argument("--output", type=Path)
    replay_parser = commands.add_parser("replay", help="Re-execute a trajectory and verify the recorded results")
    replay_parser.add_argument("directory", type=Path)
    replay_parser.add_argument("trajectory", type=Path)
    replay_parser.add_argument("--private", type=Path)
    replay_parser.add_argument("--split")
    replay_parser.add_argument("--mode", choices=("closed", "train"))
    replay_parser.add_argument("--reports", type=Path)
    replay_parser.add_argument("--tolerances", type=Path)
    session = commands.add_parser("session", help="Run one episode over JSON lines on stdin/stdout")
    session.add_argument("directory", type=Path, nargs="?")
    session.add_argument("--private", type=Path)
    session.add_argument("--config", type=Path)
    session.add_argument("--seed", type=int)
    session.add_argument("--mode", choices=("closed", "train"), default="closed")
    session.add_argument("--timeout", type=float, default=60.0)
    session.add_argument("--tolerances", type=Path)
    session.add_argument("--output", type=Path)
    report = commands.add_parser("report", help="Render a saved run as a self-contained offline HTML report")
    report.add_argument("directory", type=Path)
    report.add_argument("--output", type=Path)
    score = commands.add_parser("score", help="Evaluate a saved submission with the production matcher")
    score.add_argument("directory", type=Path)
    score.add_argument("submission", type=Path)
    score.add_argument("--private", type=Path)
    score.add_argument("--inspection-steps", type=int, default=0)
    score.add_argument("--tolerances", type=Path)
    score.add_argument("--details", action="store_true", help="Include private matching diagnostics")
    bench = commands.add_parser("benchmark", help="Check generated holdout cohorts and transformed pairs")
    bench.add_argument("--seed", type=int, default=6217)
    bench.add_argument("--batches", type=int, default=1)
    bench.add_argument("--start-index", type=int, default=0)
    bench.add_argument("--progress-every", type=int, default=1)
    bench.add_argument("--output", type=Path)
    package = commands.add_parser("package", help="Create a ZIP from a checked release directory")
    package.add_argument("directory", type=Path)
    package.add_argument("archive", type=Path)
    verify_zip = commands.add_parser("verify-package", help="Verify all members and checksums in a release ZIP")
    verify_zip.add_argument("archive", type=Path)
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
                output=args.output, split=args.split, private_directory=args.private, jobs=args.jobs,
            )
            print(json.dumps(summary, indent=2))
        elif args.command == "demo":
            demo(args.output)
        elif args.command == "report":
            report_directory(args.directory, args.output or args.directory / "report.html")
        elif args.command == "score":
            if not 0 <= args.inspection_steps < STEP_LIMIT:
                raise ValueError(f"Inspection steps must be between 0 and {STEP_LIMIT - 1}")
            env = AuditEnv(tolerances=load_json(args.tolerances) if args.tolerances else None)
            env.reset(config={"episode": args.directory, "private": args.private})
            result = explain_submission(
                env.episode, load_json(args.submission), args.inspection_steps, env.tolerances,
            )
            print(json.dumps(result if args.details else result["score"], indent=2))
        elif args.command == "benchmark":
            result = benchmark(
                args.seed, args.batches, start_index=args.start_index,
                output=args.output, progress_every=args.progress_every,
            )
            print(json.dumps(result, indent=2))
        elif args.command == "package":
            print(json.dumps(build_package(args.directory, args.archive), indent=2))
        elif args.command == "verify-package":
            print(json.dumps(verify_package(args.archive), indent=2))
        elif args.command == "replay":
            locations = episode_locations(args.directory, private_directory=args.private, split=args.split)
            result = replay(
                locations, args.trajectory, mode=args.mode, reports_path=args.reports,
                tolerances=load_json(args.tolerances) if args.tolerances else None,
            )
            print(json.dumps(result, indent=2))
        elif args.command == "session":
            if args.directory is None:
                if args.private is not None or (args.config is None and args.seed is None):
                    raise ValueError("Provide an episode path, or select a generator --config or --seed")
                config = load_json(args.config) if args.config else {}
            else:
                if args.config is not None or args.seed is not None:
                    raise ValueError("Select an episode path or generator inputs")
                config = {"episode": args.directory, "private": args.private}
            if not isinstance(config, dict):
                raise ValueError("Generator configuration must be a JSON object")
            run_session(
                sys.stdin.fileno(), sys.stdout.fileno(), config=config, seed=args.seed,
                mode=args.mode, timeout=args.timeout, output=args.output,
                tolerances=load_json(args.tolerances) if args.tolerances else None,
            )
        elif args.command == "verify":
            return verify()
        elif args.command == "release":
            print(json.dumps(prepare_release(evaluate, args.output), indent=2))
    except (ValueError, OSError, KeyError, SchemaError, GenerationError, InjectionError,
            WorkerProtocolError) as error:
        print(f"audit_sim: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
