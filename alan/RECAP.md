# Recap — Alan's part (Genesis FIN_CASE 2026)

Status as of 19 Nov 2026. Branch `claude/gallant-ride-18rjlt`.
**18 of 25 tasks done** (ticked in [`README.md`](README.md)); the other 7 need Alimzhan's side.
The single check command `python scripts/check.py` passes locally and in Docker
(`--network none --cpus 2 --memory 4g`). 162 tests pass.

## What was built

| Area | Where | What it does |
|---|---|---|
| Spec excerpt | `docs/spec/genesis-excerpt.md` | The case text (§2–3, §6–10) saved in the repo. §1, §4, §5 were missing, so §4–5 were reconstructed from Alimzhan's plan. |
| Data contract (A01–A02) | `docs/CONTRACT.md` | File formats, IDs, money/time/FX rules, the 4 error types (what is compared, amount, currency, tolerance), accepted evidence sets, public/private split. |
| Generator (A03–A14) | `fingen/` | Builds a consistent company history first, then injects errors and records every change. 4 error types, 2 combinations, decoy cases. Same config + seed → byte-identical files. |
| Independent checker (A15, A08) | `fincheck/` | Never imports the generator. Recomputes every declared error from open files, checks each evidence set is sufficient, rebuilds the pre-error books and checks they balance, scans for leaks. Includes the A08 control episode for Alimzhan. |
| Paired episodes (A18) | `fingen/transform.py` | Shuffles rows and renames all IDs consistently; scores stay identical. Writes `id_map.json`. |
| Commands (A20) | `python -m fingen generate / build / pair / selfcheck` | Generate, build whole sets, make pairs, generate twice and compare byte for byte. |
| Episode sets (A19) | `configs/train.json`, `configs/dev.json`, `episodes/` | 10 train + 10 dev (non-overlapping seeds) + 20 pairs. All 40 pass the checker. Table in `configs/README.md`. |
| Container (A21) | `Dockerfile`, `LICENSE` (MIT), `scripts/check.py` | One command runs tests, reproducibility, episode verification, a fresh unseen seed, and the teammate's `env` stage (skipped until it exists). |
| Docs (A23) | `README.md`, `docs/DESIGN.md` | How to build and run, episode structure, money/time rules, measured limits, honest limitations. |

## The four error types

| Type | What is compared | Claimed amount (base currency) |
|---|---|---|
| `duplicate_invoice` | Invoice ↔ its ledger postings (one expected, two found) | The extra posting |
| `payment_allocation` | Payment's invoice refs ↔ its allocation rows (missing or partial) | The unallocated part |
| `fx_conversion` | Foreign document ↔ posting at the wrong date/rate | Difference vs. the correct conversion (±1 tolerance) |
| `wrong_period` | Invoice date ↔ posting date across the period boundary (incl. UTC vs. local date) | The misposted amount |

Combinations: a partially allocated foreign payment converted at the wrong date near the period
boundary; a duplicated invoice plus a different invoice posted in the wrong period.

## Measured

- Generating an episode: ~0.25 s, peak memory 61 MB (limits: 60 s, 4 GB).
- All 40 open episodes: ~5 MB on disk, verified in ~1.2 s.

## Things to know

- The checker proves the generator matches **our** contract. The contract itself isn't checked
  against the full PDF yet — compare §4–5 with the original.
- The Docker image contains the hidden answer files (`episodes/private`). Keeping them from the
  agent depends on Alimzhan's runner mounting only `public/` (his task B14).

## Still open (needs Alimzhan)

- **A01–A02** — agree on the contract and file layout.
- **A06** — confirm his loader reads the files.
- **A08** — run the control episode (`fincheck/fixtures/a08/`) through his API together.
- **A22** — plug his CLI and tests into the `env` stage of `scripts/check.py`.
- **A24–A25** — check the full package (4 policy adapters, `trajectories.jsonl`) and rehearse the demo.
