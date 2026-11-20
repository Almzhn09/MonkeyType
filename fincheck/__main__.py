"""CLI.

  python3.11 -m fincheck verify --public <dir> --private <dir>
  python3.11 -m fincheck verify --all <episodes_root>        # public/<split>/* + private/<split>/*
  python3.11 -m fincheck reconcile --public <dir> [-o answer.json]
  python3.11 -m fincheck match --public <dir> --private <dir> --submission <answer.json>

Exit code 0 only if every check passes.
"""
from __future__ import annotations

import argparse
import json
import sys

from .load import SchemaError, load_private, load_public
from .matcher_stub import explain_unmatched, match
from .recompute import reconcile
from .verify import verify_all, verify_episode


def _cmd_verify(a) -> int:
    if a.all:
        reports, problems = verify_all(a.all)
    elif a.public and a.private:
        reports, problems = [verify_episode(a.public, a.private)], []
    else:
        print("verify: give --public and --private, or --all", file=sys.stderr)
        return 2
    for p in problems:
        print(f"FAIL layout: {p}")
    for r in reports:
        print(r.format(verbose=a.verbose))
    bad = sum(not r.ok for r in reports) + len(problems)
    print(f"{len(reports) - sum(not r.ok for r in reports)}/{len(reports)} episodes passed"
          + (f", {len(problems)} layout problem(s)" if problems else ""))
    return 1 if bad or not reports else 0


def _cmd_reconcile(a) -> int:
    pub = load_public(a.public)
    out = {"episode_id": pub.manifest["episode_id"], "findings": reconcile(pub)}
    text = json.dumps(out, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    if a.output:
        with open(a.output, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
    else:
        sys.stdout.write(text)
    return 0


def _cmd_match(a) -> int:
    pub = load_public(a.public)
    prv = load_private(a.private)
    with open(a.submission, encoding="utf-8") as fh:
        sub = json.load(fh)
    findings = sub.get("findings", []) if isinstance(sub, dict) else []
    issues = prv.truth["issues"]
    ids = pub.id_space()
    res = match(findings, issues, ids)
    print(json.dumps({"tp": res.tp, "fp": res.fp, "fn": res.fn, "precision": res.precision,
                      "recall": res.recall, "f1": res.f1, "pairs": res.pairs}))
    for line in explain_unmatched(res, findings, issues, ids):
        print("  " + line)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m fincheck")
    sub = ap.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("verify", help="verify episode(s)")
    v.add_argument("--public")
    v.add_argument("--private")
    v.add_argument("--all", metavar="EPISODES_ROOT")
    v.add_argument("-v", "--verbose", action="store_true", help="show warnings for PASS too")
    r = sub.add_parser("reconcile", help="print the reference reconciler's submission")
    r.add_argument("--public", required=True)
    r.add_argument("-o", "--output")
    m = sub.add_parser("match", help="score a submission with the reference matcher")
    m.add_argument("--public", required=True)
    m.add_argument("--private", required=True)
    m.add_argument("--submission", required=True)
    a = ap.parse_args(argv)
    try:
        return {"verify": _cmd_verify, "reconcile": _cmd_reconcile,
                "match": _cmd_match}[a.cmd](a)
    except SchemaError as exc:
        for e in exc.errors:
            print(f"schema: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
