"""CLI: ``python3.11 -m fingen generate --config <file.json> [--index N] --out <dir>``."""

from __future__ import annotations

import argparse
import os
import sys

from .config import ConfigError
from .generate import generate_from_file


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m fingen")
    sub = parser.add_subparsers(dest="command", required=True)
    gen = sub.add_parser("generate", help="generate episodes from a config file")
    gen.add_argument("--config", required=True, help="config JSON (object, list or {episodes:[...]})")
    gen.add_argument("--index", type=int, default=None, help="only the N-th config (0-based)")
    gen.add_argument("--out", required=True, help="output root (public/ and private/ inside)")
    args = parser.parse_args(argv)
    try:
        results = generate_from_file(args.config, args.out, args.index)
    except (ConfigError, IndexError, NotImplementedError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    for ep, pub, priv in results:
        c = ep.history
        print(f"{ep.episode_id} split={ep.split} docs={len(c.invoices) + len(c.payments)} "
              f"rows={len(c.rows)} issues={len(ep.issues)} "
              f"public={os.path.relpath(pub, args.out)} private={os.path.relpath(priv, args.out)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
