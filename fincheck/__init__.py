"""fincheck — independent verifier for synthetic financial episodes (contract v1.0).

Reads episode files from disk and docs/CONTRACT.md rules only; it never imports the
generator package, so the reference answer and its evidence are checked independently.

Modules:
  load          strict loader/validator of public and private episode files
  recompute     posting rules, reference reconciler, evidence sufficiency
  matcher_stub  reference TP rule, one-to-one matching, P/R/F1
  verify        per-episode verification report and CLI (python -m fincheck)
"""

SCHEMA_VERSION = "1.0"
__all__ = ["SCHEMA_VERSION"]
