# Recap — Alan's part (Genesis FIN_CASE 2026)

The integration branch covers the technical deliverables A01–A24 and both demo scenarios.
Team rehearsal and submission confirmation are tracked under A25 in the [task list](README.md).
The current archive, offline verification command and results are in [the submission guide](../submission/README.md).

## What was built

| Area | Where | What it does |
|---|---|---|
| Spec excerpt | `docs/spec/genesis-excerpt.md` | The requirements in §1–10, checked against the six-page case document. |
| Data contract (A01–A02) | `docs/CONTRACT.md` | File formats, IDs, money/time/FX rules, the 4 error types (what is compared, amount, currency, tolerance), accepted evidence sets, public/private split. |
| Generator (A03–A14) | `fingen/` | Builds a consistent company history first, then injects errors and records every change. 4 error types, 2 combinations, decoy cases. Same config + seed → byte-identical files. |
| Independent checker (A15, A08) | `fincheck/` | Never imports the generator. Recomputes every declared error from open files, checks each evidence set is sufficient, rebuilds the pre-error books and checks they balance, scans for leaks. Includes the A08 control episode for Alimzhan. |
| Paired episodes (A18) | `fingen/transform.py` | Shuffles rows and renames all IDs consistently; scores stay identical. Writes `id_map.json`. |
| Commands (A20) | `python -m fingen generate / build / pair / selfcheck` | Generate, build whole sets, make pairs, generate twice and compare byte for byte. |
| Episode sets (A19) | `configs/train.json`, `configs/dev.json`, `episodes/` | 10 train + 10 dev (non-overlapping seeds) + 20 pairs. All 40 pass the checker. Table in `configs/README.md`. |
| Container (A21) | `Dockerfile`, `LICENSE` (MIT), `scripts/check.py` | One command runs the tests, generation with a separate seed, reproducibility and episode checks, policy evaluation, trajectory replay and both demo scenarios. |
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

## Measurements from the initial generator review

- Generating an episode: ~0.25 s, peak memory 61 MB (limits: 60 s, 4 GB).
- All 40 open episodes: ~5 MB on disk, verified in ~1.2 s.

## Integrated status

The shared build runs the generator and audit environment on the checked-in train and dev sets. The process sandbox prevents policies from opening files or network sockets. The integrated check includes the A08 fixture, four dev policies, training trajectories and renamed pairs.

[Final demo](../docs/DEMO.md) · [Submission package](../submission/README.md)
