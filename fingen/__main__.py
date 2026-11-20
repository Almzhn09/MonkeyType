"""CLI (A18, A20).

  python3.11 -m fingen generate --config F [--index N] --out DIR
  python3.11 -m fingen pair --public P --private Q --out DIR --split S --seed N
                            [--no-permute] [--no-rename]
  python3.11 -m fingen build --configs F [F ...] --out DIR [--pairs] [--pair-split pairs]
  python3.11 -m fingen selfcheck --config F --out DIR [--pairs] [--keep]

``selfcheck`` generates the same config file twice into two fresh directories
under DIR and compares the trees byte for byte (exit 1 on any difference).
"""

from __future__ import annotations

import argparse
from json import JSONDecodeError
import os
import shutil
import sys
import tempfile

from .config import ConfigError, load_config_file
from .generate import generate_episode
from .history import GenerationError
from .inject import InjectionError
from .transform import TransformError, make_pair, pair_seed


def _print_episode(ep, pub, priv, root) -> None:
    c = ep.history
    print(f"{ep.episode_id} split={ep.split} docs={len(c.invoices) + len(c.payments)} "
          f"rows={len(c.rows)} issues={len(ep.issues)} "
          f"public={os.path.relpath(pub, root)} private={os.path.relpath(priv, root)}")


def build(config_files, out_dir: str, pairs: bool = False, pair_split: str = "pairs",
          index: int | None = None, quiet: bool = False) -> list[str]:
    """Generate every episode of ``config_files`` (and optionally one pair each).
    Returns the episode_ids written. Raises ConfigError on duplicate episode_ids."""
    written: list[str] = []
    seen: dict[str, str] = {}
    for path in config_files:
        items = load_config_file(path)
        if index is not None:
            if not 0 <= index < len(items):
                raise IndexError(f"--index {index} out of range ({path} has {len(items)} configs)")
            items = [items[index]]
        for item in items:
            ep, pub, priv = generate_episode(item, out_dir)
            where = f"{path}[seed={ep.history.seed}]"
            if ep.episode_id in seen:
                raise ConfigError(f"duplicate episode {ep.episode_id}: {seen[ep.episode_id]} "
                                  f"and {where}")
            seen[ep.episode_id] = where
            written.append(ep.episode_id)
            if not quiet:
                _print_episode(ep, pub, priv, out_dir)
            if pairs:
                seed = pair_seed(ep.history.seed, ep.episode_id)
                new_id = make_pair(pub, priv, out_dir, pair_split, seed)
                written.append(new_id)
                if not quiet:
                    print(f"{new_id} split={pair_split} pair_of={ep.episode_id} "
                          f"transform_seed={seed}")
    return written


def tree_diff(a: str, b: str) -> list[str]:
    """Relative paths that differ (missing on one side or different bytes)."""
    def files(root):
        out = {}
        for base, _dirs, names in os.walk(root):
            for n in names:
                p = os.path.join(base, n)
                out[os.path.relpath(p, root)] = p
        return out

    fa, fb = files(a), files(b)
    diffs = [f"only in first: {p}" for p in sorted(set(fa) - set(fb))]
    diffs += [f"only in second: {p}" for p in sorted(set(fb) - set(fa))]
    for p in sorted(set(fa) & set(fb)):
        with open(fa[p], "rb") as x, open(fb[p], "rb") as y:
            if x.read() != y.read():
                diffs.append(f"differs: {p}")
    return diffs


def _cmd_selfcheck(a) -> int:
    os.makedirs(a.out, exist_ok=True)
    runs = [tempfile.mkdtemp(prefix=f"selfcheck-{tag}-", dir=a.out) for tag in ("a", "b")]
    ids = [build([a.config], r, pairs=a.pairs, quiet=True) for r in runs]
    diffs = tree_diff(*runs)
    if ids[0] != ids[1]:
        diffs.insert(0, "episode lists differ")
    if diffs:
        print(f"FAIL selfcheck {a.config}: {len(diffs)} difference(s)")
        for d in diffs[:50]:
            print(f"    - {d}")
        print(f"runs kept in {runs[0]} and {runs[1]}")
        return 1
    n_files = sum(len(fs) for _b, _d, fs in os.walk(runs[0]))
    print(f"PASS selfcheck {a.config}: {len(ids[0])} episode(s), {n_files} files "
          f"byte-identical across two runs")
    if a.keep:
        print(f"runs kept in {runs[0]} and {runs[1]}")
    else:
        for r in runs:
            shutil.rmtree(r, ignore_errors=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m fingen")
    sub = parser.add_subparsers(dest="command", required=True)
    gen = sub.add_parser("generate", help="generate episodes from a config file")
    gen.add_argument("--config", required=True, help="config JSON (object, list or {episodes:[...]})")
    gen.add_argument("--index", type=int, default=None, help="only the N-th config (0-based)")
    gen.add_argument("--out", required=True, help="output root (public/ and private/ inside)")

    pr = sub.add_parser("pair", help="A18: write a permuted, ID-renamed copy of an episode")
    pr.add_argument("--public", required=True, help="source public episode dir")
    pr.add_argument("--private", required=True, help="source private episode dir")
    pr.add_argument("--out", required=True, help="output root (public/ and private/ inside)")
    pr.add_argument("--split", required=True, help="split name for the paired episode")
    pr.add_argument("--seed", required=True, type=int, help="transform seed")
    pr.add_argument("--no-permute", action="store_true", help="keep record order")
    pr.add_argument("--no-rename", action="store_true", help="keep IDs (episode_id changes)")

    bd = sub.add_parser("build", help="generate every episode of one or more config files")
    bd.add_argument("--configs", required=True, nargs="+", help="config files")
    bd.add_argument("--out", required=True, help="output root")
    bd.add_argument("--pairs", action="store_true",
                    help="also write one paired episode per episode into --pair-split")
    bd.add_argument("--pair-split", default="pairs", help="split for pairs (default: pairs)")

    sc = sub.add_parser("selfcheck", help="A20: generate twice and compare byte for byte")
    sc.add_argument("--config", required=True)
    sc.add_argument("--out", required=True, help="directory for the two runs")
    sc.add_argument("--pairs", action="store_true", help="also build and compare pairs")
    sc.add_argument("--keep", action="store_true", help="keep both runs")

    args = parser.parse_args(argv)
    try:
        if args.command == "generate":
            build([args.config], args.out, index=args.index)
        elif args.command == "pair":
            new_id = make_pair(args.public, args.private, args.out, args.split, args.seed,
                               permute=not args.no_permute, rename=not args.no_rename)
            print(f"{new_id} split={args.split} "
                  f"public={os.path.join('public', args.split, new_id)} "
                  f"private={os.path.join('private', args.split, new_id)}")
        elif args.command == "build":
            ids = build(args.configs, args.out, pairs=args.pairs, pair_split=args.pair_split)
            print(f"built {len(ids)} episode(s) in {args.out}")
        else:
            return _cmd_selfcheck(args)
    except (ConfigError, IndexError, JSONDecodeError, OSError, GenerationError, InjectionError,
            NotImplementedError, TransformError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
