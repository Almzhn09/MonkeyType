"""fingen — synthetic financial-episode generator (contract v1.0, docs/CONTRACT.md).

Pipeline: config (A03) -> hidden baseline history (A04) -> injection hooks
(A09-A12, see ``fingen.inject``) -> public/private export (A05-A07).

Standard library only; deterministic for a given config + seed.
"""

SCHEMA_VERSION = "1.0"
GENERATOR_VERSION = "fingen-0.1.1"

__all__ = ["SCHEMA_VERSION", "GENERATOR_VERSION"]
