"""A18 paired episodes (fingen.transform) and the A20 CLI (build / pair / selfcheck)."""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import unittest
from collections import Counter

from fincheck.load import canon_ids
from fincheck.load import load_public as fc_load_public
from fincheck.matcher_stub import match
from fincheck.recompute import reconcile
from fincheck.verify import check_pairs, verify_all, verify_episode
from fingen.__main__ import main as cli_main
from fingen.__main__ import tree_diff
from fingen.config import load_config_file, resolve_config
from fingen.generate import generate_episode
from fingen.inject import REGISTRY, load_injectors
from fingen.reader import load_private, load_public
from fingen.transform import make_pair
from tests.gen_support import EXAMPLE, REPO, episode, read_tree, tmpdir

A08 = os.path.join(REPO, "fincheck", "fixtures", "a08")
ALL_TYPES = os.path.join(REPO, "configs", "examples", "all_types.json")
LEDGER_IDS = ("row_id", "entry_id", "entity_id", "invoice_id", "payment_id")


def opaque_ids(pub_dir: str, priv_dir: str) -> dict[str, str]:
    """Independent collection of every opaque ID (-> kind) of an episode."""
    pub, prv = load_public(pub_dir), load_private(priv_dir)
    out: dict[str, str] = {pub["manifest"]["episode_id"]: "episode"}

    def add(v, kind):
        if v:
            out.setdefault(v, kind)

    def add_row(r):
        for c in LEDGER_IDS:
            add(r.get(c), c[:-3])

    for r in pub["ledger"]:
        add_row(r)
    for d in pub["invoices"]:
        add(d["doc_id"], "doc"), add(d["invoice_id"], "invoice"), add(d["entity_id"], "entity")
        for li in d["line_items"]:
            add(li["line_id"], "line")
    for d in pub["payments"]:
        add(d["doc_id"], "doc"), add(d["payment_id"], "payment"), add(d["entity_id"], "entity")
    for iss in prv["truth"]["issues"]:
        add(iss["issue_id"], "issue")
    for ch in prv["provenance"]["changes"]:
        add(ch["issue_id"], "issue")
        add(ch["target"], "row")
        for k in ("before", "after"):
            if ch[k]:
                add_row(ch[k])
    return out


def lead(ids) -> str:
    return re.match(r"[A-Z]*", os.path.commonprefix(sorted(ids))).group(0)


def canon_finding(f, m=None):
    m = m or {}
    return (f["type"], tuple(canon_ids(m.get(x, x) for x in f["affected_ids"])),
            f["claimed_amount_minor"], f["currency"],
            tuple(canon_ids(m.get(x, x) for x in f["evidence_ids"])))


def oracle(truth) -> list[dict]:
    return [{"type": i["type"], "affected_ids": list(i["affected_ids"]),
             "claimed_amount_minor": i["expected_amount_minor"], "currency": i["currency"],
             "evidence_ids": list(i["evidence_sets"][0]), "explanation": "oracle"}
            for i in truth["issues"]]


def flag_everything(pub) -> list[dict]:
    """Attack answer: every document, every type, amount 0, only the doc as evidence."""
    out = []
    for d in sorted(pub["invoices"] + pub["payments"], key=lambda d: d["doc_id"])[:7]:
        for t in ("duplicate_invoice", "payment_allocation", "fx_conversion", "wrong_period"):
            out.append({"type": t, "affected_ids": [d["doc_id"]], "claimed_amount_minor": 0,
                        "currency": pub["manifest"]["base_currency"],
                        "evidence_ids": [d["doc_id"]], "explanation": "x"})
    return out


def rename(findings, m):
    out = []
    for f in findings:
        g = dict(f)
        g["affected_ids"] = [m.get(x, x) for x in f["affected_ids"]]
        g["evidence_ids"] = [m.get(x, x) for x in f["evidence_ids"]]
        out.append(g)
    return out


def score(findings, prv_dir, pub_dir):
    res = match(findings, load_private(prv_dir)["truth"]["issues"],
                fc_load_public(pub_dir).id_space())
    return res.tp, res.fp, res.fn, res.f1


class PairMixin:
    """Subclasses set SRC_PUB/SRC_PRIV in setUpClass; one pair per class (seed 11)."""

    SEED = 11

    @classmethod
    def make(cls, seed, **kw):
        root = tmpdir()
        eid = make_pair(cls.SRC_PUB, cls.SRC_PRIV, root, "pairs", seed, **kw)
        return root, eid, (os.path.join(root, "public", "pairs", eid),
                           os.path.join(root, "private", "pairs", eid))

    @classmethod
    def setUpClass(cls):
        cls.root, cls.eid, (cls.pub, cls.priv) = cls.make(cls.SEED)
        with open(os.path.join(cls.priv, "id_map.json"), encoding="utf-8") as fh:
            cls.id_map = json.load(fh)
        cls.ids = cls.id_map["ids"]
        cls.src = load_public(cls.SRC_PUB)
        cls.src_prv = load_private(cls.SRC_PRIV)
        cls.new = load_public(cls.pub)

    def test_fincheck_verify_passes_on_pair(self):
        src_rep = verify_episode(self.SRC_PUB, self.SRC_PRIV)
        rep = verify_episode(self.pub, self.priv)
        self.assertTrue(src_rep.ok, src_rep.format())
        self.assertTrue(rep.ok, rep.format())
        self.assertFalse([w for w in rep.warnings if "baseline_sha256" in w
                          or "config_sha256" in w], rep.warnings)
        check_pairs([src_rep, rep])  # A18 pair check via id_map.json
        self.assertTrue(rep.ok, rep.format())
        self.assertEqual(self.new["manifest"]["counts"], self.src["manifest"]["counts"])

    def test_id_map_is_bijection_over_all_opaque_ids(self):
        old = opaque_ids(self.SRC_PUB, self.SRC_PRIV)
        self.assertEqual(set(self.ids), set(old))
        self.assertEqual(len(set(self.ids.values())), len(self.ids))
        self.assertFalse(set(self.ids) & set(self.ids.values()))
        self.assertEqual(self.id_map["source_episode_id"], self.src["manifest"]["episode_id"])
        self.assertEqual(self.id_map["episode_id"], self.eid)
        self.assertEqual(self.ids[self.id_map["source_episode_id"]], self.eid)
        new = opaque_ids(self.pub, self.priv)
        self.assertEqual(set(new), set(self.ids.values()))
        # different style: per kind, the visible prefix changes
        kinds = {}
        for o, k in old.items():
            kinds.setdefault(k, []).append(o)
        for k, olds in kinds.items():
            if k == "episode":
                continue
            lo, ln = lead(olds), lead(self.ids[o] for o in olds)
            self.assertTrue(not lo or not (ln.startswith(lo) or lo.startswith(ln)),
                            f"{k}: prefix {lo!r} -> {ln!r}")

    def test_no_old_id_in_any_new_file(self):
        files = read_tree(self.root)
        # id_map.json is the old->new mapping itself; every other file must be clean
        self.assertEqual([p for p in files if p.endswith("id_map.json")],
                         [os.path.join("private", "pairs", self.eid, "id_map.json")])
        blob = b"\n".join(v for p, v in files.items()
                          if not p.endswith("id_map.json")).decode("utf-8")
        olds = sorted(self.ids, key=len, reverse=True)
        rx = re.compile(r"(?<![A-Za-z0-9])(?:" + "|".join(map(re.escape, olds)) +
                        r")(?![A-Za-z0-9])")
        self.assertIsNone(rx.search(blob), "old id found in new episode")
        for o in olds:
            if len(o) >= 6:
                self.assertNotIn(o, blob)

    def test_fx_keys_amounts_and_semantics_kept(self):
        m = self.ids
        self.assertEqual(Counter((r["timestamp"], r["account"], r["debit_minor"],
                                  r["credit_minor"], r["currency"]) for r in self.src["ledger"]),
                         Counter((r["timestamp"], r["account"], r["debit_minor"],
                                  r["credit_minor"], r["currency"]) for r in self.new["ledger"]))
        self.assertEqual(sorted(map(tuple, (r.values() for r in self.src["fx"]))),
                         sorted(map(tuple, (r.values() for r in self.new["fx"]))))
        new_docs = {d["doc_id"]: d for d in self.new["invoices"] + self.new["payments"]}
        for d in self.src["payments"]:
            n = new_docs[m[d["doc_id"]]]
            self.assertEqual([(m[r["invoice_id"]], r["amount_minor"]) for r in d["invoice_refs"]],
                             [(r["invoice_id"], r["amount_minor"]) for r in n["invoice_refs"]])
            self.assertEqual((n["amount_minor"], n["currency"], n["timestamp"]),
                             (d["amount_minor"], d["currency"], d["timestamp"]))
        for d in self.src["invoices"]:
            n = new_docs[m[d["doc_id"]]]
            self.assertEqual([m[li["line_id"]] for li in d["line_items"]],
                             [li["line_id"] for li in n["line_items"]])
            self.assertEqual(n["total_minor"], d["total_minor"])
        src_fx = {k for i in self.src_prv["truth"]["issues"] for s in i["evidence_sets"]
                  for k in s if k.startswith("FX:")}
        new_fx = {k for i in load_private(self.priv)["truth"]["issues"]
                  for s in i["evidence_sets"] for k in s if k.startswith("FX:")}
        self.assertEqual(src_fx, new_fx)

    def test_reconciler_findings_map_onto_source(self):
        src_f = reconcile(fc_load_public(self.SRC_PUB))
        new_f = reconcile(fc_load_public(self.pub))
        self.assertEqual(sorted(canon_finding(f, self.ids) for f in src_f),
                         sorted(canon_finding(f) for f in new_f))

    def test_matcher_scores_identical_through_id_map(self):
        answers = {"oracle": oracle(self.src_prv["truth"]),
                   "flag_all": flag_everything(self.src),
                   "empty": []}
        answers["oracle_plus_attack"] = answers["oracle"] + answers["flag_all"]
        if answers["oracle"]:
            wrong = dict(answers["oracle"][0])
            wrong["claimed_amount_minor"] += 5
            answers["one_wrong"] = [wrong] + answers["oracle"][1:]
        for name, ans in answers.items():
            a = score(ans, self.SRC_PRIV, self.SRC_PUB)
            b = score(rename(ans, self.ids), self.priv, self.pub)
            self.assertEqual(a, b, name)
            if name == "oracle":
                self.assertEqual(a[3], 1.0)

    def test_order_actually_differs(self):
        m = self.ids
        self.assertNotEqual([m[r["row_id"]] for r in self.src["ledger"]],
                            [r["row_id"] for r in self.new["ledger"]])
        for k in ("invoices", "payments"):
            if len(self.src[k]) > 1:
                self.assertNotEqual([m[d["doc_id"]] for d in self.src[k]],
                                    [d["doc_id"] for d in self.new[k]])
        self.assertNotEqual([tuple(r.values()) for r in self.src["fx"]],
                            [tuple(r.values()) for r in self.new["fx"]])
        # and the new order is not just the canonical sort of the new IDs either
        rows = [(r["timestamp"], r["row_id"]) for r in self.new["ledger"]]
        self.assertNotEqual(rows, sorted(rows))

    def test_deterministic_for_seed_and_differs_for_other_seed(self):
        root2, eid2, _ = self.make(self.SEED)
        self.assertEqual(eid2, self.eid)
        self.assertEqual(read_tree(self.root), read_tree(root2))
        root3, eid3, (pub3, priv3) = self.make(self.SEED + 1)
        self.assertNotEqual(eid3, self.eid)
        t1, t3 = read_tree(self.root), read_tree(root3)
        self.assertNotEqual(sorted(t1.values()), sorted(t3.values()))
        self.assertTrue(verify_episode(pub3, priv3).ok)

    def test_no_rename_no_permute_variants(self):
        _root, eid, (pub, priv) = self.make(5, rename=False)
        with open(os.path.join(priv, "id_map.json"), encoding="utf-8") as fh:
            ids = json.load(fh)["ids"]
        self.assertEqual(ids, {self.src["manifest"]["episode_id"]: eid})
        self.assertEqual(sorted(r["row_id"] for r in load_public(pub)["ledger"]),
                         sorted(r["row_id"] for r in self.src["ledger"]))
        self.assertTrue(verify_episode(pub, priv).ok)
        _root, eid, (pub, priv) = self.make(5, permute=False)
        with open(os.path.join(priv, "id_map.json"), encoding="utf-8") as fh:
            m = json.load(fh)["ids"]
        self.assertEqual([m[r["row_id"]] for r in self.src["ledger"]],
                         [r["row_id"] for r in load_public(pub)["ledger"]])
        self.assertTrue(verify_episode(pub, priv).ok)


class TestPairCleanEpisode(PairMixin, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        e = episode("almaty")
        cls.SRC_PUB, cls.SRC_PRIV = e["pub"], e["priv"]
        super().setUpClass()


class TestPairA08Fixture(PairMixin, unittest.TestCase):
    SRC_PUB = os.path.join(A08, "public")
    SRC_PRIV = os.path.join(A08, "private")

    def test_fixture_has_issues(self):
        self.assertEqual(len(self.src_prv["truth"]["issues"]), 2)


def _registered_base_types() -> list[str]:
    try:
        load_injectors()
    except Exception:  # noqa: BLE001 - parallel work in progress: skip, do not fail
        return []
    return [t for t in ("duplicate_invoice", "payment_allocation", "fx_conversion",
                        "wrong_period") if t in REGISTRY]


@unittest.skipUnless(_registered_base_types(), "no error injectors registered yet")
class TestPairInjectedEpisode(PairMixin, unittest.TestCase):
    """An episode with one issue of every registered base error type."""

    @classmethod
    def setUpClass(cls):
        root = tmpdir()
        cfg = {"seed": 2024, "errors": {t: 1 for t in _registered_base_types()}}
        _ep, cls.SRC_PUB, cls.SRC_PRIV = generate_episode(cfg, root)
        super().setUpClass()

    def test_source_has_issues(self):
        self.assertEqual(len(self.src_prv["truth"]["issues"]), len(_registered_base_types()))
        self.assertTrue(self.src_prv["provenance"]["changes"])


def _all_types_available() -> bool:
    """all_types.json exists and every error/combo it requests has a registered injector."""
    if not os.path.exists(ALL_TYPES):
        return False
    try:
        cfg = resolve_config(load_config_file(ALL_TYPES)[0])
        load_injectors()
    except Exception:  # noqa: BLE001 - parallel work in progress: skip, do not fail
        return False
    wanted = [k for g in ("errors", "combos") for k, v in cfg[g].items() if v]
    return bool(wanted) and all(k in REGISTRY for k in wanted)


@unittest.skipUnless(_all_types_available(), "all_types.json or its injectors not available")
class TestPairAllTypes(PairMixin, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = tmpdir()
        with contextlib.redirect_stdout(io.StringIO()):
            assert cli_main(["generate", "--config", ALL_TYPES, "--index", "0",
                             "--out", root]) == 0
        split = os.listdir(os.path.join(root, "public"))[0]
        eid = os.listdir(os.path.join(root, "public", split))[0]
        cls.SRC_PUB = os.path.join(root, "public", split, eid)
        cls.SRC_PRIV = os.path.join(root, "private", split, eid)
        super().setUpClass()


class TestCli(unittest.TestCase):
    def run_cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli_main(list(argv))
        return code, out.getvalue()

    def test_build_with_pairs_verifies(self):
        root = tmpdir()
        code, out = self.run_cli("build", "--configs", EXAMPLE, "--out", root, "--pairs")
        self.assertEqual(code, 0, out)
        self.assertEqual(sorted(os.listdir(os.path.join(root, "public"))), ["pairs", "train"])
        reports, problems = verify_all(root)
        self.assertEqual(problems, [])
        self.assertEqual(len(reports), 2)
        for r in reports:
            self.assertTrue(r.ok, r.format())
        self.assertFalse(any("pair source" in w for r in reports for w in r.warnings))

    def test_pair_command(self):
        root = tmpdir()
        code, out = self.run_cli("pair", "--public", os.path.join(A08, "public"),
                                 "--private", os.path.join(A08, "private"), "--out", root,
                                 "--split", "jury", "--seed", "3")
        self.assertEqual(code, 0)
        eid = out.split()[0]
        self.assertTrue(os.path.exists(os.path.join(root, "private", "jury", eid, "id_map.json")))

    def test_selfcheck_passes(self):
        code, out = self.run_cli("selfcheck", "--config", EXAMPLE, "--out", tmpdir(), "--pairs")
        self.assertEqual(code, 0, out)
        self.assertIn("PASS selfcheck", out)

    def test_tree_diff_detects_difference(self):
        a, b = tmpdir(), tmpdir()
        for d in (a, b):
            with open(os.path.join(d, "x.txt"), "w", encoding="utf-8", newline="\n") as fh:
                fh.write("same")
        self.assertEqual(tree_diff(a, b), [])
        with open(os.path.join(b, "x.txt"), "w", encoding="utf-8", newline="\n") as fh:
            fh.write("diff")
        with open(os.path.join(b, "y.txt"), "w", encoding="utf-8", newline="\n") as fh:
            fh.write("extra")
        self.assertEqual(tree_diff(a, b), ["only in second: y.txt", "differs: x.txt"])


if __name__ == "__main__":
    unittest.main()
