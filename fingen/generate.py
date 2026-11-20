"""End-to-end episode generation: config -> baseline -> injection -> files."""

from __future__ import annotations

from dataclasses import dataclass

from . import inject
from .config import MAX_LEDGER_ROWS, load_config_file, resolve_config
from .export import (episode_dirs, render_provenance, render_public, render_truth, sha256_hex,
                     write_files)
from .history import GenerationError, History, build_history
from .rng import EpisodeRng


@dataclass
class Episode:
    history: History
    issues: list
    public: dict[str, bytes]
    private: dict[str, bytes]

    @property
    def episode_id(self) -> str:
        return self.history.episode_id

    @property
    def split(self) -> str:
        return self.history.config["split"]


def build_episode(raw_config: dict) -> Episode:
    """Generate one episode in memory (no file I/O)."""
    cfg = resolve_config(raw_config)
    er = EpisodeRng(cfg["seed"])
    history = build_history(cfg, er)
    baseline_sha = {name: sha256_hex(data) for name, data in render_public(history).items()}
    issues = inject.apply(history, cfg, er)
    if len(history.rows) > MAX_LEDGER_ROWS:
        raise GenerationError(f"ledger rows exceed {MAX_LEDGER_ROWS} after applying errors")
    public = render_public(history)
    private = {"truth.json": render_truth(history, issues),
               "provenance.json": render_provenance(history, baseline_sha)}
    return Episode(history, issues, public, private)


def write_episode(ep: Episode, out_dir: str) -> tuple[str, str]:
    pub, priv = episode_dirs(out_dir, ep.split, ep.episode_id)
    write_files(pub, ep.public)
    write_files(priv, ep.private)
    return pub, priv


def generate_episode(raw_config: dict, out_dir: str) -> tuple[Episode, str, str]:
    ep = build_episode(raw_config)
    pub, priv = write_episode(ep, out_dir)
    return ep, pub, priv


def generate_from_file(path: str, out_dir: str, index: int | None = None):
    """Generate all episodes of a config file (or only ``index``)."""
    items = load_config_file(path)
    if index is not None:
        if not 0 <= index < len(items):
            raise IndexError(f"--index {index} out of range (file has {len(items)} configs)")
        items = [items[index]]
    return [generate_episode(item, out_dir) for item in items]
