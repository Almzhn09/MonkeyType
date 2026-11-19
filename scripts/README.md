# scripts/

`check.py` is the single check command (stdlib only, Python 3.11).

    python3.11 scripts/check.py            # all stages
    python3.11 scripts/check.py --quick    # skip fresh generation + verify
    python3.11 scripts/check.py --verbose  # show output of passing stages

Stages (each printed as `[stage] PASS/FAIL/SKIP (Xs)`):

1. `tests` - `unittest` discovery in `tests/`.
2. `repro` - `fingen selfcheck` if available, else generate twice and compare bytes.
3. `verify-episodes` - `fincheck verify --all episodes` if `episodes/public` exists.
4. `fresh-gen` - generate each `configs/examples/*.json`, verify with fincheck;
   prints per-episode generation time and peak RSS against the limits
   (2 CPU, 4 GB, 60 s/episode).
5. `env` - runs `python -m $FINCASE_ENV_CHECK` (default `fincase_env.check`) if
   importable, otherwise `SKIP` (does not fail the run).

Exit code is non-zero if any stage FAILs. In Docker:
`docker run --rm --network none --cpus 2 --memory 4g fincase`.
