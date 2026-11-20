"""Generate and independently check the submission data and recorded policies."""

from collections import Counter
from pathlib import Path
from shutil import copy2, copytree, ignore_patterns
from tempfile import TemporaryDirectory

from fincheck.verify import verify_all
from fingen.__main__ import build, tree_diff
from fingen.config import load_config_file

from .data import Episode, load_json
from .fixtures import dump_json, dump_jsonl
from .replay import replay
from .report import write_report
from .scoring import oracle_submission, score_submission

ROOT = Path(__file__).resolve().parents[1]
CONFIGS = ROOT / "configs"
SPLITS = ("train", "dev")
SOURCE_DIRS = ("audit_sim", "fingen", "fincheck", "configs", "tests", "scripts", "docs")
SOURCE_FILES = (".dockerignore", ".gitignore", "Dockerfile", "LICENSE", "README.md", "requirements.txt", "start-genesis.command")
DEMO_ERROR_ID = "EP-CHK-A08"
DEMO_CLEAN_ID = "EP-QRS8HS6811"


def evaluate_demo(evaluate, *, dataset=ROOT / "episodes", output=None):
    control = ROOT / "fincheck" / "fixtures" / "a08"
    with TemporaryDirectory(prefix="audit-demo-") as temp:
        directory = Path(temp)
        for scope in ("public", "private"):
            target = directory / scope / "demo"
            copytree(control / scope, target / DEMO_ERROR_ID)
            copytree(dataset / scope / "dev" / DEMO_CLEAN_ID, target / DEMO_CLEAN_ID)
        return evaluate(directory, split="demo", choices=("reconcile", "mark_all"), output=output)


def check_oracle_pairs(dataset):
    originals = {
        public.name: Episode.load(public, dataset / "private" / split / public.name)
        for split in SPLITS
        for public in (dataset / "public" / split).iterdir()
    }
    count = 0
    for private in sorted((dataset / "private" / "pairs").iterdir()):
        mapping = load_json(private / "id_map.json")
        original = originals.pop(mapping["source_episode_id"])
        transformed = Episode.load(dataset / "public" / "pairs" / private.name, private)
        ids = mapping["ids"]
        answer = oracle_submission(original)
        expected = score_submission(original, answer, 3)
        answer["episode_id"] = ids[answer["episode_id"]]
        for finding in answer["findings"]:
            for key in ("affected_ids", "evidence_ids"):
                finding[key] = [ids.get(value, value) for value in finding[key]]
        actual = score_submission(transformed, answer, 3)
        if expected.f1 != 1 or expected != actual:
            raise ValueError(f"Oracle score changed after reordering and renaming {original.manifest['episode_id']}")
        count += 1
    if originals:
        raise ValueError("Some episodes were not paired")
    return count


def validate_configs(configs):
    seen = set()
    for split, path in zip(SPLITS, configs):
        items = load_config_file(path)
        seeds = {item["seed"] for item in items}
        if len(items) != 10 or len(seeds) != 10 or any(item["split"] != split for item in items) or seeds & seen:
            raise ValueError(f"Expected ten unique, disjoint seeds on the {split} split")
        seen.update(seeds)


def prepare_release(evaluate, output=None):
    if output is not None and output.exists() and any(output.iterdir()):
        raise ValueError(f"Release output must be an empty directory: {output}")
    configs = [str(CONFIGS / f"{split}.json") for split in SPLITS]
    validate_configs(configs)
    with TemporaryDirectory() as temp:
        dataset, repeated = (Path(temp) / name for name in ("episodes", "repeat"))
        for root in (dataset, repeated):
            build(configs, str(root), pairs=True, quiet=True)
        differences = tree_diff(dataset, repeated)
        if differences:
            raise ValueError(f"Dataset is not reproducible: {differences[:5]}")
        verified, problems = verify_all(str(dataset))
        if len(verified) != 40 or problems or any(not item.ok for item in verified):
            failures = [item.format() for item in verified if not item.ok]
            raise ValueError(f"Expected 40 valid originals and pairs: {problems + failures}")
        pairs = check_oracle_pairs(dataset)

        reports, summary, _ = evaluate(dataset, split="dev")
        if len(reports) != 40 or any(
            item["truncated"] or item["invalid_submission"] for item in reports
        ) or summary["oracle"]["mean_f1"] != 1:
            raise ValueError("Expected 40 completed dev runs and correct oracle results")
        if summary["reconcile"]["mean_f1"] != 1 or summary["mark_all"]["mean_f1"] >= 0.1:
            raise ValueError("Reference reconciliation or negative control disagrees with the dev data")
        train_reports, _, trajectories = evaluate(
            dataset, split="train", choices=("reconcile",), mode="train",
        )
        if len(train_reports) != 10 or any(
            item["reason"] != "submitted" or item["f1"] != 1 for item in train_reports
        ):
            raise ValueError("Expected ten successful training trajectories")
        counts = Counter(row["episode_id"] for row in trajectories)
        if any(counts[item["episode_id"]] != item["steps"] for item in train_reports):
            raise ValueError("Training trajectory length disagrees with executed actions")
        trace_file = Path(temp) / "trajectories.jsonl"
        report_file = Path(temp) / "train-reports.json"
        dump_jsonl(trace_file, trajectories)
        dump_json(report_file, train_reports)
        training_public = dataset / "public" / "train"
        training_private = dataset / "private" / "train"
        locations = {item.name: (item, training_private / item.name) for item in training_public.iterdir()}
        replayed = replay(locations, trace_file, reports_path=report_file)

        demo_reports, _, demo_trace = evaluate_demo(evaluate, dataset=dataset)
        demo_scores = {(item["episode_id"], item["policy"]): item for item in demo_reports}
        error = demo_scores[DEMO_ERROR_ID, "reconcile"]
        clean = demo_scores[DEMO_CLEAN_ID, "reconcile"]
        broad = demo_scores[DEMO_CLEAN_ID, "mark_all"]
        if ((error["tp"], error["fp"], error["fn"]) != (2, 0, 0)
                or not clean["terminated"] or clean["invalid_submission"]
                or clean["submitted_findings"] or not broad["fp"] or clean["reward"] <= broad["reward"]):
            raise ValueError("The demonstration failed to distinguish supported findings from false accusations")

        result = {
            "train_episodes": 10, "dev_episodes": 10, "verified_pairs": pairs,
            "dev_runs": len(reports), "recorded_train_steps": len(trajectories),
            "replayed_train_steps": replayed["steps"],
            "demo_runs": len(demo_reports),
            "max_dev_episode_seconds": max(item["duration_seconds"] for item in reports),
            "dev": summary,
        }
        if output is not None:
            output.mkdir(parents=True, exist_ok=True)
            for directory in SOURCE_DIRS:
                copytree(ROOT / directory, output / directory, ignore=ignore_patterns("__pycache__"))
            for filename in SOURCE_FILES:
                copy2(ROOT / filename, output / filename)
            copytree(dataset, output / "episodes")
            dump_json(output / "dev-reports.json", reports)
            copy2(report_file, output / report_file.name)
            dump_json(output / "validation.json", result)
            copy2(trace_file, output / trace_file.name)
            write_report(output / "train-report.html", train_reports, trajectories)
            write_report(output / "demo-report.html", demo_reports, demo_trace)
        return result
