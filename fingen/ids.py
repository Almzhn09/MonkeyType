"""Opaque identifier generation (CONTRACT §2 "ID space").

Every ID is uppercase, has no spaces and is built from a per-episode prefix
and separator plus a random token drawn from the episode RNG. Tokens are not
sequential, so neither IDs nor sort order reveal generation order. A single
``IdFactory`` per episode guarantees uniqueness across *all* kinds; injected
rows must take their IDs from the same factory (same distribution).
"""

from __future__ import annotations

import hashlib
import random

ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # Crockford base32 (no I, L, O, U)

KINDS = ("row", "entry", "doc", "invoice", "payment", "entity", "line", "issue")

PREFIX_POOLS: dict[str, tuple[str, ...]] = {
    "row": ("R", "RW", "LN", "GLR", "JL", "TX", "RR"),
    "entry": ("E", "JE", "EN", "GLE", "PST", "VCH", "JV"),
    "doc": ("DOC", "D", "DC", "SRC", "DK", "FILE", "DOCU"),
    "invoice": ("INV", "SI", "IN", "BILL", "INVC", "FAC", "SINV"),
    "payment": ("PAY", "PMT", "RCPT", "RC", "PY", "TRF", "RCV"),
    "entity": ("CUST", "CL", "CP", "ENT", "PTY", "BP", "ACC"),
    "line": ("L", "LI", "POS", "LINE", "IT"),
    "issue": ("ISS",),
}

TOKEN_LENGTHS: dict[str, tuple[int, int]] = {
    "row": (6, 8), "entry": (6, 8), "doc": (6, 9), "invoice": (5, 7),
    "payment": (5, 7), "entity": (4, 6), "line": (4, 6), "issue": (6, 6),
}


def fx_key(date: str, source: str, target: str) -> str:
    """CONTRACT §2 FX key, e.g. ``FX:2026-03-31:EUR:KZT``."""
    return f"FX:{date}:{source}:{target}"


def episode_id_from(config_sha256: str) -> str:
    """``EP-`` + 10 base32 chars derived from the canonical config hash."""
    n = int.from_bytes(hashlib.sha256(f"{config_sha256}:episode_id".encode()).digest()[:8], "big")
    chars = []
    for _ in range(10):
        n, r = divmod(n, 32)
        chars.append(ALPHABET[r])
    return "EP-" + "".join(chars)


class IdFactory:
    """Per-episode ID generator. Styles (prefix, separator, token length,
    optional year segment for business numbers) are drawn once from ``rng``."""

    def __init__(self, rng: random.Random, year: int | None = None) -> None:
        self._rng = rng
        self._used: set[str] = set()  # membership only; never iterated
        sep = rng.choice(("-", "-", "_", ""))
        self.styles: dict[str, dict] = {}
        taken: list[str] = []
        for kind in KINDS:
            pool = [p for p in PREFIX_POOLS[kind] if p not in taken]
            prefix = rng.choice(pool)
            taken.append(prefix)
            lo, hi = TOKEN_LENGTHS[kind]
            kind_sep = sep if sep else ("-" if kind == "issue" else "")
            style = {"prefix": prefix, "sep": kind_sep, "length": rng.randint(lo, hi), "year": None}
            if kind in ("invoice", "payment") and year is not None and rng.random() < 0.4:
                style["year"] = str(year)
            self.styles[kind] = style

    def reserve(self, value: str) -> None:
        if value in self._used:
            raise ValueError(f"duplicate id {value!r}")
        self._used.add(value)

    def exists(self, value: str) -> bool:
        return value in self._used

    def new(self, kind: str) -> str:
        style = self.styles[kind]
        while True:
            token = "".join(self._rng.choice(ALPHABET) for _ in range(style["length"]))
            parts = [style["prefix"]]
            if style["year"]:
                parts.append(style["year"])
            parts.append(token)
            value = style["sep"].join(parts) if style["sep"] else "".join(parts)
            if value not in self._used and not value.startswith("FX"):
                self._used.add(value)
                return value
