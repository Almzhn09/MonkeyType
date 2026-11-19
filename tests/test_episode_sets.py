"""A19/A20: train/dev configuration sets and the committed episodes/ tree."""
import os
import tempfile
import unittest

from fingen.__main__ import build, tree_diff
from fingen.config import ERROR_TYPES, COMBO_TYPES, load_config_file, resolve_config

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPLITS = {"train": (1000, 1999), "dev": (2000, 2999)}


def cfg_path(split):
    return os.path.join(ROOT, "configs", f"{split}.json")


class EpisodeSets(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.raw = {s: load_config_file(cfg_path(s)) for s in SPLITS}
        cls.resolved = {s: [resolve_config(c) for c in cs] for s, cs in cls.raw.items()}

    def test_counts_and_split_field(self):
        for s, cs in self.resolved.items():
            self.assertEqual(len(cs), 10, s)
            self.assertTrue(all(c["split"] == s for c in cs), s)

    def test_seeds(self):
        allseeds = []
        for s, (lo, hi) in SPLITS.items():
            seeds = [c["seed"] for c in self.resolved[s]]
            self.assertEqual(len(set(seeds)), 10, s)
            self.assertTrue(all(lo <= x <= hi for x in seeds), s)
            allseeds += seeds
        self.assertEqual(len(set(allseeds)), 20)

    def test_coverage(self):
        for s, cs in self.resolved.items():
            for t in ERROR_TYPES:
                alone = [c for c in cs if c["errors"][t] > 0
                         and sum(c["errors"].values()) == c["errors"][t]
                         and not any(c["combos"].values())]
                self.assertTrue(alone, f"{s}: {t} not covered alone")
            clean = [c for c in cs if not any(c["errors"].values())
                     and not any(c["combos"].values())]
            self.assertGreaterEqual(len(clean), 2, s)
            self.assertTrue(all(sum(c["distractors"].values()) > 0 for c in clean), s)
            for k in COMBO_TYPES:
                self.assertTrue(any(c["combos"][k] for c in cs), f"{s}: {k}")

    def test_episode_dirs_match_build(self):
        with tempfile.TemporaryDirectory() as d:
            for s in SPLITS:
                ids = build([cfg_path(s)], d, quiet=True)
                self.assertEqual(len(ids), 10)
                on_disk = sorted(os.listdir(os.path.join(ROOT, "episodes", "public", s)))
                self.assertEqual(on_disk, sorted(ids), s)
                priv = sorted(os.listdir(os.path.join(ROOT, "episodes", "private", s)))
                self.assertEqual(priv, sorted(ids), s)

    def test_rebuild_reproduces_committed_tree(self):
        with tempfile.TemporaryDirectory() as d:
            build([cfg_path("train"), cfg_path("dev")], d, pairs=True, quiet=True)
            diffs = tree_diff(os.path.join(ROOT, "episodes"), d)
            self.assertEqual(diffs, [], diffs[:10])


if __name__ == "__main__":
    unittest.main()
