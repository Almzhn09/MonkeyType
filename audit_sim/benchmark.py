"""Run reproducible, varied holdout cohorts with independent data checks."""

from pathlib import Path
from random import Random
from tempfile import NamedTemporaryFile, TemporaryDirectory
from time import monotonic
import json
import sys

from fincheck.verify import verify_all
from fingen.generate import generate_episode
from fingen.transform import make_pair, pair_seed

from .policies import POLICIES
from .runner import run_policy

COHORT_SIZE = 20
CASES = (
    ({}, {}), ({}, {}), ({}, {}), ({}, {}), ({}, {}),
    ({"duplicate_invoice": 1}, {}),
    ({"payment_allocation": 2}, {}),
    ({"fx_conversion": 2}, {}),
    ({"wrong_period": 2}, {}),
    ({}, {"duplicate_plus_period": 1}),
    ({}, {"fx_partial_payment_boundary": 1}),
    ({}, {"fx_partial_payment_boundary": 2}),
    ({"wrong_period": 2}, {"duplicate_plus_period": 1}),
    ({"duplicate_invoice": 2}, {"fx_partial_payment_boundary": 1}),
    ({"duplicate_invoice": 2, "payment_allocation": 1}, {}),
    ({"fx_conversion": 2, "wrong_period": 1}, {}),
    ({"payment_allocation": 2, "wrong_period": 2}, {}),
    ({"duplicate_invoice": 2, "fx_conversion": 3, "wrong_period": 2}, {}),
    ({"duplicate_invoice": 2, "payment_allocation": 2, "fx_conversion": 2, "wrong_period": 2},
     {"fx_partial_payment_boundary": 1, "duplicate_plus_period": 1}),
    ({"payment_allocation": 2, "fx_conversion": 1}, {"fx_partial_payment_boundary": 1, "duplicate_plus_period": 1}),
)
ZONES = ("Asia/Almaty", "Asia/Dubai", "Asia/Kolkata", "Asia/Tokyo", "America/Bogota", "Etc/UTC")
CURRENCIES = ("KZT", "EUR", "USD", "JPY", "KWD", "CNY", "GBP")
INVARIANT_METRICS = ("tp", "fp", "fn", "f1", "reward", "steps")
PAIRED_POLICIES = ("reconcile", "oracle", "simple")


def cohort_configs(seed, index):
    rng = Random(f"genesis-benchmark-v1:{seed}:{index}")
    seed_start = rng.randrange(10_000, 2**63 - COHORT_SIZE)
    for case, (errors, combos) in enumerate(CASES):
        issue_count = sum(errors.values()) + 2 * sum(combos.values())
        invoices = rng.randint(max(14, 3 * issue_count), 95)
        base, *foreign = rng.sample(CURRENCIES, 1 + rng.randint(1, 3))
        # At the largest document counts use fewer split receipts to leave room for FX combos.
        splits = 1 if invoices >= 90 else rng.randint(1, 3)
        yield {
            "seed": seed_start + case,
            "split": "holdout",
            "size": {"invoices": invoices, "background_entries": rng.randint(0, 680)},
            "period": {"start_month": f"2027-{rng.randint(1, 12):02}", "months": rng.randint(1, 3)},
            "timezone": rng.choice(ZONES), "base_currency": base, "foreign_currencies": foreign,
            "rounding_mode": rng.choice(("half_up", "half_even")),
            "errors": errors, "combos": combos,
            "distractors": {
                "split_payments": splits, "refunds": 1, "repeated_amounts": 2,
                "utc_boundary": 3, "advances": 1,
            },
        }


def run_cohort(seed, index):
    with TemporaryDirectory(prefix="audit-holdout-") as temp:
        root = Path(temp)
        originals = []
        for config in cohort_configs(seed, index):
            try:
                episode, public, private = generate_episode(config, temp)
                paired = make_pair(public, private, temp, "pairs", pair_seed(config["seed"], episode.episode_id))
            except (ValueError, RuntimeError) as error:
                raise ValueError(f"Episode seed {config['seed']}: {error}") from error
            originals.append((public, private, paired))
        checked, problems = verify_all(temp)
        if len(checked) != COHORT_SIZE * 2 or problems or any(not item.ok for item in checked):
            details = problems + [item.format() for item in checked if not item.ok]
            raise ValueError("Independent data check failed: " + " | ".join(details[:4]))

        metrics = []
        for public, private, paired in originals:
            reference = {}
            for name in POLICIES:
                result, _ = run_policy(public, name, private_directory=private, policy_seed=seed)
                if result["reason"] != "submitted":
                    raise ValueError(f"{name} did not finish {result['episode_id']}")
                if name in ("oracle", "reconcile") and result["f1"] != 1:
                    raise ValueError(f"{name} missed an issue in {result['episode_id']}")
                metrics.append(result)
                reference[name] = result
            for name in PAIRED_POLICIES:
                result, _ = run_policy(
                    root / "public" / "pairs" / paired, name,
                    private_directory=root / "private" / "pairs" / paired, policy_seed=seed,
                )
                if result["reason"] != "submitted" or any(
                    result[metric] != reference[name][metric] for metric in INVARIANT_METRICS
                ):
                    raise ValueError(f"{name} score changed on pair {paired}")
        return metrics


def write_progress(path, report):
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, prefix=".benchmark-", delete=False) as temp:
        temporary_path = Path(temp.name)
        try:
            json.dump(report, temp, indent=2, allow_nan=False)
            temp.write("\n")
            temp.close()
            temporary_path.replace(path)
        finally:
            temporary_path.unlink(missing_ok=True)


def benchmark(seed, batches, *, start_index=0, output=None, progress_every=1):
    if batches < 1 or start_index < 0 or progress_every < 1:
        raise ValueError("batches and progress interval must be positive, and start index nonnegative")
    start = monotonic()
    totals = {name: {"runs": 0, "f1_total": 0.0, "reward_total": 0.0,
                     "max_steps": 0, "max_seconds": 0.0} for name in POLICIES}
    report = {
        "profile_version": 1, "seed": seed, "start_index": start_index,
        "planned_cohorts": batches, "completed_cohorts": 0,
        "verified_originals": 0, "verified_pairs": 0, "policy_checks": 0,
        "elapsed_seconds": 0.0, "status": "running", "policies": {},
    }
    try:
        for offset in range(batches):
            index = start_index + offset
            metrics = run_cohort(seed, index)
            for row in metrics:
                total = totals[row["policy"]]
                total["runs"] += 1
                total["f1_total"] += row["f1"]
                total["reward_total"] += row["reward"]
                total["max_steps"] = max(total["max_steps"], row["steps"])
                total["max_seconds"] = max(total["max_seconds"], row["duration_seconds"])
            report.update(
                completed_cohorts=offset + 1,
                verified_originals=(offset + 1) * COHORT_SIZE,
                verified_pairs=(offset + 1) * COHORT_SIZE,
                policy_checks=(offset + 1) * COHORT_SIZE * (len(POLICIES) + len(PAIRED_POLICIES)),
                elapsed_seconds=round(monotonic() - start, 3),
                policies={name: {
                    "runs": total["runs"], "mean_f1": total["f1_total"] / total["runs"],
                    "mean_reward": total["reward_total"] / total["runs"],
                    "max_steps": total["max_steps"], "max_seconds": total["max_seconds"],
                } for name, total in totals.items()},
            )
            if (offset + 1) % progress_every == 0:
                print(f"benchmark: {offset + 1}/{batches} cohorts checked", file=sys.stderr, flush=True)
            if output is not None:
                write_progress(output, report)
    except Exception as error:
        report.update(status="failed", failed_cohort=start_index + report["completed_cohorts"], error=str(error))
        if output is not None:
            write_progress(output, report)
        raise
    report["status"] = "passed"
    if output is not None:
        write_progress(output, report)
    return report
