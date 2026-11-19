"""Deterministic random streams.

One :class:`EpisodeRng` per episode; every component draws from its own
sub-stream seeded by ``sha256(f"{seed}:{name}")`` so that adding draws in one
component never shifts another component's output.
"""

from __future__ import annotations

import hashlib
import random


def substream_seed(seed: int, name: str) -> int:
    """64-bit seed derived from ``(seed, component name)``."""
    digest = hashlib.sha256(f"{seed}:{name}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")


class EpisodeRng:
    """Per-episode random state with named, independent sub-streams."""

    def __init__(self, seed: int) -> None:
        self.seed = int(seed)
        self._streams: dict[str, random.Random] = {}

    def stream(self, name: str) -> random.Random:
        """Return the (cached) ``random.Random`` for component ``name``."""
        rnd = self._streams.get(name)
        if rnd is None:
            rnd = random.Random(substream_seed(self.seed, name))
            self._streams[name] = rnd
        return rnd
