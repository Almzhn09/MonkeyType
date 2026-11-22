#!/usr/bin/env python3
"""Single check command: unit tests, reference checks, data generation and offline evaluation.
Stdlib only. A failed stage returns a nonzero exit code."""
import argparse
import json
import os
import platform
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable
RESULTS = []  # one dict per stage: name, status, seconds, covers, extra facts
VERBOSE = False
OFFICIAL = True  # without --quick, a skipped stage counts as a failure
COVERS = {
    "tests": "Unit and integration tests: generator, independent verifier, environment contract, grader, "
             "policies, sandbox isolation, playground, dashboard and hosting.",
    "fresh-seed": "A seed and error mix absent from train/dev: generate, verify independently, reference "
                  "reconciler F1 = 1, and two builds byte-identical.",
    "fresh-gen": "Every example config: generate within 60 s and 4 GB, then verify independently.",
    "integrated-release": "Build train/dev with pairs twice and compare bytes; verify 20 originals and 20 "
                          "shuffled, renamed pairs; oracle, reconcile and simple score identically on every "
                          "pair; evaluate every policy on dev; record train trajectories.",
}


def run(args, **kw):
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONHASHSEED="0", PYTHONIOENCODING="utf-8")
    return subprocess.run([PY, *args], cwd=ROOT, env=env, encoding="utf-8", errors="replace",
                          capture_output=True, **kw)


try:
    import resource
except ImportError:  # Windows
    resource = None


def peak_rss_mb():
    """Peak resident memory of this process and its children, or None where unavailable."""
    if resource is None:
        return None
    c = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    s = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    unit = 1024.0 * 1024.0 if sys.platform == "darwin" else 1024.0  # macOS: bytes, Linux: KiB
    return max(c, s) / unit


def format_rss():
    rss = peak_rss_mb()
    return "n/a" if rss is None else f"{rss:.0f} MB"


def stage(name):
    def deco(fn):
        def wrapper(*a, **k):
            t = time.perf_counter()
            extra = {}
            try:
                status, detail, *rest = fn(*a, **k)
                extra = rest[0] if rest else {}
            except Exception as e:  # noqa: BLE001
                status, detail = "FAIL", f"exception: {e!r}"
            if status == "SKIP" and OFFICIAL:
                status = "FAIL"
                detail = f"{detail or 'skipped'} (no stage may be skipped in the official run)"
            dt = time.perf_counter() - t
            RESULTS.append({"name": name, "status": status, "seconds": round(dt, 1), "covers": COVERS.get(name, ""), **extra})
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
    ran = re.search(r"Ran (\d+) tests?", tail(p))
    skipped = re.search(r"skipped=(\d+)", tail(p))
    extra = {"tests_run": int(ran.group(1)) if ran else 0, "tests_skipped": int(skipped.group(1)) if skipped else 0}
    ok = p.returncode == 0 and extra["tests_run"] > 0
    return ("PASS" if ok else "FAIL"), tail(p), extra


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
            lines.append(f"{cfg.name}: generate {dt:.2f}s (limit 60s), "
                         f"peak RSS so far {format_rss()} (limit 4096 MB)")
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
    return ("FAIL" if bad else "PASS"), "\n".join(l for l in lines if "generate" not in l), {"configs": len(cfgs)}


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
        for c in json.loads((ROOT / "configs" / f"{sp}.json").read_text(encoding="utf-8"))["episodes"]:
            used.add(c["seed"])
            if (c["errors"], c["combos"]) == (cfg["errors"], cfg["combos"]):
                return "FAIL", "combination already present in train/dev"
    if cfg["seed"] in used:
        return "FAIL", "seed already used in train/dev"
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "fresh.json"
        path.write_text(json.dumps(cfg), encoding="utf-8")
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


def write_verification(output, quick):
    """Record which stages ran, and what each covers, next to the release's own validation."""
    path = output / "validation.json"
    validation = json.loads(path.read_text(encoding="utf-8"))
    validation["verification"] = {
        "command": "python3 scripts/check.py" + (" --quick" if quick else ""),
        "mode": "quick" if quick else "official",
        "python": platform.python_version(),
        "platform": sys.platform,
        "stages": RESULTS,
        "result": "OK" if all(item["status"] == "PASS" for item in RESULTS) else "FAIL",
    }
    path.write_text(json.dumps(validation, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")


def main():
    global VERBOSE, OFFICIAL
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--quick", action="store_true", help="skip the extra example checks")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--output", type=Path, help="Export the source code, checked episodes and trajectories")
    a = ap.parse_args()
    VERBOSE = a.verbose
    OFFICIAL = not a.quick
    sys.path.insert(0, str(ROOT))
    st_tests()
    st_fresh_seed()
    if a.quick:
        print("[fresh-gen] SKIP (--quick)")
        RESULTS.append({"name": "fresh-gen", "status": "SKIP", "seconds": 0, "covers": COVERS["fresh-gen"],
                        "reason": "--quick"})
    else:
        st_fresh()
    can_export = all(item["status"] in ("PASS", "SKIP") for item in RESULTS)
    output = a.output.resolve() if a.output and can_export else None
    st_release(output)
    if output is not None and RESULTS[-1]["status"] == "PASS":
        write_verification(output, a.quick)
    print(f"peak RSS (self+children): {format_rss()}")
    failed = [item["name"] for item in RESULTS if item["status"] == "FAIL"]
    print("RESULT: " + ("FAIL (" + ", ".join(failed) + ")" if failed else "OK"))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
