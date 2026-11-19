#!/usr/bin/env python3
"""Single check command: unit tests, reproducibility, fincheck verification,
env hook. Stdlib only. Exit code != 0 if any stage FAILs (SKIP is fine)."""
import argparse
import filecmp
import importlib.util
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
            if not (name == "env" and status == "SKIP"):
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


def has_selfcheck():
    p = run(["-m", "fingen", "--help"])
    return "selfcheck" in tail(p)


def gen(cfg, out, index=None):
    args = ["-m", "fingen", "generate", "--config", str(cfg), "--out", str(out)]
    if index is not None:
        args += ["--index", str(index)]
    t = time.perf_counter()
    p = run(args)
    return p, time.perf_counter() - t


@stage("repro")
def st_repro():
    cfg = ROOT / "configs/examples/all_types.json"
    if not cfg.exists():
        cfg = ROOT / "configs/examples/clean_small.json"
    if not cfg.exists():
        return "SKIP", "no example config"
    if has_selfcheck():
        with tempfile.TemporaryDirectory() as d:
            p = run(["-m", "fingen", "selfcheck", "--config", str(cfg), "--out", d])
        return ("PASS" if p.returncode == 0 else "FAIL"), tail(p)
    with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
        for d in (a, b):
            p, _ = gen(cfg, d)
            if p.returncode != 0:
                return "FAIL", tail(p)
        diffs = []

        def walk(x, y, rel=""):
            c = filecmp.dircmp(x, y)
            diffs.extend(f"{rel}/{n}" for n in c.left_only + c.right_only)
            for n in c.common_files:
                if (Path(x) / n).read_bytes() != (Path(y) / n).read_bytes():
                    diffs.append(f"{rel}/{n}")
            for d in c.common_dirs:
                walk(Path(x) / d, Path(y) / d, f"{rel}/{d}")
        walk(a, b)
        return ("PASS", "byte-identical (selfcheck absent; compared manually)") \
            if not diffs else ("FAIL", "differences: " + ", ".join(diffs[:20]))


@stage("verify-episodes")
def st_verify_existing():
    if not (ROOT / "episodes/public").exists():
        return "SKIP", "episodes/public not present"
    p = run(["-m", "fincheck", "verify", "--all", "episodes"])
    return ("PASS" if p.returncode == 0 else "FAIL"), tail(p)


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
        s = run(["-m", "fingen", "selfcheck", "--config", str(path), "--out", str(Path(d) / "sc")])
        return ("PASS" if s.returncode == 0 else "FAIL"), tail(s) + f"generate {dt:.2f}s"


@stage("env")
def st_env():
    mod = os.environ.get("FINCASE_ENV_CHECK", "fincase_env.check")
    try:
        spec = importlib.util.find_spec(mod)
    except (ImportError, ValueError):
        spec = None
    if spec is None:
        return "SKIP", None
    p = run(["-m", mod])
    return ("PASS" if p.returncode == 0 else "FAIL"), tail(p)


def main():
    global VERBOSE
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--quick", action="store_true", help="skip fresh generation stage (d)")
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()
    VERBOSE = a.verbose
    sys.path.insert(0, str(ROOT))
    st_tests()
    st_repro()
    st_verify_existing()
    st_fresh_seed()
    if a.quick:
        print("[fresh-gen] SKIP (--quick)")
    else:
        st_fresh()
    if st_env() == "SKIP":
        print("[env] SKIP (teammate package not present)")
    print(f"peak RSS (self+children): {peak_rss_mb():.0f} MB")
    failed = [n for n, s in RESULTS if s == "FAIL"]
    print("RESULT: " + ("FAIL (" + ", ".join(failed) + ")" if failed else "OK"))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
