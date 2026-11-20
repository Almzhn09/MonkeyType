#!/usr/bin/env python3
"""Single check command: unit tests, reference checks, data generation and offline evaluation.
Stdlib only. A failed stage returns a nonzero exit code."""
import argparse
import json
import os
import resource
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable
RESULTS = []
VERBOSE = False


def run(args, **kw):
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONHASHSEED="0")
    return subprocess.run([PY, *args], cwd=ROOT, env=env, text=True,
                          capture_output=True, **kw)


def peak_rss_mb():
    c = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    s = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return max(c, s) / 1024.0  # Linux: KiB


def stage(name):
    def deco(fn):
        def wrapper(*a, **k):
            t = time.perf_counter()
            try:
                status, detail = fn(*a, **k)
            except Exception as e:  # noqa: BLE001
                status, detail = "FAIL", f"exception: {e!r}"
            dt = time.perf_counter() - t
            RESULTS.append((name, status))
            print(f"[{name}] {status} ({dt:.1f}s)")
            if detail and (status == "FAIL" or VERBOSE or status == "SKIP"):
                print("    " + str(detail).strip().replace("\n", "\n    ")[-3000:])
            return status
        return wrapper
    return deco


def tail(p):
    return (p.stdout or "") + (p.stderr or "")


@stage("tests")
def st_tests():
    p = run(["-m", "unittest", "discover", "-s", "tests", "-t", "."])
    return ("PASS" if p.returncode == 0 else "FAIL"), tail(p)


def gen(cfg, out, index=None):
    args = ["-m", "fingen", "generate", "--config", str(cfg), "--out", str(out)]
    if index is not None:
        args += ["--index", str(index)]
    t = time.perf_counter()
    p = run(args)
    return p, time.perf_counter() - t


@stage("fresh-gen")
def st_fresh():
    cfgs = sorted((ROOT / "configs/examples").glob("*.json"))
    if not cfgs:
        return "SKIP", "no configs/examples/*.json"
    lines, bad = [], False
    with tempfile.TemporaryDirectory() as d:
        for cfg in cfgs:
            out = Path(d) / cfg.stem
            p, dt = gen(cfg, out)
            rss = peak_rss_mb()
            lines.append(f"{cfg.name}: generate {dt:.2f}s (limit 60s), "
                         f"peak RSS so far {rss:.0f} MB (limit 4096 MB)")
            if p.returncode != 0:
                bad = True
                lines.append(tail(p))
                continue
            v = run(["-m", "fincheck", "verify", "--all", str(out)])
            if v.returncode != 0:
                bad = True
                lines.append(f"{cfg.name}: fincheck FAIL\n" + tail(v))
            if dt > 60:
                bad = True
                lines.append(f"{cfg.name}: exceeds 60s/episode")
    print("    " + "\n    ".join(l for l in lines if "generate" in l))
    return ("FAIL" if bad else "PASS"), "\n".join(l for l in lines if "generate" not in l)


@stage("fresh-seed")
def st_fresh_seed():
    """A20: unseen seed + a combination absent from train/dev, verified and selfchecked."""
    cfg = {"config_version": "1", "schema_version": "1.0", "seed": 900001, "split": "jury",
           "size": {"invoices": 64, "background_entries": 150},
           "period": {"start_month": "2027-01", "months": 2},
           "timezone": "Asia/Dubai", "base_currency": "EUR",
           "foreign_currencies": ["USD", "CNY", "KZT"], "rounding_mode": "half_even",
           "errors": {"duplicate_invoice": 1, "payment_allocation": 0,
                      "fx_conversion": 2, "wrong_period": 0},
           "combos": {"fx_partial_payment_boundary": 2, "duplicate_plus_period": 0},
           "distractors": {"split_payments": 3, "refunds": 1, "repeated_amounts": 3,
                           "utc_boundary": 3, "advances": 2},
           "transform": None}
    used = set()
    for sp in ("train", "dev"):
        for c in json.loads((ROOT / f"configs/{sp}.json").read_text())["episodes"]:
            used.add(c["seed"])
            if (c["errors"], c["combos"]) == (cfg["errors"], cfg["combos"]):
                return "FAIL", "combination already present in train/dev"
    if cfg["seed"] in used:
        return "FAIL", "seed already used in train/dev"
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "fresh.json"
        path.write_text(json.dumps(cfg))
        out = Path(d) / "out"
        p, dt = gen(path, out)
        if p.returncode != 0:
            return "FAIL", tail(p)
        v = run(["-m", "fincheck", "verify", "--all", str(out)])
        if v.returncode != 0:
            return "FAIL", tail(v)
        evaluation = run(["-m", "audit_sim", "run", str(out), "--policy", "reconcile"])
        if evaluation.returncode != 0:
            return "FAIL", tail(evaluation)
        if json.loads(evaluation.stdout)["reconcile"]["mean_f1"] != 1.0:
            return "FAIL", tail(evaluation)
        s = run(["-m", "fingen", "selfcheck", "--config", str(path), "--out", str(Path(d) / "sc")])
        return ("PASS" if s.returncode == 0 else "FAIL"), tail(s) + f"generate {dt:.2f}s"


@stage("integrated-release")
def st_release(output):
    args = ["-m", "audit_sim", "release"]
    if output is not None:
        args.extend(("--output", str(output)))
    p = run(args)
    if p.returncode != 0:
        return "FAIL", tail(p)
    print(tail(p).strip())
    return "PASS", None


@stage("package")
def st_package(directory, archive):
    result = run(["-m", "audit_sim", "package", str(directory), str(archive)])
    if result.returncode != 0:
        return "FAIL", tail(result)
    print(result.stdout.strip())
    return "PASS", None


def release_and_package(output, archive):
    if output is None and archive is not None:
        with tempfile.TemporaryDirectory(prefix="audit-release-") as temp:
            release_and_package(Path(temp), archive)
        return
    if st_release(output) == "PASS" and archive is not None:
        st_package(output, archive)


def main():
    global VERBOSE
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--quick", action="store_true", help="skip the extra example checks")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--output", type=Path, help="Export the source code, checked episodes and trajectories")
    ap.add_argument("--archive", type=Path, help="Write a checked ZIP with a checksum manifest")
    ap.add_argument("--cpus", type=int, help="Limit inherited CPU affinity for this check")
    ap.add_argument("--process-memory-mib", type=int, help="Limit address space per process")
    ap.add_argument("--offline", action="store_true", help="Require a loopback-only network namespace")
    a = ap.parse_args()
    VERBOSE = a.verbose
    sys.path.insert(0, str(ROOT))
    from audit_sim.limits import apply_limits
    try:
        limits = apply_limits(cpus=a.cpus, process_memory_mib=a.process_memory_mib, offline=a.offline)
    except (ValueError, OSError) as error:
        ap.error(str(error))
    if limits:
        print("OS limits " + json.dumps(limits), flush=True)
    st_tests()
    st_fresh_seed()
    if a.quick:
        print("[fresh-gen] SKIP (--quick)")
    else:
        st_fresh()
    can_export = all(status == "PASS" for _, status in RESULTS)
    release_and_package(
        a.output.resolve() if a.output and can_export else None,
        a.archive.resolve() if a.archive and can_export else None,
    )
    print(f"peak RSS (self+children): {peak_rss_mb():.0f} MB")
    failed = [n for n, s in RESULTS if s == "FAIL"]
    print("RESULT: " + ("FAIL (" + ", ".join(failed) + ")" if failed else "OK"))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
